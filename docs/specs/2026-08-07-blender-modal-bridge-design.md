# blender_modal_bridge 设计文档（Blender 云端渲染农场）

日期:2026-08-07 · 状态:已与用户对齐,待实施
决策链:独立新项目(与 comfyui_modal_bridge 零耦合)→ Blender addon + N 面板 UI → HTTP 直传自建端点 → MVP=渲染、二期=烘焙贴图(协议预留 task_type)

## 1. 背景与目标

给 Mac 上的 Blender 5.2 LTS 加一个 addon,把渲染类重负载丢到 Modal serverless GPU(默认 L40S,OptiX)上并行跑,结果自动取回本地。**不经 ComfyUI / comfyui_modal_bridge**——那是 AIGC 工作流的桥,本项目是 DCC 管线工具,各管各的。

用户是游戏 TA:一期解决"渲视频/渲静帧"(look dev、转台、成片),二期解决"渲贴图"(texture baking:高模→低模 normal/AO、程序化材质烘 PBR 贴图集)——bake 与渲染共用 Cycles 路径追踪内核(同吃 OPTIX/RT core),云端镜像与 GPU 选型完全复用,二期只是多一种 job 分发方式。

**明确不做**:EEVEE(无头要 GPU context/EGL)、物理模拟烘焙(时序依赖不能并行,只是卸载不是加速,cache 回传贵)、批量几何处理/任意 bpy 脚本(安全面大,看需求三期再议)、多用户队列、ComfyUI 任何改动。

## 2. 总体架构

```
Mac Blender 5.2 (addon UI)                Modal 云端 (独立 app: blender-bridge)
┌─────────────────────────┐    HTTPS     ┌──────────────────────────────────┐
│ N 面板: 提交/进度/取回     │ ──upload──→ │ upload 端点 → Volume blender-bridge│
│ pack 副本 + 本地预检       │ ──run─────→ │ coordinator(CPU) 滑动窗口分片      │
│ 后台线程 + timer 轮询      │ ←─status──  │   └→ render_frame(L40S,OPTIX)×N  │
│ 结果下载 //render_farm/    │ ←─fetch───  │ 产物 _outputs/<job>/ (mp4|zip)    │
└─────────────────────────┘              └──────────────────────────────────┘
```

仓库布局(`/workspace/documents/blender_modal_bridge/`,经挂载 Mac 可见,addon 可直接从挂载目录装载;项目名与 comfyui_modal_bridge 平行,内部模块保留 farm_* 短前缀指"渲染农场功能"):

```
blender_modal_bridge/
├── addon/blender_modal_bridge/  # Blender addon(Mac 侧,纯 stdlib,零第三方依赖)
│   ├── __init__.py            # bl_info、注册
│   ├── client.py              # HTTP 客户端(urllib;upload/run/status/cancel/fetch)
│   ├── ops.py                 # operators(提交/取消/下载;后台线程 + 队列)
│   ├── ui.py                  # N 面板 + preferences
│   └── jobs.py                # 会话内 job 列表状态(timer 驱动刷新)
├── modal_app/
│   ├── farm_app.py            # Modal app:镜像/worker/coordinator/6 端点
│   └── farm_common.py         # 纯函数协议层(校验/帧列表/ffmpeg;零第三方依赖,单测)
├── farm_deploy.py             # 容器内一键部署(建 Secret + deploy + 打印 endpoint/key)
├── tests/test_farm_common.py
└── docs/specs/…(本文档)
```

## 3. 云端设计

### 3.1 资源与鉴权

- 独立 Modal app `blender-bridge`(与 comfyui-bridge 平行命名)、Volume `blender-bridge`(场景 `scenes/`、产物 `_outputs/`)、Secret `blender-bridge-secrets`(存 `FARM_API_KEY`)、Dict `blender-bridge-jobs`(job 状态,终态 TTL 1h + 数量上限清扫,连带 `:call`/`:subcalls` key)
- 鉴权:自建 `farm_key`(部署时随机生成进 Secret;GET `?key=` / POST body `auth_key`),全部端点校验

### 3.2 镜像

debian_slim **Python 3.13** + `apt xorg libxkbcommon0 ffmpeg` + `pip bpy==5.2.0`(与 Mac Blender 5.2 LTS 严格对版;pip 版 bpy 无头跑 Cycles,不装完整 Blender、不起 X server)。GPU/超时/并行度是部署期 env(`FARM_GPU=L40S` / `FARM_FRAME_TIMEOUT=1800` / `FARM_JOB_TIMEOUT=14400` / `FARM_MAX_PARALLEL=10`)。

### 3.3 端点(6 个)

| 端点 | 方法 | 作用 |
|---|---|---|
| `/upload` | POST(流式 body) | 收 .blend 写 Volume `scenes/<sha1[:8]>_<name>.blend`,返回 `blend_path`。单 POST 流式,百 MB 级;GB 级分块二期 |
| `/run` | POST | 提交 job:`{task_type:"render", blend_path, render:{…}, auth_key}` → `{id, status:"queued", gpu}` |
| `/status` | GET | job 状态;running 带 `progress:{step,total,s_it,elapsed}`(step=已完成帧数) |
| `/cancel` | POST | 取消 coordinator + 连带取消 in-flight 子任务(`:subcalls`) |
| `/fetch` | GET | 流式取回产物(路径囚笼 `_outputs/<job_id>/` 内;`delete=1` 取完删) |
| `/health` | GET | `{healthy, gpu, bpy, deployed_version}` |

### 3.4 job 协议(task_type 从第一版就预留)

```jsonc
{
  "auth_key": "fk-…",
  "task_type": "render",              // MVP 只实现 render;二期加 bake。骨架(upload/status/cancel/fetch/进度)任务类型无关
  "blend_path": "scenes/ab12cd34_scene.blend",
  "render": {
    "frame_start": 1, "frame_end": 250, "frame_step": 1,   // ≤2000 帧/job;帧号 ≤99999
    "output": "video",                // video=render.mp4 | frames=frames.zip
    "fps": 24,
    "file_format": "PNG",             // PNG | OPEN_EXR(EXR 需 output=frames)
    "samples": 128,                   // 以下可选:不给 = 尊重 .blend 场景设置(艺术家文件是真源)
    "resolution_x": 1920, "resolution_y": 1080, "resolution_percentage": 100,
    "camera": "Camera.001"
  }
}
```

### 3.5 render worker(沿用已验证设计)

- `render_frame(job, frame, job_id)`(gpu=L40S,单帧 timeout 1800s,max_containers=并行护栏):
  - **容器级场景缓存 key=job_id**:同 job 分到本容器的所有帧只 `open_mainfile` 一次;跨 job 必重载(overrides 不同,正确性优先)
  - Cycles 设备配置 **OPTIX → CUDA → CPU 逐级回退**;场景引擎非 CYCLES 显式报错;denoiser 仅在场景已开 denoise 时切 OPTIX 实现;实际后端写进结果(`render_device`)可核对
  - 加载后查外部资产断链(`_missing_files`:未 pack 贴图/断链 library)→ 写 job `warnings`(不失败但必须可见)
  - 渲到 /tmp 再 copy 到 Volume(bpy 不直接写 FUSE):`_outputs/<job_id>/frames/frame_%05d.png|exr`
- `render_job(job, job_id)` coordinator(CPU,timeout 4h):**滑动窗口 spawn**(in-flight ≤ 2×并行度)——cancel 需要逐个取消子 call(coordinator 被杀后已 spawn 的帧不会自动停),窗口把要取消的 id 数封顶在 ~20;未 spawn 的帧零费用、中途失败即止损。进度按完成帧数写 Dict;完成后 video→ffmpeg 合成 `render.mp4`(散帧删)、frames→`frames.zip`;产物元数据 `outputs:[{filename, volume_path, size_bytes}]` 写终态
- **demo 模式**:`blend_path` 省略 → 内置金属立方体旋转场景(48 帧 720p),不依赖上传即可冒烟全链路

## 4. Blender addon 设计

### 4.1 UI(3D 视图 N 面板 "Farm" 页签)

- **连接区**:endpoint、farm_key 状态(存 addon preferences,不进 .blend);Test Connection 按钮(打 /health)
- **提交区**:帧范围(默认场景 frame_start/end;"当前帧"一键静帧)、输出 video/frames、格式 PNG/EXR、fps。samples/分辨率**不提供覆盖 UI**——场景是真源(二期看需求再加)
- **任务区**:会话内 job 列表(状态/进度条 step÷total/耗时/s_it),运行中可 Cancel,完成后 Download 按钮;断链警告红字显示

### 4.2 提交流程

1. `bpy.ops.file.pack_all()` + `save_as_mainfile(copy=True)` 到**临时副本**(不动工作文件)
2. **本地预检** missing files(遍历 `bpy.data.images`/`libraries`,pack 不进去的资产也在此暴露)→ 面板红字警告,用户可选择仍然提交
3. 后台线程:HTTP 上传副本(`/upload`)→ 提交 job(`/run`)→ 删临时副本
4. timer 轮询 `/status` 更新面板;完成后**自动** `/fetch` 下载到 `//render_farm/<job_id>/`(preferences 可关自动下载、可改目录)

### 4.3 异步模型

网络 IO 全在 `threading.Thread`(daemon),结果经 `queue.Queue` 回主线程;`bpy.app.timers` 每 2s 驱动:收队列 → 更新 job 列表属性 → `tag_redraw`。**UI 线程永不做网络请求**。Blender 退出/addon 卸载时线程自然终止(daemon),云端 job 不受影响(可重开 Blender 后凭 job_id 查——job 列表持久化到 addon preferences 的 JSON 字段,重启不丢)。

### 4.4 错误面

- 上传失败/网络断:job 条目标 error,可重试(重新提交)
- 云端 failed:显示 error 摘要(trace 前 400 字)
- 轮询超时不误杀:超过 job_timeout 提示"仍在云端跑,可 cancel",不静默放弃

## 5. 二期:烘焙贴图(bake)设计草案

协议与执行模型现在定死,bpy 无头执行细节二期开工时先做云端冒烟验证再实施。

- **哲学延续**:场景是真源——用户在 .blend 里配好 bake 前提(低模 UV 展好、材质里挂目标 image texture 节点并 active、高低模摆好),农场只并行执行
- **协议**:`task_type:"bake"`,`bake:{objects:[低模名…], passes:["NORMAL","AO","DIFFUSE","ROUGHNESS","EMIT","COMBINED"], selected_to_active:bool, cage_extrusion, max_ray_distance, margin, resolution?(覆盖目标 image 尺寸), samples?}`
- **高低模配对**:命名约定 `<name>_low` / `<name>_high`(Substance/Marmoset 同款约定,TA 熟悉);`selected_to_active=true` 时按前缀自动配对,配不上报错
- **并行维度**:对象 × pass 每个一个 input(与逐帧同一滑动窗口骨架);进度 step=已完成 (对象×pass) 数
- **产物**:`<object>_<pass>.png|exr` → `textures.zip`;UDIM 二期内视情况支持
- **UI**:N 面板加 Bake 子区(对象多选/pass 多选/分辨率/margin/cage);其余(上传/进度/取回)全复用
- **开工前置验证**(riskiest first):无头 bpy 下 `bpy.ops.object.bake()` 的 context 准备(active object + selected、材质 active image node)在 Modal 容器里冒烟通过,再写完整实施计划

## 6. 部署与配置链路

- **部署在容器内做**(modal token 已有,与 bridge 同账号):`python farm_deploy.py` 一条命令 = 建/更新 Secret(生成或沿用 farm_key)+ `modal deploy modal_app/farm_app.py` + 打印 endpoint base 与 farm_key。部署期参数经 env 烤进镜像(FARM_GPU 等;运行时读的必须进镜像 `.env()`)
- **Mac 侧一次性**:Blender → Preferences → Add-ons → Install 指向挂载目录的 addon(或 zip);preferences 填 endpoint + farm_key
- **调试链路**:addon 装载/重载/冒烟可经宿主机 Blender MCP(`execute_blender_code`)直接在 Mac Blender 里驱动,不用手点

## 7. 测试策略

- **单测**(容器内 pytest,零云依赖):farm_common 纯函数——render/bake 参数校验、帧列表、frame spec 解析、ffmpeg 命令、bake 配对纯逻辑(二期)
- **云端冒烟**(真金白银,分钟级):部署后 demo 场景 `--frames 1-8` 走 upload-less 全链路(检查 OPTIX 生效、mp4 可播);EXR/frames 链路;cancel 后 Modal dashboard 无残留 running
- **addon 集成**:经宿主机 Blender MCP 在真 Blender 里驱动——装载、Test Connection、提交 demo、看进度、下载落盘;真 .blend(pack 后)提交 4 帧
- 每期收尾把实测性能/费用写进 memory

## 8. 分期交付

| 期 | 内容 | 验收 |
|---|---|---|
| **MVP** | 云端 app(render)+ addon(提交/进度/取回)+ 部署脚本 | Mac Blender 里对真 .blend 一键云渲 8 帧动画取回 mp4;demo 冒烟;cancel 干净 |
| **二期** | bake(云端 worker + 协议 + UI 子区)| 高低模命名约定场景烘 normal+AO 取回贴图 zip |
| 三期(看需求) | BAT 打包 + 资产级 CAS 增量上传(替代原"分块上传"方向,见 Flamenco 调研)、批量几何/LOD、模拟烘焙 | — |
