# blender_modal_bridge

**Blender → Modal serverless 渲染农场**:在 Blender 里一键把当前 .blend 提交到
[Modal](https://modal.com) 的 serverless GPU(默认 L40S + OptiX)逐帧并行渲染,
面板里看进度,产物(mp4 / 帧序列 zip)自动取回本地。

Submit Cycles renders from Blender's N-panel to Modal serverless GPUs — per-frame
parallel rendering with OptiX, live progress, auto-download of results.

```
Blender 5.2 (addon, N 面板)                Modal 云端 (app: blender-bridge)
┌─────────────────────────┐    HTTPS     ┌──────────────────────────────────┐
│ 提交 / 进度 / 取回         │ ──upload──→ │ upload 端点 → Volume              │
│ pack 副本 + 断链预检       │ ──run─────→ │ coordinator(CPU) 滑动窗口分帧      │
│ 后台线程 + timer 轮询      │ ←─status──  │   └→ render_frame(L40S,OptiX)×N  │
│ 结果落 //render_farm/     │ ←─fetch───  │ 产物: render.mp4 | frames.zip     │
└─────────────────────────┘              └──────────────────────────────────┘
```

## 特性

- **逐帧并行**:一段动画拆成单帧任务扇出到多张 GPU(默认上限 10 并行,费用护栏),
  实测 720p/64spp 稳态 ~1.5-2.5s/帧(含场景加载摊销)
- **OptiX 加速**:Cycles `compute_device_type=OPTIX` 吃满 L40S 的 RT core,
  枚举失败自动逐级回退 CUDA → CPU;OptiX denoiser 在容器内不可用,自动降级 OIDN
- **场景是真源**:分辨率 / 采样 / 相机 / fps / denoise 全部尊重 .blend 里的设置,农场不覆盖
- **不动工作文件**:提交时 pack 贴图到临时副本上传,你的 .blend 和会话状态保持原样;
  外部资产断链(缺贴图 / Link 库)提交前后双重警告
- **serverless 计费**:任务结束容器即回收(cancel 实测零残留),不跑不花钱
- **协议可扩展**:job 带 `task_type`,渲染之外的任务类型(如烘焙贴图)加 worker 即可,
  上传 / 状态 / 取消 / 取回骨架不动

## 部署(一次性)

前置:Python 3.10+,Modal 账号已鉴权(`pip install modal && modal token new`)。

```bash
python3 farm_deploy.py                 # 建 Secret + modal deploy + 写 farm_config.json
python3 smoke_test.py --frames 1-8     # 内置 demo 场景全链路冒烟(不需要 .blend)
```

部署脚本打印 `endpoint` 和 `farm_key`(也存在 `farm_config.json`,已 gitignore)。
换卡 / 调并行度:`python3 farm_deploy.py --gpu L4 --max-parallel 6`(部署期参数,重跑生效)。
⚠ 永远经 `farm_deploy.py` 部署,裸跑 `modal deploy` 会丢 FARM_* 环境变量。

## Blender addon 安装(一次性)

要求 Blender **5.2+**(云端 bpy 5.2 与其对版;老版本 .blend 正常,新版本文件有前向兼容风险)。

1. Blender → Preferences → Add-ons → Install,选 `addon/blender_modal_bridge` 目录(或其 zip)
2. 启用 "Blender Modal Bridge (Render Farm)",展开填 **Endpoint** 与 **Farm Key**(部署时打印的两项)
3. 3D 视图按 `N` → **Farm** 页签

## 使用

面板里选帧范围(Scene Range / Current Frame / Custom)、输出(Video mp4 / Frames zip、
PNG / OpenEXR)→ **Submit to Farm**。任务列表实时显示 `已完成帧数/总帧数` 与秒/帧;
完成后自动下载到 `//render_farm/<job_id>/`(preferences 可改目录 / 关自动下载)。

注意:

- **只支持 Cycles**——EEVEE 无头渲染需要 GPU context,不支持(提交后首帧明确报错)
- **Link 的库文件不会打包上传**,面板会红字警告;需要的话先 Make Local
- 单 job ≤ 2000 帧;上传走 HTTP 单 POST 流式,百 MB 级场景可用(GB 级建议先精简)

## 仓库结构

```
addon/blender_modal_bridge/   # Blender addon(纯 stdlib,零第三方依赖)
modal_app/farm_app.py         # Modal app:镜像 / 渲染 worker / coordinator / 6 端点
modal_app/farm_common.py      # 提交协议纯函数(校验 / 帧列表 / ffmpeg;有单测)
farm_deploy.py                # 一键部署
smoke_test.py                 # 全链路冒烟(内置 demo 场景,不依赖上传)
tests/                        # python3 -m pytest tests/ -q
docs/specs/                   # 设计文档
docs/plans/                   # 实施计划
```

## Roadmap

- **二期:烘焙贴图(bake)**——高模→低模 normal / AO、程序化材质烘 PBR 贴图集;
  `_low` / `_high` 命名约定配对,对象 × pass 并行(协议已预留 `task_type: "bake"`)
- **打包升级**:pack_all 换 [BAT(Blender Asset Tracer)](https://pypi.org/project/blender-asset-tracer/)
  ——追踪贴图 / 链接库 / caches 打自包含包,配 **资产级 CAS 增量上传**(改哪个贴图传哪个,
  参考 Flamenco Shaman 的思路),大场景迭代不再重传整包
- Output Properties 面板入口、失败帧单独重试、转台预设

## License

MIT
