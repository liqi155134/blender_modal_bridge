# blender_modal_bridge

Blender 云端渲染农场:Mac Blender 5.2 里一键把当前 .blend 提交到 Modal serverless GPU
(L40S + OptiX)逐帧并行渲染,进度可视,产物(mp4 / 帧序列 zip)自动取回本地。

- 设计文档:`docs/specs/2026-08-07-blender-modal-bridge-design.md`
- 实施计划:`docs/plans/2026-08-07-mvp-render-plan.md`
- MVP = 渲染(静帧/动画/转台);二期 = 烘焙贴图(bake,协议已预留 `task_type`)

## 部署(容器内,一次性)

```bash
# 前置:modal SDK 已装且已鉴权(~/.modal.toml 或 MODAL_TOKEN_ID/SECRET)
python3 farm_deploy.py            # 建 Secret + modal deploy + 写 farm_config.json
python3 smoke_test.py --frames 1-8   # demo 场景全链路冒烟(不需要 .blend)
```

部署脚本会打印 `endpoint` 和 `farm_key`(也存在 `farm_config.json`,不入 git)。
改 GPU/并行度/超时:`python3 farm_deploy.py --gpu L4 --max-parallel 6`(部署期参数,改完重跑即生效)。

## Blender addon 安装(Mac,一次性)

1. Blender → Preferences → Add-ons → Install,选 `addon/blender_modal_bridge`
   (目录经挂载 Mac 可见;或把该目录 zip 后安装)
2. 启用后展开 addon 项,填 **Endpoint** 与 **Farm Key**(上面部署打印的两项)
3. 3D 视图按 N → **Farm** 页签:选帧范围/输出模式 → Submit to Farm

## 使用要点

- **场景是真源**:分辨率/采样数/相机/fps 全部用 .blend 里的设置,农场不覆盖
- **只支持 Cycles**(EEVEE 无头渲染需 GPU context,不支持,提交后首帧会明确报错)
- 提交时自动 pack 贴图到临时副本(不动你的工作文件);**链接库(Link)不会打包**,
  面板会红字警告 —— 需要的话先 Make Local
- 单 job ≤ 2000 帧;并行上限默认 10×L40S(费用护栏 ≈ $18/h 封顶,实际按秒计费)
- 结果默认落 `//render_farm/<job_id>/`(.blend 旁边),完成自动下载(preferences 可关)

## 仓库结构

```
addon/blender_modal_bridge/   # Blender addon(纯 stdlib,零第三方依赖)
modal_app/farm_app.py         # Modal app:镜像/渲染 worker/coordinator/6 端点
modal_app/farm_common.py      # 协议纯函数(校验/帧列表/ffmpeg;有单测)
farm_deploy.py                # 一键部署
smoke_test.py                 # 全链路冒烟(demo 场景,不依赖上传)
tests/                        # python3 -m pytest tests/ -q
```
