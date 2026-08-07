# blender_modal_bridge MVP(渲染链路)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mac Blender 5.2 里一键把当前 .blend 提交到 Modal serverless GPU(L40S+OptiX)逐帧并行渲染,进度可视,产物(mp4/帧 zip)自动取回本地。

**Architecture:** 独立 Modal app `blender-bridge`(6 端点:upload/run/status/cancel/fetch/health,farm_key 自建鉴权)+ Blender addon(N 面板,纯 stdlib,threading+timer 异步)。协议第一版带 `task_type`(MVP 只实现 render,二期 bake 零骨架改动)。coordinator 滑动窗口 spawn 逐帧 GPU 任务,in-flight call id 写 `{job_id}:subcalls` 供 cancel 连带取消。spec:`docs/specs/2026-08-07-blender-modal-bridge-design.md`。

**Tech Stack:** Modal(Volume/Dict/spawn/fastapi_endpoint)、bpy 5.2.0(pip 版无头 Cycles)、ffmpeg、Blender Python API(addon:bpy.props/operators/app.timers)。

## Global Constraints

- 仓库根 `/workspace/documents/blender_modal_bridge/`(git 已 init;经挂载 Mac 可见)
- 云端镜像 Python **3.13** + `bpy==5.2.0`(与 Mac Blender 5.2 LTS 对版);addon `bl_info["blender"] = (5, 2, 0)`
- **Cycles-only**:场景引擎非 CYCLES 显式报错;OPTIX→CUDA→CPU 逐级回退;denoiser 仅在场景已开时切 OPTIX
- **尊重 .blend 场景设置**:MVP 不提供 samples/分辨率覆盖 UI,fps 取场景值;协议里的覆盖字段保留(校验但 addon 不发)
- 单 job ≤ **2000 帧**、帧号 ≤ 99999;并行护栏 `FARM_MAX_PARALLEL=10`(≈$18/h 封顶);单帧 timeout 1800s、整片 14400s
- 产物走 Volume `_outputs/<job_id>/` + `/fetch` 流式,不走 base64;终态字段名 `outputs:[{filename, volume_path, size_bytes}]`
- 命名:Modal app/Volume=`blender-bridge`、Secret=`blender-bridge-secrets`、Dict=`blender-bridge-jobs`;内部模块 farm_* 前缀;鉴权 key 叫 `farm_key`(Secret 里 `FARM_API_KEY`)
- addon(Mac 侧)**零第三方依赖**,只用 stdlib + bpy;网络永不在 UI 线程
- 部署在容器内跑 `farm_deploy.py`(modal token 复用现有);**部署期 env(FARM_*)运行时要读的必须烤进镜像 `.env()`**
- 单测 `python3 -m pytest tests/ -q`(容器 py3.11 跑纯函数,不 import modal/bpy);每 task 一 commit(中文 message)
- MVP 不做:bake(二期)、EEVEE、分块上传、samples/分辨率覆盖 UI、ComfyUI 任何改动

---

### Task 1: farm_common.py — 协议纯函数 + 单测

**Files:**
- Create: `modal_app/farm_common.py`
- Create: `tests/test_farm_common.py`
- Create: `.gitignore`

**Interfaces:**
- Produces(后续所有任务依赖):
  - `normalize_job(payload: dict) -> tuple[dict | None, str | None]` — 校验整个提交 payload(`task_type`/`blend_path`/`render`),返回扁平 job dict:`{task_type, blend_path(str|None), frame_start, frame_end, frame_step, output, file_format, fps}` + 可选 `resolution_x/y, resolution_percentage, samples, camera`
  - `frames_list(job: dict) -> list[int]`
  - `parse_frame_spec(spec: str) -> tuple[int, int, int]`(addon"custom 帧范围"输入解析;非法抛 ValueError)
  - `ffmpeg_cmd(frames_dir: str, out_path: str, fps: int) -> list[str]`
  - `safe_scene_name(name: str) -> str`(upload 端点清洗文件名)
  - `MAX_FRAMES = 2000`
- 零第三方依赖(纯 stdlib):云端容器、addon、测试三方共用

- [ ] **Step 1: 写 .gitignore**

```gitignore
__pycache__/
*.pyc
.pytest_cache/
farm_config.json
```

- [ ] **Step 2: 写失败单测 tests/test_farm_common.py**

```python
"""farm_common 纯函数单测。跑法(仓库根): python3 -m pytest tests/ -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modal_app"))
import farm_common as fc  # noqa: E402


def test_normalize_defaults():
    """最小 payload(demo 单帧)→ 全默认值。"""
    job, err = fc.normalize_job({})
    assert err is None
    assert job["task_type"] == "render" and job["blend_path"] is None
    assert job["frame_start"] == 1 == job["frame_end"] and job["frame_step"] == 1
    assert job["output"] == "video" and job["file_format"] == "PNG" and job["fps"] == 24


def test_normalize_task_type_gate():
    """MVP 只认 render;bake 等二期类型给明确报错而非静默当 render。"""
    _, err = fc.normalize_job({"task_type": "bake"})
    assert err and "bake" in err
    job, err = fc.normalize_job({"task_type": "render"})
    assert err is None


def test_normalize_blend_path_jail():
    """blend_path 只认 Volume 上 scenes/ 下的规范相对路径(upload 端点生成的形态)。"""
    for bad in ("models/x.blend", "scenes/../secrets", "/scenes/x.blend", 123):
        _, err = fc.normalize_job({"blend_path": bad})
        assert err, f"应拒绝 {bad!r}"
    job, err = fc.normalize_job({"blend_path": "scenes/ab12cd34_scene.blend"})
    assert err is None and job["blend_path"] == "scenes/ab12cd34_scene.blend"


def test_normalize_frames_and_limits():
    job, err = fc.normalize_job({"render": {"frame_start": 10, "frame_end": 20, "frame_step": 5}})
    assert err is None and fc.frames_list(job) == [10, 15, 20]
    _, err = fc.normalize_job({"render": {"frame_start": 5, "frame_end": 1}})
    assert err
    _, err = fc.normalize_job({"render": {"frame_start": 1, "frame_end": fc.MAX_FRAMES + 1}})
    assert err and str(fc.MAX_FRAMES) in err
    _, err = fc.normalize_job({"render": {"frame_start": 1, "frame_end": 100000}})
    assert err  # 帧号 > 99999(%05d 字典序会乱)


def test_normalize_video_rejects_exr():
    _, err = fc.normalize_job({"render": {"output": "video", "file_format": "OPEN_EXR"}})
    assert err
    job, err = fc.normalize_job({"render": {"output": "frames", "file_format": "OPEN_EXR"}})
    assert err is None and job["file_format"] == "OPEN_EXR"


def test_normalize_overrides():
    job, err = fc.normalize_job({"render": {"samples": 64, "resolution_x": 1920,
                                            "resolution_y": 1080, "camera": " Cam.001 "}})
    assert err is None
    assert job["samples"] == 64 and job["resolution_x"] == 1920 and job["camera"] == "Cam.001"
    _, err = fc.normalize_job({"render": {"samples": 0}})
    assert err
    _, err = fc.normalize_job({"render": {"samples": "abc"}})
    assert err


def test_parse_frame_spec():
    assert fc.parse_frame_spec("1-250") == (1, 250, 1)
    assert fc.parse_frame_spec("1-250:2") == (1, 250, 2)
    assert fc.parse_frame_spec("7") == (7, 7, 1)
    for bad in ("", "a-b", "1-2:0x"):
        with pytest.raises(ValueError):
            fc.parse_frame_spec(bad)


def test_ffmpeg_cmd():
    cmd = fc.ffmpeg_cmd("/v/_outputs/j1/frames", "/v/_outputs/j1/render.mp4", 24)
    s = " ".join(cmd)
    assert "-framerate 24" in s and "yuv420p" in s and "pad=" in s  # 奇数分辨率兜底
    assert "/v/_outputs/j1/frames/*.png" in s


def test_safe_scene_name():
    assert fc.safe_scene_name("My Scene (v2).blend") == "My_Scene__v2_.blend"
    assert fc.safe_scene_name("../../../etc/passwd") == "passwd"
    assert fc.safe_scene_name("") == "scene.blend"
```

- [ ] **Step 3: 跑单测确认失败**

Run: `cd /workspace/documents/blender_modal_bridge && python3 -m pytest tests/ -q`
Expected: FAIL(`ModuleNotFoundError: farm_common`)

- [ ] **Step 4: 实现 modal_app/farm_common.py**

```python
"""
farm_common.py — 提交协议的纯函数层(校验 / 帧列表 / ffmpeg 命令 / 文件名清洗)。

零第三方依赖(纯 stdlib):云端 farm_app(容器内以顶层名 import)、Blender addon、
单测三方共用。协议改动先改这里的校验再动别处。
task_type 从第一版就存在:MVP 只实现 render,二期 bake 时这里加分支,骨架不动。
"""
import os
import re

MAX_FRAMES = 2000       # 单 job 帧数上限:cancel 窗口可控 + 并行费用护栏
MAX_FRAME_NO = 99999    # 帧号上限:产物文件名 %05d,超了字典序乱、ffmpeg glob 顺序错


def normalize_job(payload: dict) -> tuple[dict | None, str | None]:
    """校验提交 payload,返回 (扁平 job dict, None) 或 (None, 错误信息)。
    payload 形态:{task_type?, blend_path?, render: {…}}。
    blend_path=None 表示内置 demo 场景(不碰 Volume,链路冒烟)。
    resolution/samples/camera 不给 = 尊重 .blend 场景设置(艺术家文件是真源)。"""
    if not isinstance(payload, dict):
        return None, "payload 必须是对象"
    task_type = str(payload.get("task_type") or "render").lower()
    if task_type != "render":
        return None, f"task_type={task_type!r} 暂未支持(MVP 只有 render;bake 二期)"
    blend_path = payload.get("blend_path")
    if blend_path is not None:
        norm = os.path.normpath(str(blend_path)).replace("\\", "/")
        if (not isinstance(blend_path, str) or not blend_path.startswith("scenes/")
                or ".." in blend_path or blend_path != norm):
            return None, "blend_path 必须是 Volume 上 scenes/ 下的相对路径(由 /upload 返回)"
    r = payload.get("render") or {}
    if not isinstance(r, dict):
        return None, "render 必须是对象"
    try:
        start = int(r.get("frame_start", 1))
        end = int(r.get("frame_end", start))
        step = int(r.get("frame_step", 1))
    except (TypeError, ValueError):
        return None, "frame_start / frame_end / frame_step 必须是整数"
    if step < 1:
        return None, "frame_step 必须 ≥ 1"
    if end < start:
        return None, "frame_end 必须 ≥ frame_start"
    if start < 0 or end > MAX_FRAME_NO:
        return None, f"帧号范围 0..{MAX_FRAME_NO}"
    n = len(range(start, end + 1, step))
    if n > MAX_FRAMES:
        return None, f"单 job 最多 {MAX_FRAMES} 帧(现 {n} 帧);请分段提交"
    output = str(r.get("output") or "video").lower()
    if output not in ("video", "frames"):
        return None, "output 只能是 video(合成 mp4)或 frames(zip 帧序列)"
    fmt = str(r.get("file_format") or "PNG").upper()
    if fmt not in ("PNG", "OPEN_EXR"):
        return None, "file_format 只能是 PNG 或 OPEN_EXR"
    if output == "video" and fmt != "PNG":
        return None, "video 输出只支持 PNG 帧;EXR 请用 output=frames"
    try:
        fps = int(r.get("fps", 24))
    except (TypeError, ValueError):
        return None, "fps 必须是整数"
    if not 1 <= fps <= 240:
        return None, "fps 范围 1..240"
    job = {"task_type": task_type, "blend_path": blend_path,
           "frame_start": start, "frame_end": end, "frame_step": step,
           "output": output, "file_format": fmt, "fps": fps}
    for k in ("resolution_x", "resolution_y", "resolution_percentage", "samples"):
        v = r.get(k)
        if v is None:
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None, f"{k} 必须是整数"
        if v < 1:
            return None, f"{k} 必须 ≥ 1"
        job[k] = v
    cam = r.get("camera")
    if cam is not None:
        if not isinstance(cam, str) or not cam.strip():
            return None, "camera 必须是非空字符串(场景里的相机对象名)"
        job["camera"] = cam.strip()
    return job, None


def frames_list(job: dict) -> list[int]:
    """规范化后的 job → 要渲染的帧号列表。"""
    return list(range(job["frame_start"], job["frame_end"] + 1, job["frame_step"]))


def parse_frame_spec(spec: str) -> tuple[int, int, int]:
    """帧范围字符串 → (start, end, step)。"1-250" / "1-250:2" / "7"。
    非法输入抛 ValueError(语义校验交给 normalize_job,这里只管语法)。"""
    s = str(spec).strip()
    step = 1
    if ":" in s:
        s, st = s.rsplit(":", 1)
        step = int(st)
    if "-" in s:
        a, b = s.split("-", 1)
        start, end = int(a), int(b)
    else:
        start = end = int(s)
    return start, end, step


def ffmpeg_cmd(frames_dir: str, out_path: str, fps: int) -> list[str]:
    """PNG 帧序列 → H.264 mp4。glob 按字典序 = 帧序(帧名固定 %05d 零填充)。
    pad 滤镜兜底奇数分辨率(yuv420p 要求偶数,场景分辨率是艺术家定的,不该因此失败)。"""
    return ["ffmpeg", "-y", "-framerate", str(fps), "-pattern_type", "glob",
            "-i", f"{frames_dir}/*.png",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            out_path]


def safe_scene_name(name: str) -> str:
    """上传文件名清洗:取 basename、危险字符换 _,空则兜底。upload 端点用。"""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or "scene.blend"
```

- [ ] **Step 5: 跑单测确认通过**

Run: `python3 -m pytest tests/ -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add .gitignore modal_app/farm_common.py tests/test_farm_common.py
git commit -m "feat: 提交协议纯函数(task_type 预留/校验/帧列表/ffmpeg)+ 单测"
```

---

### Task 2: farm_app.py(一)— 镜像 + demo 场景 + Cycles 配置 + render_frame

**Files:**
- Create: `modal_app/farm_app.py`

**Interfaces:**
- Consumes: `farm_common.frames_list`(Task 3 用)
- Produces:
  - 模块常量:`APP_NAME/VOLUME_NAME/SECRET_NAME/FARM_GPU/BPY_VERSION/FRAME_TIMEOUT/JOB_TIMEOUT/MAX_PARALLEL`、`app: modal.App`、`models_vol`、`job_state`、`farm_image`
  - `render_frame(job: dict, frame: int, job_id: str) -> dict`——返回 `{"frame", "path", "size", "secs", "warnings": list[str], "device": str}`;帧文件落 Volume `_outputs/<job_id>/frames/frame_%05d.(png|exr)`

- [ ] **Step 1: 实现 farm_app.py(本任务先写到 render_frame 为止)**

```python
"""
farm_app.py — Blender Cycles serverless 渲染农场(Modal 独立 app)。

部署:python farm_deploy.py(容器内;别裸跑 modal deploy —— FARM_* env 会缺)。
部署后 6 个端点(<ws> 由 modal 账号决定):
    https://<ws>--blender-bridge-{upload,run,status,cancel,fetch,health}.modal.run

骨架来自 Modal 官方 blender_video 示例,生产化改造:
  1. 资产走 Volume(scenes/,由 /upload 写入)不走 bytes;场景**每容器每 job 只加载一次**
     (缓存 key=job_id:同 job 分到本容器的所有帧免重复 open_mainfile;跨 job 必重载,
     overrides 不同 —— 正确性优先于省那几秒)。
  2. compute_device_type=OPTIX(L40S 142 RT core;Cycles 通常快 1.5-2×),枚举不到
     OPTIX 设备逐级回退 CUDA → CPU;denoiser 仅在场景已开 denoise 时切 OPTIX 实现。
  3. bpy==5.2.0 + Python 3.13(与部署者 Mac Blender 5.2 LTS 对版,避免旧 bpy 开新
     .blend 的前向兼容风险)。
  4. job 协议 task_type 化(farm_common.normalize_job):MVP 只有 render,二期 bake
     加 worker 函数即可,upload/status/cancel/fetch/进度骨架不动。
"""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import modal

import farm_common

APP_NAME = os.environ.get("FARM_APP_NAME", "blender-bridge")
VOLUME_NAME = os.environ.get("FARM_VOLUME", "blender-bridge")
SECRET_NAME = os.environ.get("FARM_SECRET", "blender-bridge-secrets")
FARM_GPU = os.environ.get("FARM_GPU", "L40S")
BPY_VERSION = "5.2.0"   # 单一真源:镜像 pip 与 health 上报都用它
# 单帧超时覆盖重场景(体积/毛发单帧半小时级);coordinator 超时要盖全片。
# ⚠ Modal timeout / gpu / max_containers 都是部署期固定,换值需重新部署。
FRAME_TIMEOUT = int(os.environ.get("FARM_FRAME_TIMEOUT", "1800"))
JOB_TIMEOUT = int(os.environ.get("FARM_JOB_TIMEOUT", "14400"))
MAX_PARALLEL = int(os.environ.get("FARM_MAX_PARALLEL", "10"))

app = modal.App(APP_NAME)
models_vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
job_state = modal.Dict.from_name(f"{APP_NAME}-jobs", create_if_missing=True)

# Secret 含 FARM_API_KEY(farm_deploy.py 创建);没建过则空 secret 兜底(端点会拒绝所有请求)
try:
    farm_secret = modal.Secret.from_name(SECRET_NAME)
except Exception:
    farm_secret = modal.Secret.from_dict({})

# 镜像:pip 版 bpy 无头跑 Cycles,不装完整 Blender、不起 X server(xorg 只是给
# bpy 提供 X11 客户端库依赖)。coordinator/端点共用本镜像(要 ffmpeg;bpy 装了不用,
# 省一次独立 build)。
farm_image = (
    modal.Image.debian_slim(python_version="3.13")   # bpy 5.2 要 py3.13
    .apt_install("xorg", "libxkbcommon0", "ffmpeg")
    .pip_install(f"bpy=={BPY_VERSION}", "fastapi[standard]")
    # 运行时读的 env 必须烤进镜像(deploy 子进程的 env 只在部署解析期可见,不会自动进容器):
    #   APP_NAME/VOLUME/SECRET → 容器 re-import 本文件时顶层 from_name;FARM_GPU → 显示/health
    .env({k: os.environ[k] for k in (
        "FARM_APP_NAME", "FARM_VOLUME", "FARM_SECRET", "FARM_GPU",
    ) if os.environ.get(k)})
    .add_local_python_source("farm_app", "farm_common")
)


# ============================================================================
# job_state 清理:终态条目超 TTL 删,数量兜底(照搬 comfyui_modal_bridge 验证过的策略)
# ============================================================================
JOB_TTL_S = int(os.environ.get("FARM_JOB_TTL", "3600"))
JOB_MAX = int(os.environ.get("FARM_JOB_MAX", "200"))


def _sweep_job_state():
    """best-effort 清理过期/超量的终态 job。任何异常都不影响主流程。"""
    try:
        now = time.time()
        items = list(job_state.items())
    except Exception:
        return
    terminal = {"completed", "failed", "cancelled"}
    finished = [(jid, s.get("completed_at") or 0) for jid, s in items
                if isinstance(s, dict) and s.get("status") in terminal]

    def _drop(jid):
        for k in (jid, f"{jid}:call", f"{jid}:subcalls"):  # 连带删独立 key,不留孤儿
            try:
                del job_state[k]
            except Exception:
                pass

    for jid, done_at in finished:
        if done_at and now - done_at > JOB_TTL_S:
            _drop(jid)
    try:
        remaining = [(j, s.get("completed_at") or 0) for j, s in job_state.items()
                     if isinstance(s, dict) and s.get("status") in terminal]
        if len(remaining) > JOB_MAX:
            remaining.sort(key=lambda x: x[1])
            for jid, _ in remaining[: len(remaining) - JOB_MAX]:
                _drop(jid)
    except Exception:
        pass


# ============================================================================
# 场景装配(全部只在 render_frame 进程内调用;bpy 只在函数内 import —— 本文件
# 顶层会被端点所在的无 GPU 容器 import,顶层 import bpy 白付内存)
# ============================================================================
def _build_demo_scene():
    """内置冒烟场景:金属立方体 360° 旋转 + 地面,48 帧 720p/64 samples。
    不碰 Volume,验证 部署→分帧→OPTIX→合成→取回 全链路。"""
    import math

    import bpy
    bpy.ops.wm.read_factory_settings()
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    scene.cycles.samples = 64
    scene.frame_start, scene.frame_end = 1, 48
    cube = bpy.data.objects["Cube"]
    mat = bpy.data.materials.new("DemoMetal")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.15
    bsdf.inputs["Base Color"].default_value = (0.8, 0.45, 0.1, 1.0)
    cube.data.materials.append(mat)
    cube.rotation_euler = (0.0, 0.0, 0.0)
    cube.keyframe_insert("rotation_euler", frame=1)
    cube.rotation_euler = (0.0, 0.0, math.radians(360.0))
    cube.keyframe_insert("rotation_euler", frame=48)
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -1))


def _apply_overrides(job: dict):
    """payload 显式给的才覆盖;没给的尊重 .blend 场景设置(艺术家的文件是真源)。"""
    import bpy
    scene = bpy.context.scene
    if job.get("resolution_x"):
        scene.render.resolution_x = job["resolution_x"]
    if job.get("resolution_y"):
        scene.render.resolution_y = job["resolution_y"]
    if job.get("resolution_percentage"):
        scene.render.resolution_percentage = job["resolution_percentage"]
    if job.get("samples"):
        scene.cycles.samples = job["samples"]
    cam = job.get("camera")
    if cam:
        obj = bpy.data.objects.get(cam)
        if obj is None or obj.type != "CAMERA":
            cams = [o.name for o in bpy.data.objects if o.type == "CAMERA"]
            raise ValueError(f"场景里没有相机 {cam!r}(可选: {cams})")
        scene.camera = obj


def _missing_files() -> list[str]:
    """加载后查外部资产断链(未 pack 的贴图/链接库):渲染不失败(Blender 出粉色),
    但必须让用户看得见 —— 结果写进 job_state.warnings。"""
    import bpy
    missing = []
    for img in bpy.data.images:
        if img.source == "FILE" and img.filepath and not img.packed_file:
            if not Path(bpy.path.abspath(img.filepath)).exists():
                missing.append(f"image: {img.filepath}")
    for lib in bpy.data.libraries:
        if lib.filepath and not Path(bpy.path.abspath(lib.filepath)).exists():
            missing.append(f"library: {lib.filepath}")
    return missing[:20]   # 封顶,别撑爆 job_state


def _configure_cycles_gpu() -> str:
    """OPTIX 优先(RT core 正解),枚举不到该类设备逐级回退 CUDA → CPU(宁可慢不可炸)。
    只支持 CYCLES:EEVEE 无头需要 GPU context/EGL,显式报错比默默换引擎出错片强。
    返回实际用的后端名(写进产物,用户能核对没白付 RT core 的钱)。"""
    import bpy
    scene = bpy.context.scene
    if scene.render.engine != "CYCLES":
        raise RuntimeError(
            f"场景渲染引擎是 {scene.render.engine},当前只支持 CYCLES"
            "(EEVEE 无头渲染需 GPU context,暂不支持;请在 Blender 里切到 Cycles)")
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dtype in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = dtype
        except TypeError:      # 该 bpy 构建不含此后端
            continue
        prefs.get_devices()
        if any(d.type == dtype for d in prefs.devices):
            for d in prefs.devices:
                d.use = (d.type != "CPU")
            scene.cycles.device = "GPU"
            # 尊重场景的 denoise 开关,只把实现切到 OPTIX(硬件降噪)
            if dtype == "OPTIX" and getattr(scene.cycles, "use_denoising", False):
                scene.cycles.denoiser = "OPTIX"
            print(f"[farm] compute_device={dtype}: "
                  + ", ".join(d.name for d in prefs.devices if d.use))
            return dtype
    print("[farm] ⚠ 未枚举到 GPU 设备,回退 CPU 渲染(慢)")
    scene.cycles.device = "CPU"
    return "CPU"


# 容器级场景缓存:key=job_id。同 job 分到本容器的所有帧只 open_mainfile 一次
# (大场景加载分钟级,这是把官方示例"每帧传 bytes+重加载"换成 Volume 的核心收益)。
_SCENE = {"key": None, "warnings": [], "device": None}


@app.function(
    gpu=FARM_GPU,
    image=farm_image,
    volumes={"/vol": models_vol},
    timeout=FRAME_TIMEOUT,
    max_containers=MAX_PARALLEL,   # 并行度上限 = 费用护栏
)
def render_frame(job: dict, frame: int, job_id: str) -> dict:
    """渲染单帧,写 Volume _outputs/<job_id>/frames/frame_%05d.<ext>,返回帧元数据。
    bpy 状态是进程级全局的 —— Modal function 默认一容器一 input 串行,天然安全。"""
    import bpy
    if _SCENE["key"] != job_id:
        # 首次(或换 job):同步 Volume 拿最新上传的 .blend(此时无打开文件,reload 安全)
        models_vol.reload()
        if job.get("blend_path"):
            p = Path("/vol") / job["blend_path"]
            if not p.is_file():
                raise FileNotFoundError(
                    f".blend 不在 Volume 上: {job['blend_path']}(upload 成功了吗?)")
            bpy.ops.wm.open_mainfile(filepath=str(p))
        else:
            _build_demo_scene()
        _apply_overrides(job)
        _SCENE["warnings"] = _missing_files()
        _SCENE["device"] = _configure_cycles_gpu()
        _SCENE["key"] = job_id
    scene = bpy.context.scene
    scene.frame_set(frame)
    ext = "exr" if job["file_format"] == "OPEN_EXR" else "png"
    # 先渲到容器本地盘再拷 Volume:Blender 内部写文件不该踩 FUSE 的性能/部分写风险
    tmp_out = Path(f"/tmp/frame_{frame:05d}.{ext}")
    scene.render.filepath = str(tmp_out)
    scene.render.image_settings.file_format = job["file_format"]
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    out_dir = Path(f"/vol/_outputs/{job_id}/frames")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / tmp_out.name
    shutil.copyfile(tmp_out, final)
    tmp_out.unlink(missing_ok=True)
    models_vol.commit()   # 并发 commit 安全(各容器写各自的帧文件)
    return {"frame": frame, "path": f"_outputs/{job_id}/frames/{final.name}",
            "size": final.stat().st_size, "secs": round(time.time() - t0, 1),
            "warnings": _SCENE["warnings"], "device": _SCENE["device"]}
```

- [ ] **Step 2: 语法自检 + 单测不破**

Run: `python3 -c "import ast; ast.parse(open('modal_app/farm_app.py').read()); print('syntax ok')" && python3 -m pytest tests/ -q`
Expected: `syntax ok` + 全 PASS

- [ ] **Step 3: Commit**

```bash
git add modal_app/farm_app.py
git commit -m "feat: farm_app 镜像(bpy 5.2/py3.13)+ 单帧渲染(OPTIX 回退/容器缓存/demo 场景)"
```

---

### Task 3: farm_app.py(二)— coordinator(滑动窗口 + 进度 + 合成)

**Files:**
- Modify: `modal_app/farm_app.py`(文件末尾追加)

**Interfaces:**
- Consumes: `render_frame`(Task 2)、`farm_common.frames_list/ffmpeg_cmd`(Task 1)
- Produces:
  - `render_job(job: dict, job_id: str) -> dict`——job_state 状态机 queued→running→completed/failed;`progress:{step(=已完成帧数), total, s_it(平均秒/帧), elapsed}`;终态 `outputs:[{filename, volume_path, size_bytes}]`(video→render.mp4 / frames→frames.zip)+ `warnings` + `render_device`
  - job_state 新 key:`{job_id}:subcalls` = 当前 in-flight 帧渲染 call id 列表(滑动窗口,≤ 2×MAX_PARALLEL)

- [ ] **Step 1: farm_app.py 末尾追加 coordinator**

```python
# ============================================================================
# coordinator — 分帧调度 + 进度 + 合成/打包 + job_state 终态
# ============================================================================
@app.function(image=farm_image, volumes={"/vol": models_vol}, timeout=JOB_TIMEOUT)
def render_job(job: dict, job_id: str) -> dict:
    """滑动窗口 spawn 逐帧渲染:in-flight ≤ 2×MAX_PARALLEL。为什么不 starmap/全量 spawn:
      - cancel 需要逐个取消子 call(coordinator 被 cancel 后已 spawn 的 input 不会自动停),
        窗口把「要取消的 id 数」封顶在 ~20 个,cancel 端点的 RPC 循环才跑得完;
      - 未 spawn 的帧不产生任何排队/费用,任务中途失败即止损。"""
    from collections import OrderedDict, deque
    for _ in range(50):   # 等 run 端点把 :call 写进来(cancel 可用的前提),等不到也继续
        if job_state.get(f"{job_id}:call"):
            break
        time.sleep(0.1)
    frames = farm_common.frames_list(job)
    total = len(frames)
    t0 = time.time()
    job_state[job_id] = {**job_state.get(job_id, {}), "status": "running", "started_at": t0,
                         "progress": {"step": 0, "total": total, "elapsed": 0}}
    try:
        window = max(2, MAX_PARALLEL * 2)
        pending = deque(frames)
        inflight: OrderedDict = OrderedDict()   # FunctionCall -> frame(FIFO)
        done, warnings, device = 0, [], None

        def _fill():
            while pending and len(inflight) < window:
                f = pending.popleft()
                inflight[render_frame.spawn(job, f, job_id)] = f
            try:   # 滚动上报 in-flight 子 call,cancel 端点据此连带取消;写失败不碰任务
                job_state[f"{job_id}:subcalls"] = [c.object_id for c in inflight]
            except Exception:
                pass

        _fill()
        while inflight:
            call, _f = next(iter(inflight.items()))
            r = call.get()               # FIFO 等最老的;单帧异常 → 整个 job fail(进 except)
            inflight.pop(call)
            done += 1
            if r.get("warnings") and not warnings:
                warnings = r["warnings"]     # 同一场景警告全同,存一份
            device = r.get("device") or device
            elapsed = time.time() - t0
            try:
                job_state[job_id] = {**job_state.get(job_id, {}), "progress": {
                    "step": done, "total": total,
                    "s_it": round(elapsed / done, 1), "elapsed": int(elapsed)}}
            except Exception:
                pass
            _fill()

        # ── 合成 / 打包(帧此刻全在 Volume;先 reload 看到其它容器 commit 的文件)──
        models_vol.reload()
        out_root = Path(f"/vol/_outputs/{job_id}")
        frames_dir = out_root / "frames"
        if job["output"] == "video":
            tmp_video = Path(f"/tmp/{job_id}.mp4")   # 产物先落本地盘,成功再拷 Volume
            r = subprocess.run(farm_common.ffmpeg_cmd(str(frames_dir), str(tmp_video),
                                                      job["fps"]),
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg 合成失败: {(r.stderr or '')[-800:]}")
            shutil.copyfile(tmp_video, out_root / "render.mp4")
            shutil.rmtree(frames_dir, ignore_errors=True)   # 散帧已合成,不占 Volume
            outputs = [{"filename": "render.mp4",
                        "volume_path": f"_outputs/{job_id}/render.mp4",
                        "size_bytes": (out_root / "render.mp4").stat().st_size}]
        else:
            tmp_zip = shutil.make_archive(f"/tmp/{job_id}_frames", "zip", str(frames_dir))
            shutil.copyfile(tmp_zip, out_root / "frames.zip")
            shutil.rmtree(frames_dir, ignore_errors=True)
            outputs = [{"filename": "frames.zip",
                        "volume_path": f"_outputs/{job_id}/frames.zip",
                        "size_bytes": (out_root / "frames.zip").stat().st_size}]
        models_vol.commit()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        for cid in (job_state.get(f"{job_id}:subcalls") or []):   # 止损:停掉还在烧卡的帧
            try:
                modal.FunctionCall.from_id(cid).cancel()
            except Exception:
                pass
        job_state[job_id] = {**job_state.get(job_id, {}), "status": "failed",
                             "error": str(e), "trace": tb[-2000:],
                             "completed_at": time.time()}
        raise
    done_state = {**job_state.get(job_id, {}), "status": "completed", "outputs": outputs,
                  "render_device": device, "completed_at": time.time()}
    if warnings:
        done_state["warnings"] = warnings   # 缺贴图等断链:渲完了,但用户必须看见
    job_state[job_id] = done_state
    return {"frames": total, "outputs": len(outputs)}
```

- [ ] **Step 2: 语法自检 + 单测不破**

Run: `python3 -c "import ast; ast.parse(open('modal_app/farm_app.py').read()); print('syntax ok')" && python3 -m pytest tests/ -q`
Expected: `syntax ok` + 全 PASS

- [ ] **Step 3: Commit**

```bash
git add modal_app/farm_app.py
git commit -m "feat: coordinator 滑动窗口分帧调度 + 进度 + ffmpeg 合成/zip 打包"
```

---

### Task 4: farm_app.py(三)— 6 端点 + 鉴权

**Files:**
- Modify: `modal_app/farm_app.py`(文件末尾追加)

**Interfaces:**
- Consumes: Task 1-3 全部
- Produces(addon/farm_deploy 依赖的 HTTP 协议):
  - `POST /upload?key=&name=`(body=raw bytes)→ `{"blend_path": "scenes/<sha1[:8]>_<name>"}`
  - `POST /run` body `{auth_key, task_type?, blend_path?, render:{…}, job_id?}` → `{"id", "status": "queued", "gpu"}`
  - `GET /status?job_id=&key=` → job_state 条目(running 带 progress;completed 带 outputs)
  - `POST /cancel` body `{auth_key, job_id}` → `{"id", "status": "cancelled", "was_running"}`(取消失败带 error,绝不谎报)
  - `GET /fetch?job_id=&path=&key=&delete=` → 文件流(路径囚笼 `_outputs/<job_id>/`)
  - `GET /health?key=` → `{"healthy", "app", "gpu", "bpy", "version"}`

- [ ] **Step 1: farm_app.py 末尾追加端点**

```python
# ============================================================================
# 鉴权 — 自建 farm_key(私有端点)。部署时 farm_deploy.py 生成并写进 Secret,
# key 经 query(GET ?key=)/ body(POST auth_key)传入;拒绝时返 401。
# ============================================================================
DEPLOYED_VERSION = os.environ.get("FARM_VERSION", "unknown")


def _check(key: str):
    expected = os.environ.get("FARM_API_KEY", "")
    if expected and key == expected:
        return None
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "unauthorized — bad or missing farm key"}, status_code=401)


@app.function(image=farm_image, volumes={"/vol": models_vol}, secrets=[farm_secret],
              timeout=1800)   # 大 .blend 慢速上行也够
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-upload")
async def upload_endpoint(request):
    """流式收 .blend 写 Volume scenes/<sha1[:8]>_<name>。query: key, name。
    内容寻址命名:同内容同名秒过(文件已存在直接返回,不重写)。"""
    import hashlib

    from fastapi.responses import JSONResponse
    deny = _check(request.query_params.get("key", ""))
    if deny:
        return deny
    name = farm_common.safe_scene_name(request.query_params.get("name", ""))
    if not name.endswith(".blend"):
        return JSONResponse({"error": "只收 .blend 文件"}, status_code=400)
    sha, size = hashlib.sha1(), 0
    tmp = Path(f"/tmp/upload_{uuid.uuid4().hex}.blend")
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            sha.update(chunk)
            size += len(chunk)
    if size < 128:   # .blend 头都不止这么大,基本是空/坏请求
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": f"上传内容过小({size}B),不是有效 .blend"}, status_code=400)
    blend_path = f"scenes/{sha.hexdigest()[:8]}_{name}"
    dest = Path("/vol") / blend_path
    models_vol.reload()
    if not dest.is_file():          # 同内容重复上传:秒过
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), dest)
        models_vol.commit()
    else:
        tmp.unlink(missing_ok=True)
    return {"blend_path": blend_path, "size_bytes": size}


@app.function(image=farm_image, secrets=[farm_secret], timeout=60)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-run")
def run_endpoint(payload: dict):
    """提交渲染 job。payload: {auth_key, task_type?, blend_path?, render:{…}, job_id?}。
    blend_path 省略 = 内置 demo 场景(冒烟)。"""
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    job, err = farm_common.normalize_job(payload)
    if err:
        return {"error": err}
    job_id = payload.get("job_id") or str(uuid.uuid4())
    _sweep_job_state()
    job_state[job_id] = {"status": "queued", "queued_at": time.time(),
                         "gpu": FARM_GPU, "task_type": job["task_type"]}
    call = render_job.spawn(job, job_id)
    # call_id 存独立 key:job_state[job_id] 同时被 worker 写(running/completed),Modal Dict
    # 跨容器最终一致,merge 回写会撞 stale 覆盖 —— 分 key 各写各的,无竞态。
    job_state[f"{job_id}:call"] = call.object_id
    return {"id": job_id, "status": "queued", "gpu": FARM_GPU}


@app.function(image=farm_image, secrets=[farm_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-status")
def status_endpoint(job_id: str, key: str = ""):
    deny = _check(key)
    if deny:
        return deny
    s = job_state.get(job_id)
    if not s:
        return {"error": "job not found", "id": job_id}
    return {"id": job_id, **s}


@app.function(image=farm_image, secrets=[farm_secret], timeout=60)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-cancel")
def cancel_endpoint(payload: dict):
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    job_id = payload.get("job_id")
    if not job_id:
        return {"error": "Missing 'job_id'"}
    s = job_state.get(job_id) or {}
    was_running = s.get("status") == "running"
    call_id = job_state.get(f"{job_id}:call")
    if call_id:
        try:
            modal.FunctionCall.from_id(call_id).cancel()
        except Exception as e:
            # 取消失败绝不能谎报成功:云端还在跑、还在计费,必须让调用方看见
            print(f"[farm] cancel call {call_id} FAILED: {e}")
            return {"id": job_id, "status": s.get("status") or "unknown",
                    "error": f"cancel failed: {e}", "was_running": was_running}
    # coordinator 被杀后已 spawn 的帧不会自动停 —— 连带取消(滑动窗口保证 ≤ ~20 个)
    for cid in (job_state.get(f"{job_id}:subcalls") or []):
        try:
            modal.FunctionCall.from_id(cid).cancel()
        except Exception as e:
            print(f"[farm] cancel subcall {cid} failed: {e}")
    job_state[job_id] = {**s, "status": "cancelled", "completed_at": time.time()}
    return {"id": job_id, "status": "cancelled", "was_running": was_running}


@app.function(image=farm_image, volumes={"/vol": models_vol}, secrets=[farm_secret],
              timeout=600)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-fetch")
def fetch_endpoint(job_id: str, path: str, key: str = "", delete: int = 0):
    """流式取回产物。path 必须在 _outputs/<job_id>/ 内(囚笼,拒绝逃逸);
    delete=1 → 发送完成后删文件并 commit(默认删:产物取回即清,Volume 不堆积)。"""
    deny = _check(key)
    if deny:
        return deny
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.background import BackgroundTask
    prefix = f"_outputs/{job_id}/"
    if not path.startswith(prefix) or ".." in path or path != os.path.normpath(path):
        return JSONResponse({"error": "path out of job scope"}, status_code=403)
    models_vol.reload()   # worker 完成时 commit 过;reload 确保本容器看得到
    local = Path("/vol") / path
    if not local.is_file():
        return JSONResponse({"error": f"not found: {path}"}, status_code=404)
    cleanup = None
    if delete:
        def _cleanup(p=str(local)):
            try:
                os.remove(p)
                models_vol.commit()
            except Exception as e:
                print(f"[farm] fetch cleanup {p} failed: {e}")
        cleanup = BackgroundTask(_cleanup)
    return FileResponse(str(local), filename=Path(path).name, background=cleanup)


@app.function(image=farm_image, secrets=[farm_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-health")
def health_endpoint(key: str = ""):
    deny = _check(key)
    if deny:
        return deny
    return {"healthy": True, "app": APP_NAME, "gpu": FARM_GPU,
            "bpy": BPY_VERSION, "version": DEPLOYED_VERSION}
```

- [ ] **Step 2: 语法自检 + 单测不破**

Run: `python3 -c "import ast; ast.parse(open('modal_app/farm_app.py').read()); print('syntax ok')" && python3 -m pytest tests/ -q`
Expected: `syntax ok` + 全 PASS

- [ ] **Step 3: Commit**

```bash
git add modal_app/farm_app.py
git commit -m "feat: 6 端点(upload 流式收 blend/run/status/cancel 连带子任务/fetch 囚笼/health)+ farm_key 鉴权"
```

---

### Task 5: farm_deploy.py — 一键部署 + 云端 demo 冒烟

**Files:**
- Create: `farm_deploy.py`
- Create: `smoke_test.py`

**Interfaces:**
- Consumes: `modal_app/farm_app.py`(Task 2-4)
- Produces: `farm_config.json`(仓库根,.gitignore 已排除):`{"endpoint": "https://<ws>--blender-bridge", "farm_key": "fk-…"}`——addon 配置从这里抄

- [ ] **Step 1: 实现 farm_deploy.py**

```python
#!/usr/bin/env python3
"""
farm_deploy.py — 容器内一键部署:建/更新 Secret(farm_key)→ modal deploy → 存 endpoint。

前置:modal SDK 已装且已鉴权(env MODAL_TOKEN_ID/MODAL_TOKEN_SECRET 或 ~/.modal.toml)。
FARM_* 部署参数经本脚本注入 env(裸跑 modal deploy 会全部丢失、回退默认值)。
用法:python3 farm_deploy.py [--gpu L40S] [--max-parallel 10] [--frame-timeout 1800] [--job-timeout 14400]
"""
import argparse
import json
import os
import re
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = HERE / "farm_config.json"
APP_NAME = "blender-bridge"
VERSION = "0.1.0"


def load_cfg() -> dict:
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gpu", default="L40S")
    ap.add_argument("--max-parallel", type=int, default=10)
    ap.add_argument("--frame-timeout", type=int, default=1800)
    ap.add_argument("--job-timeout", type=int, default=14400)
    args = ap.parse_args()

    cfg = load_cfg()
    farm_key = cfg.get("farm_key") or ("fk-" + pysecrets.token_urlsafe(24))  # 复用旧 key,重部署不换锁

    env = {**os.environ,
           "FARM_GPU": args.gpu,
           "FARM_MAX_PARALLEL": str(args.max_parallel),
           "FARM_FRAME_TIMEOUT": str(args.frame_timeout),
           "FARM_JOB_TIMEOUT": str(args.job_timeout),
           "FARM_VERSION": VERSION}

    print(f"[1/2] 建/更新 Secret({APP_NAME}-secrets)…")
    r = subprocess.run([sys.executable, "-m", "modal", "secret", "create", "--force",
                        f"{APP_NAME}-secrets", f"FARM_API_KEY={farm_key}"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"secret 创建失败:{r.stderr[-500:]}\n(modal token 配好了吗?)")

    print(f"[2/2] modal deploy(gpu={args.gpu},首次要构建镜像,分钟级)…")
    proc = subprocess.Popen([sys.executable, "-m", "modal", "deploy", "modal_app/farm_app.py"],
                            cwd=str(HERE / "modal_app"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in proc.stdout:
        print("  " + line.rstrip(), flush=True)
        tail.append(line)
    if proc.wait() != 0:
        sys.exit("deploy 失败(日志见上)")

    # 从输出解析 endpoint base:https://<ws>--blender-bridge-run.modal.run → https://<ws>--blender-bridge
    m = re.search(rf"https://[\w\-]+--{re.escape(APP_NAME)}-\w+\.modal\.run", "".join(tail))
    if not m:
        sys.exit(f"✓ 部署完成,但没解析到 endpoint —— 手动填 farm_config.json(key: {farm_key})")
    base = re.sub(r"-(upload|run|status|cancel|fetch|health)\.modal\.run$", "", m.group(0))
    CFG.write_text(json.dumps({"endpoint": base, "farm_key": farm_key}, indent=2))
    try:
        CFG.chmod(0o600)
    except Exception:
        pass
    print(f"\n✓ 部署完成。endpoint + farm_key 已写 {CFG}")
    print(f"  endpoint: {base}")
    print(f"  farm_key: {farm_key}")
    print("  → Blender addon preferences 里填这两项")


if __name__ == "__main__":
    main()
```

⚠ `cwd=modal_app/` 是刻意的:`add_local_python_source("farm_app", "farm_common")` 按部署时模块名解析,deploy 必须在该目录跑。注意 deploy 命令里的路径要相应改为 `farm_app.py`——实现时统一成 `cwd=str(HERE / "modal_app")` + `modal deploy farm_app.py`。

- [ ] **Step 2: 实现 smoke_test.py(demo 冒烟,不依赖 Blender)**

```python
#!/usr/bin/env python3
"""
smoke_test.py — 部署后全链路冒烟(纯 stdlib):demo 场景 8 帧 → 轮询进度 → 下载 mp4。
用法:python3 smoke_test.py [--frames 1-8] [--output video] [--cancel-after N(秒,测取消)]
读 farm_config.json 的 endpoint/farm_key。
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "modal_app"))
from farm_common import parse_frame_spec  # noqa: E402

cfg = json.loads((HERE / "farm_config.json").read_text())
BASE, KEY = cfg["endpoint"], cfg["farm_key"]


def post(label, body):
    req = urllib.request.Request(f"{BASE}-{label}.modal.run",
                                 data=json.dumps({**body, "auth_key": KEY}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(label, **params):
    import urllib.parse
    qs = urllib.parse.urlencode({**params, "key": KEY})
    with urllib.request.urlopen(f"{BASE}-{label}.modal.run?{qs}", timeout=30) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="1-8")
    ap.add_argument("--output", default="video", choices=["video", "frames"])
    ap.add_argument("--format", default="PNG", choices=["PNG", "OPEN_EXR"])
    ap.add_argument("--cancel-after", type=int, default=0)
    args = ap.parse_args()
    start, end, step = parse_frame_spec(args.frames)

    print("health:", get("health"))
    d = post("run", {"render": {"frame_start": start, "frame_end": end, "frame_step": step,
                                "output": args.output, "file_format": args.format}})
    if "id" not in d:
        sys.exit(f"提交失败: {d}")
    job_id = d["id"]
    print(f"job {job_id}  gpu={d.get('gpu')}")
    t0 = time.time()
    while True:
        time.sleep(3)
        s = get("status", job_id=job_id)
        p = s.get("progress") or {}
        print(f"  [{s.get('status')}] {p.get('step', 0)}/{p.get('total', '?')} 帧"
              f" · {p.get('s_it', '?')}s/帧 · 已 {int(time.time() - t0)}s", flush=True)
        if args.cancel_after and time.time() - t0 > args.cancel_after:
            print("cancel:", post("cancel", {"job_id": job_id}))
            return
        if s.get("status") in ("completed", "failed", "cancelled"):
            break
    if s.get("status") != "completed":
        sys.exit(f"未成功: {s.get('status')} — {s.get('error', '')[:400]}")
    if s.get("warnings"):
        print(f"⚠ warnings: {s['warnings']}")
    print(f"render_device: {s.get('render_device')}(期望 OPTIX)")
    out_dir = Path("/tmp/farm_smoke") / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    import urllib.parse
    for o in s.get("outputs") or []:
        qs = urllib.parse.urlencode({"job_id": job_id, "path": o["volume_path"],
                                     "key": KEY, "delete": 1})
        dest = out_dir / o["filename"]
        with urllib.request.urlopen(f"{BASE}-fetch.modal.run?{qs}", timeout=600) as r, \
                open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        print(f"✓ {dest}  ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 部署**

Run: `cd /workspace/documents/blender_modal_bridge && MODAL_TOKEN_ID=… MODAL_TOKEN_SECRET=… python3 farm_deploy.py`(token 从现有 modal 配置取;若 shell 已有 ~/.modal.toml 则不用 env)
Expected: 镜像 build(bpy ~300MB 下载)→ deploy 成功 → 打印 endpoint/farm_key,`farm_config.json` 生成

- [ ] **Step 4: demo 冒烟(video)**

Run: `python3 smoke_test.py --frames 1-8`
Expected: 进度逐帧走到 8/8;`render_device: OPTIX`;`/tmp/farm_smoke/<job_id>/render.mp4` 落盘可播(金属立方体旋转)

- [ ] **Step 5: demo 冒烟(frames/EXR + cancel)**

Run: `python3 smoke_test.py --frames 1-4 --output frames --format OPEN_EXR`
Expected: `frames.zip` 落盘,解压出 4 个 .exr
Run: `python3 smoke_test.py --frames 1-48 --cancel-after 20`
Expected: cancel 返回 `status: cancelled`;Modal dashboard 上 1 分钟内无 running 的 render_frame 残留

- [ ] **Step 6: Commit**

```bash
git add farm_deploy.py smoke_test.py
git commit -m "feat: 一键部署脚本 + 全链路冒烟脚本(demo/EXR/cancel);云端实测通过"
```

---

### Task 6: addon(一)— client.py + preferences + 注册骨架

**Files:**
- Create: `addon/blender_modal_bridge/__init__.py`
- Create: `addon/blender_modal_bridge/client.py`

**Interfaces:**
- Produces:
  - `client.FarmClient(endpoint_base, key)`:`health()` / `upload(filepath, name) -> dict` / `run(render: dict, blend_path) -> dict` / `status(job_id)` / `cancel(job_id)` / `fetch(job_id, volume_path, dest_path) -> int`;网络错误抛 `FarmError`
  - `__init__.py`:`bl_info`、`FarmPreferences`(endpoint/farm_key/output_dir/auto_download/jobs_json)、`register()/unregister()`(ui/ops/jobs 的注册在 Task 7/8 补上,本任务先空挂)
- 约束:纯 stdlib(urllib/json/ssl),**不在 UI 线程调用**(调用方保证)

- [ ] **Step 1: 实现 client.py**

```python
"""client.py — 云端 HTTP 客户端(纯 stdlib;所有方法阻塞,只允许在后台线程调用)。"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FarmError(RuntimeError):
    pass


class FarmClient:
    def __init__(self, endpoint_base: str, key: str, timeout: int = 60):
        """endpoint_base 形如 https://<workspace>--blender-bridge(farm_deploy 打印的)。"""
        if not endpoint_base or "--" not in endpoint_base:
            raise FarmError("endpoint 形如 https://<workspace>--blender-bridge")
        self.base = endpoint_base.rstrip("/")
        self.key = key or ""
        self.timeout = timeout

    def _url(self, label: str) -> str:
        return f"{self.base}-{label}.modal.run"

    def _get(self, label: str, timeout: int | None = None, **params) -> dict:
        qs = urllib.parse.urlencode({**params, "key": self.key})
        return self._req(f"{self._url(label)}?{qs}", None, timeout)

    def _post(self, label: str, body: dict, timeout: int | None = None) -> dict:
        return self._req(self._url(label), {**body, "auth_key": self.key}, timeout)

    def _req(self, url: str, body: dict | None, timeout: int | None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FarmError("401 — farm_key 不对/缺失") from None
            try:
                return json.loads(e.read().decode())
            except Exception:
                raise FarmError(f"HTTP {e.code}: {url}") from None
        except Exception as e:
            raise FarmError(f"请求失败: {e}") from e

    # ── 协议 ──
    def health(self) -> dict:
        return self._get("health", timeout=15)

    def upload(self, filepath: str, name: str) -> dict:
        """流式上传 .blend,返回 {blend_path, size_bytes}。大文件给长超时。"""
        p = Path(filepath)
        size = p.stat().st_size
        qs = urllib.parse.urlencode({"key": self.key, "name": name})
        req = urllib.request.Request(
            f"{self._url('upload')}?{qs}", data=open(p, "rb"), method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(size)})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read().decode())
            except Exception:
                raise FarmError(f"upload HTTP {e.code}") from None
        except Exception as e:
            raise FarmError(f"upload 失败: {e}") from e
        if "blend_path" not in d:
            raise FarmError(f"upload 响应异常: {d.get('error') or d}")
        return d

    def run(self, render: dict, blend_path: str | None) -> dict:
        body = {"task_type": "render", "render": render}
        if blend_path:
            body["blend_path"] = blend_path
        d = self._post("run", body)
        if "id" not in d:
            raise FarmError(f"run 失败: {d.get('error') or d}")
        return d

    def status(self, job_id: str) -> dict:
        return self._get("status", job_id=job_id, timeout=20)

    def cancel(self, job_id: str) -> dict:
        """⚠ 返回带 error 表示取消失败、云端仍在计费 —— 调用方必须透出。"""
        return self._post("cancel", {"job_id": job_id}, timeout=30)

    def fetch(self, job_id: str, volume_path: str, dest_path: str,
              delete_remote: bool = True) -> int:
        qs = urllib.parse.urlencode({"job_id": job_id, "path": volume_path,
                                     "key": self.key, "delete": int(delete_remote)})
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(f"{self._url('fetch')}?{qs}", timeout=600) as r, \
                    open(dest, "wb") as f:
                size = 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    size += len(chunk)
                return size
        except urllib.error.HTTPError as e:
            raise FarmError(f"fetch HTTP {e.code}({volume_path})") from None
```

- [ ] **Step 2: 实现 __init__.py(骨架)**

```python
"""Blender Modal Bridge — 云端渲染农场(Modal serverless GPU)。
N 面板(3D 视图 → Farm 页签)提交当前文件,进度可视,产物自动取回。"""
bl_info = {
    "name": "Blender Modal Bridge (Render Farm)",
    "author": "liqi",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Farm",
    "description": "Submit Cycles renders to Modal serverless GPUs (L40S/OptiX)",
    "category": "Render",
}

import bpy

from . import jobs, ops, ui


class FarmPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    endpoint: bpy.props.StringProperty(
        name="Endpoint", description="https://<workspace>--blender-bridge(farm_deploy 打印的)")
    farm_key: bpy.props.StringProperty(
        name="Farm Key", subtype="PASSWORD", description="fk-…(farm_deploy 打印的)")
    output_dir: bpy.props.StringProperty(
        name="Output Dir", subtype="DIR_PATH", default="//render_farm/",
        description="产物下载目录(默认 .blend 旁边 render_farm/)")
    auto_download: bpy.props.BoolProperty(
        name="Auto Download", default=True, description="job 完成后自动下载产物")
    jobs_json: bpy.props.StringProperty(default="[]", options={"HIDDEN"})  # job 列表持久化

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "endpoint")
        col.prop(self, "farm_key")
        col.prop(self, "output_dir")
        col.prop(self, "auto_download")


_CLASSES = (FarmPreferences,)


def register():
    for c in _CLASSES:
        bpy.utils.register_class(c)
    jobs.register()
    ops.register()
    ui.register()


def unregister():
    ui.unregister()
    ops.unregister()
    jobs.unregister()
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
```

(本任务 jobs/ops/ui 还不存在——先建三个空模块占位:各含空的 `register()/unregister()`,Task 7/8 填实。)

- [ ] **Step 3: 语法自检(容器无 bpy,只查语法)**

Run: `python3 -c "import ast; [ast.parse(open(f).read()) for f in ('addon/blender_modal_bridge/__init__.py','addon/blender_modal_bridge/client.py','addon/blender_modal_bridge/jobs.py','addon/blender_modal_bridge/ops.py','addon/blender_modal_bridge/ui.py')]; print('syntax ok')"`
Expected: `syntax ok`

- [ ] **Step 4: Commit**

```bash
git add addon/
git commit -m "feat(addon): HTTP 客户端 + preferences + 注册骨架"
```

---

### Task 7: addon(二)— jobs.py(状态/轮询)+ ops.py(提交/取消/下载)

**Files:**
- Modify: `addon/blender_modal_bridge/jobs.py`
- Modify: `addon/blender_modal_bridge/ops.py`

**Interfaces:**
- Consumes: `client.FarmClient`(Task 6)、`farm_common.parse_frame_spec` 逻辑(addon 内联同款解析,不跨仓 import——addon 要能独立拷走)
- Produces:
  - `jobs.FarmJobItem`(CollectionProperty item:job_id/label/status/step/total/s_it/elapsed/error/warnings/out_dir/downloaded)挂 `WindowManager.farm_jobs` + `farm_jobs_index`
  - `jobs.ensure_timer()/push_result(fn)/persist()/restore()`——线程安全的「后台线程 → 主线程」通道 + preferences 持久化
  - `ops.FARM_OT_test_connection / FARM_OT_submit / FARM_OT_cancel / FARM_OT_download`

- [ ] **Step 1: 实现 jobs.py**

```python
"""jobs.py — job 列表状态 + 「后台线程 → 主线程」通道 + 轮询 timer。
线程规则:网络全在 daemon 线程;线程产出的 UI 变更打包成闭包丢 _RESULTS 队列,
bpy.app.timers 在主线程消费(bpy 数据只在主线程碰)。"""
import json
import queue
import threading

import bpy

_RESULTS: queue.Queue = queue.Queue()   # 闭包队列:主线程 timer 逐个执行
_POLLING = set()                        # 正在轮询中的 job_id(防重复起线程)
_TIMER_ON = False


class FarmJobItem(bpy.types.PropertyGroup):
    job_id: bpy.props.StringProperty()
    label: bpy.props.StringProperty()          # 显示名(文件名 + 帧范围)
    status: bpy.props.StringProperty(default="queued")
    step: bpy.props.IntProperty(default=0)
    total: bpy.props.IntProperty(default=0)
    s_it: bpy.props.FloatProperty(default=0.0)
    elapsed: bpy.props.IntProperty(default=0)
    error: bpy.props.StringProperty()
    warnings: bpy.props.StringProperty()       # 断链清单(分号拼接)
    out_dir: bpy.props.StringProperty()        # 下载目录(绝对路径)
    downloaded: bpy.props.BoolProperty(default=False)
    outputs_json: bpy.props.StringProperty(default="[]")   # 终态 outputs(下载用)


def prefs():
    return bpy.context.preferences.addons[__package__].preferences


def get_client():
    from .client import FarmClient
    p = prefs()
    return FarmClient(p.endpoint, p.farm_key)


def find(job_id: str):
    for it in bpy.context.window_manager.farm_jobs:
        if it.job_id == job_id:
            return it
    return None


def push_result(fn):
    """后台线程调用:把「主线程要做的事」入队。"""
    _RESULTS.put(fn)
    ensure_timer()


def _tick():
    global _TIMER_ON
    # 1) 消费线程结果(闭包在主线程执行,可安全碰 bpy 数据)
    while not _RESULTS.empty():
        try:
            _RESULTS.get_nowait()()
        except Exception as e:
            print(f"[farm] result 处理失败: {e}")
    # 2) 对活跃 job 起轮询线程(在跑的不重复起)
    active = [it.job_id for it in bpy.context.window_manager.farm_jobs
              if it.status in ("queued", "running", "uploading", "downloading")
              and it.status != "uploading"]   # uploading 由提交线程自己推进
    for jid in active:
        if jid not in _POLLING and not jid.startswith("local-"):
            _POLLING.add(jid)
            threading.Thread(target=_poll_once, args=(jid,), daemon=True).start()
    # 3) UI 重绘 + 决定 timer 去留
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    has_active = any(it.status in ("queued", "running", "uploading", "downloading")
                     for it in bpy.context.window_manager.farm_jobs)
    if not has_active and _RESULTS.empty():
        _TIMER_ON = False
        return None      # 停表(下次 push_result/提交会重启)
    return 2.0


def ensure_timer():
    global _TIMER_ON
    if not _TIMER_ON:
        _TIMER_ON = True
        bpy.app.timers.register(_tick, first_interval=0.5)


def _poll_once(job_id: str):
    """后台线程:查一次状态,结果闭包回主线程。"""
    try:
        s = get_client().status(job_id)
    except Exception as e:
        s = {"_poll_error": str(e)}
    finally:
        _POLLING.discard(job_id)

    def apply():
        it = find(job_id)
        if it is None:
            return
        if s.get("_poll_error"):
            return   # 网络抖动:保持现状,下个 tick 再试
        it.status = s.get("status") or it.status
        p = s.get("progress") or {}
        if p:
            it.step, it.total = p.get("step", it.step), p.get("total", it.total)
            it.s_it, it.elapsed = p.get("s_it", it.s_it), p.get("elapsed", it.elapsed)
        if s.get("error"):
            it.error = str(s["error"])[:400]
        if s.get("warnings"):
            it.warnings = "; ".join(s["warnings"])[:800]
        if it.status == "completed" and not it.downloaded:
            it.outputs_json = json.dumps(s.get("outputs") or [])
            persist()
            if prefs().auto_download:
                from . import ops
                ops.start_download(it)
        elif it.status in ("failed", "cancelled"):
            persist()
    push_result(apply)


# ── 持久化(重启 Blender 后 job 列表不丢)──
def persist():
    data = [{"job_id": it.job_id, "label": it.label, "status": it.status,
             "out_dir": it.out_dir, "downloaded": it.downloaded,
             "outputs_json": it.outputs_json, "error": it.error}
            for it in bpy.context.window_manager.farm_jobs]
    prefs().jobs_json = json.dumps(data)


def restore():
    wm = bpy.context.window_manager
    if len(wm.farm_jobs):
        return
    try:
        data = json.loads(prefs().jobs_json or "[]")
    except Exception:
        data = []
    for d in data[-20:]:                     # 最多恢复最近 20 条
        it = wm.farm_jobs.add()
        for k, v in d.items():
            setattr(it, k, v)
    if any(it.status in ("queued", "running") for it in wm.farm_jobs):
        ensure_timer()                       # 有未完 job → 恢复轮询


def register():
    bpy.utils.register_class(FarmJobItem)
    bpy.types.WindowManager.farm_jobs = bpy.props.CollectionProperty(type=FarmJobItem)
    bpy.types.WindowManager.farm_jobs_index = bpy.props.IntProperty(default=0)
    bpy.app.timers.register(restore, first_interval=1.0)   # 启动后恢复列表


def unregister():
    del bpy.types.WindowManager.farm_jobs_index
    del bpy.types.WindowManager.farm_jobs
    bpy.utils.unregister_class(FarmJobItem)
```

- [ ] **Step 2: 实现 ops.py**

```python
"""ops.py — operators:测试连接 / 提交 / 取消 / 下载。
提交流程(全在后台线程,UI 不阻塞):pack 副本 → 本地预检 → upload → run → 转轮询。"""
import os
import tempfile
import threading
from pathlib import Path

import bpy

from . import jobs


def _scene_props(context):
    """从 Scene 自定义属性读提交参数(ui.py 注册;fps 尊重场景)。"""
    sc = context.scene
    mode = sc.farm_frame_mode
    if mode == "CURRENT":
        start = end = sc.frame_current
        step = 1
    elif mode == "SCENE":
        start, end, step = sc.frame_start, sc.frame_end, sc.frame_step
    else:   # CUSTOM
        start, end, step = sc.farm_frame_start, sc.farm_frame_end, sc.farm_frame_step
    fps = round(sc.render.fps / sc.render.fps_base)
    return {"frame_start": start, "frame_end": end, "frame_step": step,
            "output": sc.farm_output, "file_format": sc.farm_file_format, "fps": fps}


def _precheck_missing() -> list[str]:
    """本地预检:pack 不进去的外部资产(比云端更早暴露)。链接库必警告——不会上云。"""
    missing = []
    for img in bpy.data.images:
        if img.source == "FILE" and img.filepath and not img.packed_file:
            if not Path(bpy.path.abspath(img.filepath)).exists():
                missing.append(f"image: {img.filepath}")
    for lib in bpy.data.libraries:
        missing.append(f"library(不会打包,云端必缺): {lib.filepath}")
    return missing


def _pack_and_save_copy() -> tuple[str, list[str]]:
    """pack_all + save copy 到临时文件(不动工作文件);新 pack 的资产随后 unpack 还原,
    用户会话状态保持提交前的样子。返回 (临时路径, 预检警告)。必须在主线程调用。"""
    packed_before = {img.name for img in bpy.data.images if img.packed_file}
    try:
        bpy.ops.file.pack_all()
    except Exception as e:
        print(f"[farm] pack_all 部分失败(继续,断链走警告): {e}")
    warnings = _precheck_missing()
    tmp = Path(tempfile.gettempdir()) / f"farm_submit_{os.getpid()}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(tmp), copy=True, compress=True)
    for img in bpy.data.images:      # 还原:只 unpack 这次新 pack 的
        if img.packed_file and img.name not in packed_before:
            try:
                img.unpack(method="REMOVE")
            except Exception:
                pass
    return str(tmp), warnings


class FARM_OT_test_connection(bpy.types.Operator):
    bl_idname = "farm.test_connection"
    bl_label = "Test Connection"
    bl_description = "检查 endpoint/farm_key 是否可用(/health)"

    def execute(self, context):
        def work():
            try:
                h = jobs.get_client().health()
                msg, ok = f"✓ {h.get('app')} gpu={h.get('gpu')} bpy={h.get('bpy')}", True
            except Exception as e:
                msg, ok = f"✗ {e}", False

            def apply():
                # report 只能在 operator 生命周期内用,这里用弹窗替代
                def draw(self_, _ctx):
                    self_.layout.label(text=msg)
                bpy.context.window_manager.popup_menu(
                    draw, title="Farm Connection", icon="CHECKMARK" if ok else "ERROR")
            jobs.push_result(apply)
        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


class FARM_OT_submit(bpy.types.Operator):
    bl_idname = "farm.submit"
    bl_label = "Submit to Farm"
    bl_description = "pack 当前文件副本并提交云端渲染(不改动工作文件)"

    def execute(self, context):
        p = jobs.prefs()
        if not p.endpoint or not p.farm_key:
            self.report({"ERROR"}, "先在 addon preferences 填 endpoint 和 farm_key")
            return {"CANCELLED"}
        render = _scene_props(context)
        if render["output"] == "video" and render["file_format"] != "PNG":
            self.report({"ERROR"}, "video 输出只支持 PNG;EXR 请切 output=frames")
            return {"CANCELLED"}
        # 主线程:pack + save copy(bpy.ops 必须主线程)
        tmp_path, warnings = _pack_and_save_copy()
        name = Path(bpy.data.filepath or "untitled.blend").name
        # 下载目录:// 相对当前 .blend 解析为绝对
        out_root = bpy.path.abspath(p.output_dir or "//render_farm/")

        wm = context.window_manager
        it = wm.farm_jobs.add()
        it.job_id = f"local-{os.urandom(4).hex()}"   # 提交成功后换成云端 id
        it.label = f"{name}  {render['frame_start']}-{render['frame_end']}"
        it.status = "uploading"
        if warnings:
            it.warnings = "; ".join(warnings)[:800]
        wm.farm_jobs_index = len(wm.farm_jobs) - 1
        local_key = it.job_id
        jobs.ensure_timer()

        def work():
            try:
                c = jobs.get_client()
                up = c.upload(tmp_path, name)
                d = c.run(render, up["blend_path"])
            except Exception as e:
                def fail():
                    it2 = jobs.find(local_key)
                    if it2:
                        it2.status, it2.error = "failed", str(e)[:400]
                        jobs.persist()
                jobs.push_result(fail)
                return
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

            def ok():
                it2 = jobs.find(local_key)
                if it2:
                    it2.job_id, it2.status = d["id"], "queued"
                    it2.out_dir = str(Path(out_root) / d["id"])
                    jobs.persist()
            jobs.push_result(ok)
        threading.Thread(target=work, daemon=True).start()
        self.report({"INFO"}, "已提交上传(后台进行,见 Farm 面板)")
        return {"FINISHED"}


class FARM_OT_cancel(bpy.types.Operator):
    bl_idname = "farm.cancel"
    bl_label = "Cancel"
    bl_description = "取消云端任务(含 in-flight 的帧渲染)"

    job_id: bpy.props.StringProperty()

    def execute(self, context):
        jid = self.job_id

        def work():
            try:
                r = jobs.get_client().cancel(jid)
            except Exception as e:
                r = {"error": str(e)}

            def apply():
                it = jobs.find(jid)
                if it is None:
                    return
                if r.get("error"):
                    it.error = f"取消失败(云端仍在计费!): {r['error']}"[:400]
                else:
                    it.status = "cancelled"
                jobs.persist()
            jobs.push_result(apply)
        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


def start_download(it):
    """下载 job 产物到 it.out_dir(jobs 轮询完成时自动调,或 Download 按钮手动调)。
    必须主线程调用(读 item 属性),网络在线程。"""
    import json as _json
    jid, out_dir = it.job_id, it.out_dir
    outputs = _json.loads(it.outputs_json or "[]")
    if not outputs:
        return
    it.status = "downloading"
    jobs.ensure_timer()

    def work():
        err = None
        try:
            c = jobs.get_client()
            for o in outputs:
                c.fetch(jid, o["volume_path"], str(Path(out_dir) / o["filename"]))
        except Exception as e:
            err = str(e)[:400]

        def apply():
            it2 = jobs.find(jid)
            if it2 is None:
                return
            if err:
                it2.status, it2.error = "completed", f"下载失败(可重试): {err}"
            else:
                it2.status, it2.downloaded = "completed", True
                it2.error = ""
            jobs.persist()
        jobs.push_result(apply)
    threading.Thread(target=work, daemon=True).start()


class FARM_OT_download(bpy.types.Operator):
    bl_idname = "farm.download"
    bl_label = "Download"
    bl_description = "下载该任务的产物到输出目录"

    job_id: bpy.props.StringProperty()

    def execute(self, context):
        it = jobs.find(self.job_id)
        if it is None:
            return {"CANCELLED"}
        start_download(it)
        return {"FINISHED"}


class FARM_OT_clear_finished(bpy.types.Operator):
    bl_idname = "farm.clear_finished"
    bl_label = "Clear Finished"
    bl_description = "清掉列表里的终态任务"

    def execute(self, context):
        wm = context.window_manager
        for i in range(len(wm.farm_jobs) - 1, -1, -1):
            if wm.farm_jobs[i].status in ("completed", "failed", "cancelled"):
                wm.farm_jobs.remove(i)
        jobs.persist()
        return {"FINISHED"}


_CLASSES = (FARM_OT_test_connection, FARM_OT_submit, FARM_OT_cancel,
            FARM_OT_download, FARM_OT_clear_finished)


def register():
    for c in _CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
```

- [ ] **Step 3: 语法自检**

Run: `python3 -c "import ast; [ast.parse(open(f).read()) for f in ('addon/blender_modal_bridge/jobs.py','addon/blender_modal_bridge/ops.py')]; print('syntax ok')"`
Expected: `syntax ok`

- [ ] **Step 4: Commit**

```bash
git add addon/
git commit -m "feat(addon): job 状态/轮询 timer/线程通道 + 提交(pack 副本+预检)/取消/下载 operators"
```

---

### Task 8: addon(三)— ui.py N 面板 + Mac 装载 + 真机集成验证

**Files:**
- Modify: `addon/blender_modal_bridge/ui.py`

**Interfaces:**
- Consumes: Task 6/7 全部
- Produces: Scene 属性(`farm_frame_mode/farm_frame_start/end/step/farm_output/farm_file_format`)+ `FARM_PT_panel`(3D 视图 N 面板 "Farm" 页签)

- [ ] **Step 1: 实现 ui.py**

```python
"""ui.py — N 面板(3D 视图 → Sidebar → Farm)+ Scene 提交参数属性。"""
import bpy

from . import jobs


def _scene_props():
    S = bpy.types.Scene
    S.farm_frame_mode = bpy.props.EnumProperty(
        name="Frames", default="SCENE",
        items=[("SCENE", "Scene Range", "用场景的 frame_start/end/step"),
               ("CURRENT", "Current Frame", "只渲当前帧(look dev 快查)"),
               ("CUSTOM", "Custom", "自定义范围")])
    S.farm_frame_start = bpy.props.IntProperty(name="Start", default=1, min=0)
    S.farm_frame_end = bpy.props.IntProperty(name="End", default=48, min=0)
    S.farm_frame_step = bpy.props.IntProperty(name="Step", default=1, min=1)
    S.farm_output = bpy.props.EnumProperty(
        name="Output", default="video",
        items=[("video", "Video (mp4)", "帧渲完 ffmpeg 合成 mp4"),
               ("frames", "Frames (zip)", "帧序列打 zip(EXR 用这个)")])
    S.farm_file_format = bpy.props.EnumProperty(
        name="Format", default="PNG",
        items=[("PNG", "PNG", ""), ("OPEN_EXR", "OpenEXR", "需 Output=Frames")])


def _del_scene_props():
    S = bpy.types.Scene
    for k in ("farm_frame_mode", "farm_frame_start", "farm_frame_end",
              "farm_frame_step", "farm_output", "farm_file_format"):
        delattr(S, k)


_STATUS_ICON = {"uploading": "EXPORT", "queued": "SORTTIME", "running": "RENDER_ANIMATION",
                "downloading": "IMPORT", "completed": "CHECKMARK",
                "failed": "ERROR", "cancelled": "X"}


class FARM_PT_panel(bpy.types.Panel):
    bl_label = "Modal Bridge Farm"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Farm"

    def draw(self, context):
        lay = self.layout
        p = jobs.prefs()
        sc = context.scene

        if not p.endpoint or not p.farm_key:
            box = lay.box()
            box.label(text="未配置", icon="ERROR")
            box.label(text="Preferences → Add-ons → Blender Modal Bridge")
            box.label(text="填 Endpoint 和 Farm Key(farm_deploy 打印的)")
            return

        col = lay.column(align=True)
        col.prop(sc, "farm_frame_mode")
        if sc.farm_frame_mode == "CUSTOM":
            row = col.row(align=True)
            row.prop(sc, "farm_frame_start")
            row.prop(sc, "farm_frame_end")
            row.prop(sc, "farm_frame_step")
        row = col.row(align=True)
        row.prop(sc, "farm_output", expand=True)
        col.prop(sc, "farm_file_format")
        if sc.render.engine != "CYCLES":
            col.label(text=f"引擎是 {sc.render.engine},农场只支持 Cycles", icon="ERROR")

        lay.operator("farm.submit", icon="CLOUD")
        row = lay.row(align=True)
        row.operator("farm.test_connection", icon="PLUGIN")
        row.operator("farm.clear_finished", icon="TRASH")

        wm = context.window_manager
        if not len(wm.farm_jobs):
            return
        box = lay.box()
        for it in reversed(list(wm.farm_jobs)[-8:]):     # 最近 8 条,新的在上
            row = box.row(align=True)
            row.label(text=it.label or it.job_id[:8],
                      icon=_STATUS_ICON.get(it.status, "QUESTION"))
            if it.status == "running" and it.total:
                row.label(text=f"{it.step}/{it.total}  {it.s_it:.0f}s/帧")
                row.operator("farm.cancel", text="", icon="X").job_id = it.job_id
            elif it.status in ("queued", "uploading", "downloading"):
                row.label(text=it.status)
                if it.status != "uploading":
                    row.operator("farm.cancel", text="", icon="X").job_id = it.job_id
            elif it.status == "completed":
                if it.downloaded:
                    row.label(text="已下载")
                else:
                    row.operator("farm.download", text="", icon="IMPORT").job_id = it.job_id
            elif it.status == "failed":
                row.label(text=(it.error or "failed")[:40])
            if it.warnings:
                sub = box.row()
                sub.label(text=f"⚠ {it.warnings[:70]}", icon="LIBRARY_DATA_BROKEN")


_CLASSES = (FARM_PT_panel,)


def register():
    _scene_props()
    for c in _CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
    _del_scene_props()
```

- [ ] **Step 2: 语法自检 + 全量单测**

Run: `python3 -c "import ast; ast.parse(open('addon/blender_modal_bridge/ui.py').read()); print('syntax ok')" && python3 -m pytest tests/ -q`
Expected: `syntax ok` + 全 PASS

- [ ] **Step 3: Mac Blender 装载(经宿主机 Blender MCP,不用手点)**

用 `mcp__blender__execute_blender_code` 在 Mac Blender 里执行:

```python
import bpy, sys
p = "/Users/bytedance/Documents/blender_modal_bridge/addon"   # 挂载目录的 Mac 侧路径
if p not in sys.path:
    sys.path.append(p)
import addon_utils
bpy.ops.preferences.addon_refresh()
# 直接以模块方式启用(开发期;正式装走 zip)
import blender_modal_bridge
blender_modal_bridge.register()
prefs = bpy.context.preferences.addons.get("blender_modal_bridge")
print("registered:", prefs is not None)
```

再把 `farm_config.json` 的 endpoint/farm_key 写进 preferences(同样经 MCP execute)。
Expected: N 面板出现 Farm 页签;Test Connection 弹 ✓

- [ ] **Step 4: 真机集成验证(依次经 Blender MCP 驱动或用户手点)**

1. demo 等价验证:开一个简单 Cycles 场景(默认 cube 即可),Frames=Current Frame,提交 → 面板 uploading→queued→running→completed→自动下载,`//render_farm/<job_id>/render.mp4`(单帧 mp4)或改 frames 出 zip
2. 真实场景:打开用户的工作 .blend(如 3dgs_swan),Scene Range 渲 4 帧提交;检查 warnings 是否如实反映断链;结果落盘可看
3. cancel:提交 48 帧,running 时点 X → 状态转 cancelled;Modal dashboard 无 running 残留
4. 重启 Blender → 面板恢复历史 job 列表(persist/restore 生效)

- [ ] **Step 5: 收尾**

- README.md(仓库根):一段话 + 部署三步 + addon 安装两步 + 面板截图位(后补)
- 实测数据(demo 单帧耗时/s_it/费用量级、OPTIX 确认、上传速率)写进 memory(`blender-worker-design.md` 状态改为 MVP 已上线 + 新增性能基线记忆)
- Commit + 与用户确认是否建远端仓库

```bash
git add addon/ README.md
git commit -m "feat(addon): N 面板 UI;Mac Blender 5.2 真机集成验证通过"
```

---

## Self-Review 结论(写计划时已跑)

- **Spec 覆盖**:6 端点 ✓ 滑动窗口/cancel 连带 ✓ OPTIX 回退 ✓ 容器缓存 ✓ demo 冒烟 ✓ pack 副本+还原 ✓ 本地预检(链接库必警告) ✓ 线程/timer 异步 ✓ 持久化恢复 ✓ 自动下载 ✓ task_type 预留 ✓
- **类型一致性**:`normalize_job` 的 job 键 ↔ `render_frame/_apply_overrides` 读的键 ↔ addon `_scene_props` 构造的 render 键逐一核对;`outputs[].volume_path` 生产(coordinator)与消费(smoke_test/addon fetch)一致;`FarmClient.run(render, blend_path)` 与 run 端点 payload 对齐
- **已知风险与对策**:①upload 流式收大文件(`request.stream()`)在 Modal fastapi_endpoint 的实际表现要 Task 5 冒烟实测(百 MB 级;不行则降级为一次性读 body,GB 级留分块二期);②`pack_all→save copy→unpack(REMOVE)` 的还原路径 Task 8 真机验证(若 REMOVE 有副作用,退路:不还原、提示用户"会话已 pack,不保存即不影响磁盘文件");③coordinator 被 cancel 后子 call 不自动停 → 窗口 + `:subcalls` 双保险;④bpy 写 FUSE → 一律 /tmp 落地再 copy;⑤OPTIX 枚举失败 → 逐级回退且 `render_device` 可核对
