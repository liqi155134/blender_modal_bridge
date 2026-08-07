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

# fastapi 只在容器镜像里有(fastapi[standard]);部署解析侧(本地 python)没有 → 兜底 None。
# upload_endpoint 的参数用字符串注解 "Request":容器运行时 FastAPI 用 get_type_hints
# 解析到这里的真类才会注入 Request 对象 —— ⚠ 没有注解会被当 query 参数,端点直接 422
# (2026-08-08 实锤:大文件上传表现为 Broken pipe,小请求才看得到 422 真身)。
try:
    from fastapi import Request
except ModuleNotFoundError:
    Request = None  # type: ignore[assignment,misc]

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
        "FARM_APP_NAME", "FARM_VOLUME", "FARM_SECRET", "FARM_GPU", "FARM_VERSION",
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
        if img.users - int(img.use_fake_user) <= 0:
            continue   # 孤儿数据块(无引用):与渲染无关,别报着吓人
        if img.source == "FILE" and img.filepath and not img.packed_file:
            if not Path(bpy.path.abspath(img.filepath)).exists():
                missing.append(f"image: {img.filepath}")
    for lib in bpy.data.libraries:
        if not lib.users_id:
            continue   # 空壳库引用(链接块已全删):不参与渲染,不报
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
            print(f"[farm] compute_device={dtype}: "
                  + ", ".join(d.name for d in prefs.devices if d.use))
            _fix_denoiser(scene)
            return dtype
    print("[farm] ⚠ 未枚举到 GPU 设备,回退 CPU 渲染(慢)")
    scene.cycles.device = "CPU"
    _fix_denoiser(scene)
    return "CPU"


def _fix_denoiser(scene):
    """OptiX denoiser 在 Modal 容器实测创建失败(2026-08-07 冒烟:渲染 device 走 OPTIX
    正常,但 "Failed to create OptiX denoiser" 直接炸掉整帧)——场景若设了 OPTIX
    denoiser,统一降级 OIDN(质量不输,GPU 版 4.1+ 可用)。denoise 开关本身尊重场景。"""
    if getattr(scene.cycles, "use_denoising", False) and \
            getattr(scene.cycles, "denoiser", "") == "OPTIX":
        scene.cycles.denoiser = "OPENIMAGEDENOISE"
        print("[farm] denoiser OPTIX → OPENIMAGEDENOISE(容器内 OptiX denoiser 不可用)")


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
    ext0 = "exr" if job["file_format"] == "OPEN_EXR" else "png"
    done = Path(f"/vol/_outputs/{job_id}/frames/frame_{frame:05d}.{ext0}")
    if done.is_file() and done.stat().st_size > 0:
        # 幂等:该帧已渲过(job 重跑/失败重试场景),跳过 —— Flamenco use_overwrite=False
        # 思想在本架构的翻译。Volume 视图旧只会导致重复渲(无害),不会误跳。
        return {"frame": frame, "path": f"_outputs/{job_id}/frames/{done.name}",
                "size": done.stat().st_size, "secs": 0.0, "skipped": True,
                "warnings": _SCENE["warnings"], "device": _SCENE["device"]}
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
        cur = job_state.get(job_id) or {}
        if cur.get("status") == "cancelled":
            # cancel 端点已写终态;本异常只是取消传播(subcall 被 cancel → get() 抛
            # "Function call was cancelled…")——别用 failed + SDK 吓人文案覆盖用户的取消
            raise
        job_state[job_id] = {**cur, "status": "failed",
                             "error": str(e), "trace": tb[-2000:],
                             "completed_at": time.time()}
        raise
    done_state = {**job_state.get(job_id, {}), "status": "completed", "outputs": outputs,
                  "render_device": device, "completed_at": time.time()}
    if warnings:
        done_state["warnings"] = warnings   # 缺贴图等断链:渲完了,但用户必须看见
    job_state[job_id] = done_state
    return {"frames": total, "outputs": len(outputs)}


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


def _finalize_scene_file(tmp: Path, name: str, size: int) -> dict:
    """临时文件 → 算 sha → 落 Volume scenes/<sha1[:8]>_<name>(内容寻址,已存在秒过)。"""
    import hashlib
    sha = hashlib.sha1()
    with open(tmp, "rb") as f:
        while chunk := f.read(1 << 22):
            sha.update(chunk)
    blend_path = f"scenes/{sha.hexdigest()[:8]}_{name}"
    dest = Path("/vol") / blend_path
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), dest)
        models_vol.commit()
    else:
        tmp.unlink(missing_ok=True)
    return {"blend_path": blend_path, "size_bytes": size}


def _sweep_stale_uploads():
    """清 24h 前的断头分块目录(上传中断的残留)。best-effort。"""
    try:
        updir = Path("/vol/uploads")
        if not updir.is_dir():
            return
        now = time.time()
        for d in updir.iterdir():
            if d.is_dir() and now - d.stat().st_mtime > 86400:
                shutil.rmtree(d, ignore_errors=True)
        models_vol.commit()
    except Exception:
        pass


@app.function(image=farm_image, volumes={"/vol": models_vol}, secrets=[farm_secret],
              timeout=1800)   # 大块慢速上行也够
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-upload")
async def upload_endpoint(request: "Request"):
    """收 .blend 写 Volume scenes/<sha1[:8]>_<name>(内容寻址,同内容秒过)。
    两种模式(query 区分):
      单发:?key&name                              —— body=整文件(⚠ Modal 单请求体实测
           150MB OK / 700MB 被 303 拒,大文件必须走分块)
      分块:?key&name&upload_id&index&total        —— 块暂存 Volume uploads/<id>/
           (端点多请求不保证同容器,容器本地 /tmp 会丢块,必须经 Volume 中转);
           客户端串行发块,收到末块时 reload 数块拼接落盘。"""
    from fastapi.responses import JSONResponse
    q = request.query_params
    deny = _check(q.get("key", ""))
    if deny:
        return deny
    name = farm_common.safe_scene_name(q.get("name", ""))
    if not name.endswith(".blend"):
        return JSONResponse({"error": "只收 .blend 文件"}, status_code=400)

    upload_id = q.get("upload_id", "")
    if not upload_id:
        # ── 单发模式 ──
        size = 0
        tmp = Path(f"/tmp/upload_{uuid.uuid4().hex}.blend")
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
        if size < 128:   # .blend 头都不止这么大,基本是空/坏请求
            tmp.unlink(missing_ok=True)
            return JSONResponse({"error": f"上传内容过小({size}B)"}, status_code=400)
        models_vol.reload()
        return _finalize_scene_file(tmp, name, size)

    # ── 分块模式 ──
    if not (len(upload_id) == 32 and all(c in "0123456789abcdef" for c in upload_id)):
        return JSONResponse({"error": "upload_id 必须是 32 位 hex(uuid4().hex)"}, status_code=400)
    try:
        index, total = int(q.get("index", -1)), int(q.get("total", 0))
    except ValueError:
        return JSONResponse({"error": "index/total 必须是整数"}, status_code=400)
    if not (0 <= index < total <= 512):
        return JSONResponse({"error": f"index/total 非法({index}/{total})"}, status_code=400)
    updir = Path("/vol/uploads") / upload_id
    updir.mkdir(parents=True, exist_ok=True)
    part = updir / f"c_{index:05d}"
    with open(part, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    models_vol.commit()
    if index < total - 1:
        return {"ok": True, "received": index + 1, "total": total}
    # 末块:客户端串行保证前面的块都已 commit → reload 后数块拼接
    models_vol.reload()
    parts = sorted(updir.glob("c_*"))
    if len(parts) != total:
        return JSONResponse({"error": f"分块不齐:{len(parts)}/{total}(重传整个文件)"},
                            status_code=400)
    tmp = Path(f"/tmp/merge_{upload_id}.blend")
    size = 0
    with open(tmp, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                while chunk := f.read(1 << 22):
                    out.write(chunk)
                    size += len(chunk)
    result = _finalize_scene_file(tmp, name, size)
    shutil.rmtree(updir, ignore_errors=True)
    _sweep_stale_uploads()
    models_vol.commit()
    return result


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
