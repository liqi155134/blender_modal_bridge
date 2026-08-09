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
  4. job 协议 task_type 化(farm_common.normalize_job):render / bake 共用统一
     gpu_unit 全局并行护栏,upload/status/cancel/fetch/进度骨架不动。
"""
import json
import os
import re
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
PROTOCOL_VERSION = 2
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
        "FARM_FRAME_TIMEOUT", "FARM_JOB_TIMEOUT", "FARM_MAX_PARALLEL",
        "FARM_JOB_TTL", "FARM_ARTIFACT_TTL", "FARM_SCENE_TTL", "FARM_JOB_MAX",
    ) if os.environ.get(k)})
    .add_local_python_source("farm_app", "farm_common")
)


# ============================================================================
# job_state 清理:终态条目超 TTL 删,数量兜底(照搬 comfyui_modal_bridge 验证过的策略)
# ============================================================================
JOB_TTL_S = int(os.environ.get("FARM_JOB_TTL", "3600"))
JOB_ARTIFACT_TTL_S = int(os.environ.get("FARM_ARTIFACT_TTL", str(30 * 86400)))
SCENE_TTL_S = int(os.environ.get("FARM_SCENE_TTL", str(30 * 86400)))
JOB_MAX = int(os.environ.get("FARM_JOB_MAX", "200"))


def _sweep_job_state():
    """best-effort 清理过期/超量的终态 job。任何异常都不影响主流程。"""
    try:
        now = time.time()
        items = list(job_state.items())
    except Exception:
        return
    terminal = {"completed", "failed", "cancelled"}

    def _artifacts_pending(state):
        # 旧版 state 没有该字段;只要还有 outputs 就按未取回处理,升级不丢入口。
        return bool(state.get("artifacts_pending", bool(state.get("outputs"))))

    finished = [(jid, s) for jid, s in items
                if isinstance(s, dict) and s.get("status") in terminal]

    def _drop(jid):
        try:
            state = job_state.get(jid) or {}
            request_id = state.get("request_id") if isinstance(state, dict) else None
            if request_id:
                del job_state[f"request:{request_id}"]
        except Exception:
            pass
        for k in (jid, f"{jid}:call", f"{jid}:subcalls", f"{jid}:cancel",
                  f"{jid}:filling"):  # :filling 兼容清理旧部署残值
            try:
                del job_state[k]
            except Exception:
                pass
        # launch gate 正常由 worker 自删;coordinator/worker 同时硬杀时由 TTL sweep 兜底。
        try:
            for k, _v in list(job_state.items()):
                if isinstance(k, str) and k.startswith(f"{jid}:launch:"):
                    del job_state[k]
        except Exception:
            pass
        try:
            shutil.rmtree(Path(f"/vol/_outputs/{jid}"), ignore_errors=True)
            models_vol.commit()
        except Exception:
            pass

    for jid, state in finished:
        pending = _artifacts_pending(state)
        anchor = (state.get("completed_at") if pending
                  else state.get("fetched_at") or state.get("completed_at")) or 0
        ttl = JOB_ARTIFACT_TTL_S if pending else JOB_TTL_S
        if anchor and now - anchor > ttl:
            _drop(jid)
    try:
        # 数量护栏只清已取回的终态;不为了 Dict 上限吞掉未下载产物入口。
        remaining = [(j, s.get("fetched_at") or s.get("completed_at") or 0)
                     for j, s in job_state.items()
                     if (isinstance(s, dict) and s.get("status") in terminal
                         and not _artifacts_pending(s))]
        if len(remaining) > JOB_MAX:
            remaining.sort(key=lambda x: x[1])
            for jid, _ in remaining[: len(remaining) - JOB_MAX]:
                _drop(jid)
    except Exception:
        pass


# ============================================================================
# 场景装配(全部只在 gpu_unit 进程内调用;bpy 只在函数内 import —— 本文件
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


def _activate_job_scene(job: dict):
    """显式激活提交时选中的 Scene;多 Scene 文件不依赖保存 UI 的隐式上下文。"""
    import bpy
    name = job.get("scene_name")
    scene = bpy.data.scenes.get(name) if name else bpy.context.scene
    if scene is None:
        available = [s.name for s in bpy.data.scenes][:30]
        raise ValueError(f".blend 里找不到 Scene {name!r}(可用: {available})")
    layer_name = job.get("view_layer_name")
    layer = scene.view_layers.get(layer_name) if layer_name else scene.view_layers[0]
    if layer is None:
        available = [item.name for item in scene.view_layers]
        raise ValueError(
            f"Scene {scene.name!r} 里找不到 View Layer {layer_name!r}(可用: {available})")
    window = bpy.context.window
    if window is not None:
        window.scene = scene
        if layer_name:
            window.view_layer = layer
    # bpy wheel 无窗口时不能写 window.scene;Render 显式传 scene/layer，Bake
    # 使用 temp_override(scene/view_layer)，因此这里只需严格校验名称。
    return scene


def _job_view_layer(scene, job: dict):
    name = job.get("view_layer_name")
    return scene.view_layers.get(name) if name else scene.view_layers[0]


def _apply_overrides(job: dict, scene):
    """payload 显式给的才覆盖;没给的尊重 .blend 场景设置(艺术家的文件是真源)。"""
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
        obj = scene.objects.get(cam)
        if obj is None or obj.type != "CAMERA":
            cams = [o.name for o in scene.objects if o.type == "CAMERA"]
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
    for cache in getattr(bpy.data, "cache_files", ()):
        path = getattr(cache, "filepath", "")
        if (getattr(cache, "users", 0) and path
                and not Path(bpy.path.abspath(path)).exists()):
            missing.append(f"cache file: {path}")
    simulation_types = {"CLOTH", "FLUID", "SOFT_BODY", "DYNAMIC_PAINT"}
    for obj in bpy.data.objects:
        sims = sorted({mod.type for mod in obj.modifiers if mod.type in simulation_types})
        if getattr(obj, "particle_systems", None):
            sims.append("PARTICLES")
        if sims:
            missing.append(
                f"simulation(分帧前应烘焙/转网格): {obj.name} [{', '.join(sims)}]")
    for scene in bpy.data.scenes:
        if getattr(scene, "rigidbody_world", None):
            missing.append(f"simulation(刚体缓存可能不可用): Scene {scene.name}")
    for lib in bpy.data.libraries:
        if not lib.users_id:
            continue   # 空壳库引用(链接块已全删):不参与渲染,不报
        if lib.filepath and not Path(bpy.path.abspath(lib.filepath)).exists():
            missing.append(f"library: {lib.filepath}")
    return missing[:20]   # 封顶,别撑爆 job_state


def _configure_cycles_gpu(scene) -> str:
    """OPTIX 优先(RT core 正解),枚举不到该类设备逐级回退 CUDA → CPU(宁可慢不可炸)。
    只支持 CYCLES:EEVEE 无头需要 GPU context/EGL,显式报错比默默换引擎出错片强。
    返回实际用的后端名(写进产物,用户能核对没白付 RT core 的钱)。"""
    import bpy
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


def _render_frame_impl(job: dict, frame: int, job_id: str, launch_token: str) -> dict:
    """渲染单帧,写 Volume _outputs/<job_id>/frames/frame_%05d.<ext>,返回帧元数据。
    bpy 状态是进程级全局的 —— Modal function 默认一容器一 input 串行,天然安全。"""
    _await_launch_gate(job_id, launch_token)
    _abort_if_cancelled(job_id)
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
        scene = _activate_job_scene(job)
        _apply_overrides(job, scene)
        _SCENE["warnings"] = _missing_files()
        _SCENE["device"] = _configure_cycles_gpu(scene)
        _SCENE["key"] = job_id
    scene = _activate_job_scene(job)
    scene.frame_set(frame)
    ext = "exr" if job["file_format"] == "OPEN_EXR" else "png"
    # 先渲到容器本地盘再拷 Volume:Blender 内部写文件不该踩 FUSE 的性能/部分写风险
    tmp_out = Path(f"/tmp/{job_id}_frame_{frame:05d}.{ext}")
    tmp_out.unlink(missing_ok=True)
    scene.render.filepath = str(tmp_out)
    scene.render.image_settings.file_format = job["file_format"]
    t0 = time.time()
    result = bpy.ops.render.render(
        write_still=True, scene=scene.name, layer=job.get("view_layer_name", ""))
    if "FINISHED" not in result or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
        raise RuntimeError(f"Blender render 未生成有效帧(result={result})")
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
# coordinator — 滑动窗口调度(render/bake 共用)+ 各自的产物打包 + job_state 终态
# ============================================================================
def _coordinator_call_id(record):
    """读取 v2 {id,token} call 记录;兼容旧部署留下的字符串值。"""
    return record.get("id") if isinstance(record, dict) else record


def _await_call_key(job_id: str, coordinator_token: str):
    """等 run 端点把与本 coordinator token 绑定的 :call 写进来。

    否则端点死在 spawn→写 call ID 的缝隙时,原 coordinator 会无法取消,
    request_id 恢复又会 spawn 第二份。token 防止恢复后的 call 记录误放行
    原先那个迟到的 coordinator。
    """
    for _ in range(50):
        try:
            record = job_state.get(f"{job_id}:call")
        except Exception:
            record = None
        if isinstance(record, dict):
            if record.get("token") == coordinator_token and record.get("id"):
                return record["id"]
            if record.get("token") and record.get("token") != coordinator_token:
                raise RuntimeError(f"job {job_id} coordinator 已被新恢复调用取代")
        time.sleep(0.1)
    raise RuntimeError(f"job {job_id} coordinator call id 未登记,拒绝启动子任务")


def _abort_if_cancelled(job_id: str):
    """worker 过 launch gate 后再自检一次取消旗,收窄 ready→Blender 的传播窗口。
    Dict 读失败不挡任务(可用性优先;误跑一个单元的代价远小于误杀一个 job)。"""
    try:
        flagged = bool(job_state.get(f"{job_id}:cancel"))
    except Exception:
        return
    if flagged:
        raise _JobCancelled(f"job {job_id} 已被用户取消")


class _JobCancelled(RuntimeError):
    """内部取消信号:必须走 cancelled 终态,不能被当成普通 worker failed。"""


WATCHDOG_GRACE_S = 600


def _stalled_reason(state: dict, now: float) -> str | None:
    """识别已超过 Modal coordinator 硬超时的僵尸状态。

    不用进度心跳做短阈值:多 job 抢占 GPU 时 input 可合法长时间排队。
    queued_at + JOB_TIMEOUT + grace 之后 coordinator 理论上必已终止,仍活跃才可安全纠正。
    """
    if not isinstance(state, dict) or state.get("status") not in ("queued", "running"):
        return None
    anchor = state.get("queued_at") or state.get("started_at")
    if not isinstance(anchor, (int, float)):
        return None
    if now - float(anchor) <= JOB_TIMEOUT + WATCHDOG_GRACE_S:
        return None
    return (f"coordinator 超过 {JOB_TIMEOUT}s 平台超时仍无终态;"
            "watchdog 已停止遗留子任务")


def _fail_stalled_job(job_id: str, reason: str):
    """status 轮询触发的 fail-safe watchdog,幂等且不覆盖终态。"""
    current = job_state.get(job_id) or {}
    if current.get("status") not in ("queued", "running"):
        return
    # 先落终态:即使后面某个 cancel RPC 卡住/端点超时,用户也不会
    # 继续看到永久 running。_fail_job/_complete_job 会保护此终态。
    job_state[job_id] = {**current, "status": "failed", "error": reason,
                         "completed_at": time.time()}
    for cid in (job_state.get(f"{job_id}:subcalls") or []):
        try:
            modal.FunctionCall.from_id(cid).cancel()
        except Exception:
            pass
    call_id = _coordinator_call_id(job_state.get(f"{job_id}:call"))
    if call_id:
        try:
            modal.FunctionCall.from_id(call_id).cancel()
        except Exception:
            pass


def _cancel_requested(job_id: str) -> bool:
    """Best-effort 读取取消旗;Dict 短暂不可用时不误杀正常任务。"""
    try:
        return bool(job_state.get(f"{job_id}:cancel"))
    except Exception:
        return False


LAUNCH_GATE_TIMEOUT_S = 30.0


def _launch_key(job_id: str, token: str) -> str:
    return f"{job_id}:launch:{token}"


def _await_launch_gate(job_id: str, token: str):
    """GPU worker 两阶段启动门。

    coordinator 先写 pending,spawn 后把 call id 严格写入 :subcalls,最后才写 ready。
    因此 coordinator 即使死在 spawn→快照之间,worker 也不会进入昂贵的 Blender 操作;
    cancel flag 会让它立即退出,无取消时 gate 超时也会 fail-closed。
    """
    key = _launch_key(job_id, token)
    deadline = time.monotonic() + LAUNCH_GATE_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            # 启动阶段没有可用性上的理由冒险放行:cancel 或 gate 任一
            # Dict 读失败就继续等,最终超时失败关闭。
            try:
                cancelled = bool(job_state.get(f"{job_id}:cancel"))
                state = job_state.get(key)
            except Exception:
                time.sleep(0.05)
                continue
            if cancelled:
                raise _JobCancelled(f"job {job_id} 已被用户取消(worker launch gate)")
            if state == "ready":
                return
            time.sleep(0.05)
        raise RuntimeError(f"job {job_id} worker launch gate 超时(call id 未完成注册)")
    finally:
        try:
            del job_state[key]
        except Exception:
            pass


def _poll_call(call, timeout):
    """轮询单个子 call → (是否已完成, 结果)。

    ⚠ 两种 timeout 语义完全不同,混淆任何一边都是事故:
      - **还没出结果**:modal 的 poll_function 抛的是 **内置** TimeoutError
        (modal/_functions.py 没 import 自己的 exception.TimeoutError)——正常轮询状态,
        必须继续等。2026-08-08 实锤:只 except modal.exception.TimeoutError 抓不到它
        (那个类继承 modal Error,与内置无继承关系),每个 job 提交几秒后必 failed。
      - **worker 自身跑超时**:FunctionTimeoutError —— 真故障,必须冒泡让 job 失败,
        绝不能被当成"还没好"无限等下去。
    """
    try:
        return True, call.get(timeout=timeout)
    except modal.exception.FunctionTimeoutError:
        raise
    except (TimeoutError, modal.exception.TimeoutError):
        return False, None


def _sliding_schedule(spawn_one, units: list, job_id: str, t0: float):
    """滑动窗口 spawn 并行单元:in-flight ≤ 2×MAX_PARALLEL。为什么不 starmap/全量 spawn:
      - cancel 需要逐个取消子 call(coordinator 被 cancel 后已 spawn 的 input 不会自动停),
        窗口把「要取消的 id 数」封顶在 ~20 个,cancel 端点的 RPC 循环才跑得完;
      - 未 spawn 的单元不产生任何排队/费用,任务中途失败即止损。
    spawn_one(unit, launch_token) -> FunctionCall。返回 (results, warnings, device)。
    进度按完成单元数写 job_state.progress(render=帧,bake=对象×pass)。"""
    from collections import OrderedDict, deque
    window = max(2, MAX_PARALLEL * 2)
    pending = deque(units)
    inflight: OrderedDict = OrderedDict()   # FunctionCall -> unit(稳定快照顺序;收割非 FIFO)
    total = len(units)
    done, warnings, device = 0, [], None
    results = []

    def _publish_subcalls(strict=False):
        try:
            job_state[f"{job_id}:subcalls"] = [c.object_id for c in inflight]
            return True
        except Exception as e:
            if strict:
                raise RuntimeError(f"subcall 快照写入失败,拒绝放行 GPU worker: {e}") from e
            return False

    def _fill():
        """填充窗口;call id 注册成功后才 release 对应 worker 的 launch gate。"""
        cancelled = False
        try:
            while pending and len(inflight) < window:
                if _cancel_requested(job_id):
                    pending.clear()
                    cancelled = True
                    break
                u = pending.popleft()
                token = uuid.uuid4().hex
                key = _launch_key(job_id, token)
                try:
                    job_state[key] = "pending"
                except Exception as e:
                    raise RuntimeError(f"worker launch gate 创建失败,未 spawn: {e}") from e
                call = None
                try:
                    call = spawn_one(u, token)
                    inflight[call] = u
                    _publish_subcalls(strict=True)
                    # 快照成功后才放行。ready 写失败则主动 cancel,worker 最终 gate 超时。
                    job_state[key] = "ready"
                except Exception:
                    if call is not None:
                        try:
                            call.cancel()
                        except Exception:
                            pass
                    raise
            # flag 可能在最后一次 spawn 与循环退出之间到达,这里再收一次。
            if _cancel_requested(job_id):
                pending.clear()
                cancelled = True
        finally:
            _publish_subcalls()
        if cancelled:
            raise _JobCancelled(f"job {job_id} 已被用户取消(停止调度新单元)")

    def _next_finished():
        """收割任意已完成 call,避免 FIFO 队头慢帧让窗口空位无法及时补充。"""
        while True:
            if _cancel_requested(job_id):
                raise _JobCancelled(f"job {job_id} 已被用户取消(等待子任务)")
            calls = list(inflight)
            for call in calls:
                ok, r = _poll_call(call, 0)
                if ok:
                    return call, r
            # 全部未完成时只对一个 call 最多阻塞 0.5s,然后重新扫全窗口。
            ok, r = _poll_call(calls[0], 0.5)
            if ok:
                return calls[0], r

    _fill()
    while inflight:
        call, r = _next_finished()   # 单元异常 → 整个 job fail(调用方 except)
        inflight.pop(call)
        results.append(r)
        done += 1
        if r.get("warnings") and not warnings:
            warnings = r["warnings"]     # 同一场景警告全同,存一份
        device = r.get("device") or device
        elapsed = time.time() - t0
        try:
            job_state[job_id] = {**job_state.get(job_id, {}), "progress": {
                "step": done, "total": total,
                "s_it": round(elapsed / done, 1), "elapsed": int(elapsed)},
                "heartbeat_at": time.time()}
        except Exception:
            pass
        _fill()
    return results, warnings, device


def _fail_job(job_id: str, e: Exception):
    """coordinator 失败收尾:止损取消子 call + 写 failed 终态(cancelled 不覆盖)。"""
    import traceback
    tb = traceback.format_exc()
    for cid in (job_state.get(f"{job_id}:subcalls") or []):   # 止损:停掉还在烧卡的单元
        try:
            modal.FunctionCall.from_id(cid).cancel()
        except Exception:
            pass
    cur = job_state.get(job_id) or {}
    if cur.get("status") in ("completed", "failed", "cancelled"):
        return
    if cur.get("status") == "cancelled" or _cancel_requested(job_id):
        # cancel 端点已写终态;本异常只是取消传播(subcall 被 cancel → get() 抛
        # "Function call was cancelled…")——别用 failed + SDK 吓人文案覆盖用户的取消
        if cur.get("status") != "cancelled":
            job_state[job_id] = {**cur, "status": "cancelled",
                                 "completed_at": time.time()}
        return
    job_state[job_id] = {**cur, "status": "failed",
                         "error": str(e), "trace": tb[-2000:],
                         "completed_at": time.time()}


def _complete_job(job_id: str, outputs: list, warnings: list, device):
    cur = job_state.get(job_id, {})
    if cur.get("status") in ("completed", "failed"):
        return False
    if cur.get("status") == "cancelled" or _cancel_requested(job_id):
        job_state[job_id] = {**cur, "status": "cancelled",
                             "completed_at": cur.get("completed_at") or time.time()}
        return False
    warnings = list(warnings or [])
    if device == "CPU":
        warnings.append("⚠ Cycles 未枚举到 OPTIX/CUDA,实际回退 CPU。"
                        "产物正确但 GPU 加速未生效,请检查部署镜像/驱动")
    done_state = {**cur, "status": "completed", "outputs": outputs,
                  "artifacts_pending": bool(outputs),
                  "render_device": device, "completed_at": time.time()}
    if warnings:
        done_state["warnings"] = warnings   # 缺贴图等断链:跑完了,但用户必须看见
    job_state[job_id] = done_state
    return True


@app.function(image=farm_image, volumes={"/vol": models_vol}, timeout=JOB_TIMEOUT)
def render_job(job: dict, job_id: str, coordinator_token: str) -> dict:
    """渲染 coordinator:逐帧并行 → ffmpeg 合成 mp4 / zip 帧序列。"""
    _await_call_key(job_id, coordinator_token)
    frames = farm_common.frames_list(job)
    t0 = time.time()
    job_state[job_id] = {**job_state.get(job_id, {}), "status": "running", "started_at": t0,
                         "progress": {"step": 0, "total": len(frames), "elapsed": 0}}
    try:
        _results, warnings, device = _sliding_schedule(
            lambda f, token: gpu_unit.spawn(job, f, job_id, token),
            frames, job_id, t0)
        _abort_if_cancelled(job_id)
        # ── 合成 / 打包(帧此刻全在 Volume;先 reload 看到其它容器 commit 的文件)──
        models_vol.reload()
        out_root = Path(f"/vol/_outputs/{job_id}")
        frames_dir = out_root / "frames"
        if job["output"] == "video":
            tmp_video = Path(f"/tmp/{job_id}.mp4")   # 产物先落本地盘,成功再拷 Volume
            r = subprocess.run(farm_common.ffmpeg_cmd(
                str(frames_dir), str(tmp_video), job["fps_num"], job["fps_den"]),
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
        _abort_if_cancelled(job_id)
    except Exception as e:
        _fail_job(job_id, e)
        raise
    _complete_job(job_id, outputs, warnings, device)
    return {"frames": len(frames), "outputs": len(outputs)}


# ============================================================================
# bake — 单对象×单 pass 的 GPU 单元 + coordinator
# ============================================================================
def _bake_unit_impl(job: dict, obj_name: str, pass_type: str, job_id: str,
                    launch_token: str) -> dict:
    """烘焙单个 (对象, pass),写 Volume _outputs/<job_id>/textures/<obj>_<pass>.<ext>。
    场景缓存 key=job_id 同渲染;bake 与渲染共用 Cycles PT 内核(同吃 OPTIX/RT core)。"""
    _await_launch_gate(job_id, launch_token)
    _abort_if_cancelled(job_id)
    ext = "exr" if job["file_format"] == "OPEN_EXR" else "png"
    fname = f"{farm_common.bake_output_stem(obj_name)}_{pass_type}.{ext}"
    final = Path(f"/vol/_outputs/{job_id}/textures/{fname}")
    if final.is_file() and final.stat().st_size > 0:   # 幂等:重跑不重烘
        return {"unit": f"{obj_name}:{pass_type}", "path": str(final), "skipped": True,
                "size": final.stat().st_size, "secs": 0.0,
                "warnings": _SCENE["warnings"], "device": _SCENE["device"]}
    import bpy
    if _SCENE["key"] != job_id:
        models_vol.reload()
        if job.get("blend_path"):
            p = Path("/vol") / job["blend_path"]
            if not p.is_file():
                raise FileNotFoundError(f".blend 不在 Volume 上: {job['blend_path']}")
            bpy.ops.wm.open_mainfile(filepath=str(p))
        else:
            _build_demo_scene()
        scene = _activate_job_scene(job)
        if job.get("samples"):
            scene.cycles.samples = job["samples"]
        _SCENE["warnings"] = _missing_files()
        _SCENE["device"] = _configure_cycles_gpu(scene)
        # 场景加载即捕获 bake 分量基线(供非 DIFFUSE pass 恢复,场景是真源)
        _SCENE["bake_direct"] = scene.render.bake.use_pass_direct
        _SCENE["bake_indirect"] = scene.render.bake.use_pass_indirect
        _SCENE["key"] = job_id
    scene = _activate_job_scene(job)
    view_layer = _job_view_layer(scene, job)
    obj = scene.objects.get(obj_name)
    if obj is None or obj.type != "MESH":
        meshes = [o.name for o in scene.objects if o.type == "MESH"][:20]
        raise ValueError(f"找不到网格对象 {obj_name!r}(场景里的网格: {meshes})")
    if view_layer.objects.get(obj_name) is None:
        raise ValueError(f"对象 {obj_name!r} 被提交的 View Layer 排除,无法 Bake")
    if not obj.data.uv_layers:
        raise ValueError(f"{obj_name!r} 没有 UV —— bake 需要已展好的 UV(场景是真源)")

    # 目标 image:每个材质槽都要挂 active image node —— 冒烟实锤:空槽的面被静默跳过
    # Blender ID 名会截断;若把长对象名放前面,不同 pass/对象可能命中同一 Image。
    # job + 输出 stem 的 hash 固定短名,同时避免碰到用户文件里的旧临时数据块。
    obj_hash = farm_common.bake_output_stem(obj_name).rsplit("--", 1)[-1]
    img_name = f"farm_bake_{job_id[:8]}_{obj_hash}_{pass_type}"
    img = bpy.data.images.get(img_name)
    if img is None:
        img = bpy.data.images.new(img_name, job["resolution"], job["resolution"],
                                  float_buffer=(job["file_format"] == "OPEN_EXR"))
    if pass_type in ("NORMAL", "ROUGHNESS", "AO"):
        img.colorspace_settings.name = "Non-Color"   # 数据贴图不走 sRGB
    if not obj.material_slots:
        mat = bpy.data.materials.new(f"farm_bake_{obj_name}")
        mat.use_nodes = True
        obj.data.materials.append(mat)
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            mat = bpy.data.materials.new(f"farm_bake_{obj_name}_slot")
            mat.use_nodes = True
            slot.material = mat
        if not mat.use_nodes:
            mat.use_nodes = True
        nt = mat.node_tree
        node = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image == img), None)
        if node is None:
            node = nt.nodes.new("ShaderNodeTexImage")
            node.image = img
        nt.nodes.active = node
        node.select = True

    # context:低模 active(+高模 selected,s2a 模式按 _low/_high 命名配对)
    for selected in list(view_layer.objects):
        if selected.select_get(view_layer=view_layer):
            selected.select_set(False, view_layer=view_layer)
    s2a = job["selected_to_active"]
    hobj = None
    if s2a:
        high = farm_common.high_name(obj_name)
        if not high:
            raise ValueError(f"selected_to_active 需要对象名以 _low 结尾"
                             f"(配对 <name>_high),收到 {obj_name!r}")
        hobj = scene.objects.get(high)
        if hobj is None:
            raise ValueError(f"找不到高模 {high!r}(命名约定 <name>_low / <name>_high)")
        if view_layer.objects.get(high) is None:
            raise ValueError(f"高模 {high!r} 被当前 View Layer 排除;无法 selected-to-active")

    # 可见性隔离:场景里叠放的其他 LOD 档/源模会污染 AO 遮蔽射线与 s2a 采样。
    # 每 unit 只留 目标对 + visible_extra(显式声明的接触遮蔽参照,如相邻部件的
    # 高模)——多部件接触 AO 靠 visible_extra 传名,不再默认全场景可见。
    extra = list(job.get("visible_extra") or [])
    scene_names = {o.name for o in scene.objects}
    extra_missing = [n for n in extra if n not in scene_names]  # 其他 Scene 同名也不能算命中
    isolation = job.get("isolation", "TARGET")
    keep = {obj.name} | ({hobj.name} if hobj else set()) | set(extra)
    if isolation == "SUBMITTED":
        keep.update(job["objects"])
        if s2a:
            keep.update(filter(None, (farm_common.high_name(n) for n in job["objects"])))
    geometry_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME",
                      "CURVES", "POINTCLOUD", "GREASEPENCIL"}
    if isolation != "SCENE":
        for other in scene.objects:
            # Curve/Volume/集合实例同样会进入 Cycles 射线;LIGHT 保留供 COMBINED pass。
            if other.type in geometry_types or (other.type == "EMPTY" and other.instance_collection):
                other.hide_render = other.name not in keep
    for target in filter(None, (obj, hobj)):
        target.hide_render = False
        target.hide_viewport = False
        target.hide_set(False, view_layer=view_layer)   # 隐藏对象无法 select,烘不了

    if hobj is not None:
        hobj.select_set(True, view_layer=view_layer)
    obj.select_set(True, view_layer=view_layer)
    view_layer.objects.active = obj

    # albedo only:DIFFUSE 关光照分量。⚠ warm 容器场景状态跨 unit 存活,
    # 非 DIFFUSE pass 恢复"场景加载时的基线"(不是硬编码 True——场景是真源,
    # 艺术家有意关掉的分量必须尊重)
    if pass_type == "DIFFUSE":
        scene.render.bake.use_pass_direct = False
        scene.render.bake.use_pass_indirect = False
    else:
        scene.render.bake.use_pass_direct = _SCENE.get("bake_direct", True)
        scene.render.bake.use_pass_indirect = _SCENE.get("bake_indirect", True)
    kwargs = dict(type=pass_type, margin=job["margin"])
    if s2a:
        kwargs.update(use_selected_to_active=True,
                      cage_extrusion=job["cage_extrusion"],
                      max_ray_distance=job["max_ray_distance"])
    t0 = time.time()
    selected_objects = [item for item in (hobj, obj) if item is not None]
    with bpy.context.temp_override(
            scene=scene, view_layer=view_layer, object=obj, active_object=obj,
            selected_objects=selected_objects, selected_editable_objects=selected_objects):
        result = bpy.ops.object.bake(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender bake 未完成(result={result})")
    tmp = Path(f"/tmp/{fname}")
    tmp.unlink(missing_ok=True)
    img.filepath_raw = str(tmp)
    img.file_format = job["file_format"]
    img.save()
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise RuntimeError("Blender bake 已返回但目标贴图保存为空")
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp, final)
    tmp.unlink(missing_ok=True)
    models_vol.commit()
    warns = list(_SCENE["warnings"] or [])
    if extra_missing:
        warns.append("visible_extra 对象不存在(拼写?已忽略): "
                     + ", ".join(extra_missing[:8]))
    return {"unit": f"{obj_name}:{pass_type}",
            "path": f"_outputs/{job_id}/textures/{fname}",
            "size": final.stat().st_size, "secs": round(time.time() - t0, 1),
            "warnings": warns, "device": _SCENE["device"]}


@app.function(
    gpu=FARM_GPU,
    image=farm_image,
    volumes={"/vol": models_vol},
    timeout=FRAME_TIMEOUT,
    # Render/Bake 共用一个 function,部署的并行数才是全局费用上限,
    # 不会因两种任务同时跑而翻倍。
    max_containers=MAX_PARALLEL,
)
def gpu_unit(job: dict, unit, job_id: str, launch_token: str) -> dict:
    """Render 帧 / Bake(对象,pass) 的统一 GPU 入口。"""
    if job["task_type"] == "bake":
        obj_name, pass_type = unit
        return _bake_unit_impl(job, obj_name, pass_type, job_id, launch_token)
    return _render_frame_impl(job, int(unit), job_id, launch_token)


@app.function(image=farm_image, volumes={"/vol": models_vol}, timeout=JOB_TIMEOUT)
def bake_job(job: dict, job_id: str, coordinator_token: str) -> dict:
    """bake coordinator:对象×pass 并行 → 全部完成后打 textures.zip。"""
    _await_call_key(job_id, coordinator_token)
    units = farm_common.bake_units(job)
    t0 = time.time()
    job_state[job_id] = {**job_state.get(job_id, {}), "status": "running", "started_at": t0,
                         "progress": {"step": 0, "total": len(units), "elapsed": 0}}
    try:
        _results, warnings, device = _sliding_schedule(
            lambda u, token: gpu_unit.spawn(job, u, job_id, token),
            units, job_id, t0)
        _abort_if_cancelled(job_id)
        models_vol.reload()
        out_root = Path(f"/vol/_outputs/{job_id}")
        tex_dir = out_root / "textures"
        tmp_zip = shutil.make_archive(f"/tmp/{job_id}_textures", "zip", str(tex_dir))
        shutil.copyfile(tmp_zip, out_root / "textures.zip")
        shutil.rmtree(tex_dir, ignore_errors=True)   # 散图已打包,不占 Volume
        outputs = [{"filename": "textures.zip",
                    "volume_path": f"_outputs/{job_id}/textures.zip",
                    "size_bytes": (out_root / "textures.zip").stat().st_size}]
        models_vol.commit()
        _abort_if_cancelled(job_id)
    except Exception as e:
        _fail_job(job_id, e)
        raise
    _complete_job(job_id, outputs, warnings, device)
    return {"units": len(units), "outputs": len(outputs)}


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


def _request_key(request, legacy_query_key: str = "") -> str:
    """新客户端用 header,避免密钥进 URL/代理日志;query 仅保留旧 addon 兼容。"""
    header = request.headers.get("x-farm-key", "")
    auth = request.headers.get("authorization", "")
    if not header and auth.lower().startswith("bearer "):
        header = auth[7:].strip()
    return header or legacy_query_key


def _finalize_scene_file(tmp: Path, name: str, size: int) -> dict:
    """临时文件 → 校验/完整 SHA-256 → 内容寻址落 Volume(已存在秒过)。"""
    import hashlib
    from fastapi.responses import JSONResponse
    try:
        with open(tmp, "rb") as f:
            head = f.read(7)
        if not farm_common.looks_like_blend(head):
            tmp.unlink(missing_ok=True)
            return JSONResponse(
                {"error": f"上传内容不是有效 Blender .blend 文件(头 {head[:4].hex()})"},
                status_code=400)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": f"上传临时文件读取失败: {e}"}, status_code=500)
    sha = hashlib.sha256()
    with open(tmp, "rb") as f:
        while chunk := f.read(1 << 22):
            sha.update(chunk)
    blend_path = f"scenes/{sha.hexdigest()}_{name}"
    dest = Path("/vol") / blend_path
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), dest)
        models_vol.commit()
    else:
        tmp.unlink(missing_ok=True)
        # 内容再次提交 = 最近仍在使用;刷新保留期,避免 sweep 误删即将 run 的输入。
        os.utime(dest, None)
        models_vol.commit()
    return {"blend_path": blend_path, "size_bytes": size}


def _sweep_stale_uploads():
    """清断头分块与长期未复用的场景输入,避免 Volume 无界增长。"""
    try:
        updir = Path("/vol/uploads")
        now = time.time()
        if updir.is_dir():
            for d in updir.iterdir():
                if d.is_dir() and now - d.stat().st_mtime > 86400:
                    shutil.rmtree(d, ignore_errors=True)
                elif d.is_file() and d.name.startswith("done_") \
                        and now - d.stat().st_mtime > 86400:
                    d.unlink(missing_ok=True)
        scenes = Path("/vol/scenes")
        if scenes.is_dir():
            protected = set()
            try:
                protected = {s.get("blend_path") for _jid, s in job_state.items()
                             if (isinstance(s, dict)
                                 and s.get("status") in ("queued", "running")
                                 and s.get("blend_path"))}
            except Exception:
                # Dict 不可读时宁可这一轮不清 scene,不能冒险删活跃输入。
                protected = {scene.relative_to("/vol").as_posix()
                             for scene in scenes.iterdir() if scene.is_file()}
            for scene in scenes.iterdir():
                relative = scene.relative_to("/vol").as_posix()
                if (scene.is_file() and relative not in protected
                        and now - scene.stat().st_mtime > SCENE_TTL_S):
                    scene.unlink(missing_ok=True)
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
    deny = _check(_request_key(request, q.get("key", "")))
    if deny:
        return deny
    name = farm_common.safe_scene_name(q.get("name", ""))
    if not name.lower().endswith(".blend"):
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
        result = _finalize_scene_file(tmp, name, size)
        _sweep_stale_uploads()
        return result

    # ── 分块模式 ──
    if not (len(upload_id) == 32 and all(c in "0123456789abcdef" for c in upload_id)):
        return JSONResponse({"error": "upload_id 必须是 32 位 hex(uuid4().hex)"}, status_code=400)
    try:
        index, total = int(q.get("index", -1)), int(q.get("total", 0))
    except ValueError:
        return JSONResponse({"error": "index/total 必须是整数"}, status_code=400)
    if not (0 <= index < total <= 512):
        return JSONResponse({"error": f"index/total 非法({index}/{total})"}, status_code=400)
    # 幂等回放:末块成功但响应在网络中丢失 → 客户端重传末块。
    # 合并完成时留 done 标记,任何针对已完成 upload_id 的块直接回放原结果。
    done_marker = Path("/vol/uploads") / f"done_{upload_id}.json"
    models_vol.reload()
    if done_marker.is_file():
        return json.loads(done_marker.read_text())
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
    if isinstance(result, dict):   # 成功才留幂等标记(JSONResponse=失败,可整体重传)
        done_marker.write_text(json.dumps(result))
    shutil.rmtree(updir, ignore_errors=True)
    _sweep_stale_uploads()
    models_vol.commit()
    return result


@app.function(image=farm_image, volumes={"/vol": models_vol}, secrets=[farm_secret],
              timeout=60, max_containers=1)
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
    request_id = payload.get("request_id")
    if request_id is not None:
        if (not isinstance(request_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", request_id)):
            return {"error": "request_id 必须是 32 位小写 hex(uuid4().hex)"}
        # 客户端在 /run 响应丢失后会用同一 id 重试;返回原 job,
        # 不能重复 spawn/重复计费。
        existing_id = job_state.get(f"request:{request_id}")
        if existing_id:
            existing = job_state.get(existing_id)
            if existing:
                call_id = _coordinator_call_id(job_state.get(f"{existing_id}:call"))
                if not call_id and existing.get("status") == "queued":
                    age = time.time() - float(existing.get("queued_at") or time.time())
                    if age < 60:
                        return {"error": "job 正在登记 coordinator,请安全重试",
                                "retryable": True}
                    # 原 /run 已超 60s 硬超时,而无 :call;原 coordinator 会因
                    # _await_call_key fail-closed,此处只恢复同一 job,不新建计费 job。
                    coordinator = bake_job if job["task_type"] == "bake" else render_job
                    try:
                        token = uuid.uuid4().hex
                        call = coordinator.spawn(job, existing_id, token)
                        job_state[f"{existing_id}:call"] = {
                            "id": call.object_id, "token": token}
                    except Exception as e:
                        return {"error": f"coordinator 恢复失败: {e}",
                                "retryable": True}
                return {"id": existing_id, "status": existing.get("status", "queued"),
                        "gpu": existing.get("gpu", FARM_GPU), "deduplicated": True}
            try:
                del job_state[f"request:{request_id}"]
            except Exception:
                pass
    # job_id 一律服务端生成:外部值可拼路径逃逸 _outputs 囚笼(../scenes 等)
    job_id = str(uuid.uuid4())
    _sweep_job_state()
    state = {"status": "queued", "queued_at": time.time(),
             "heartbeat_at": time.time(), "gpu": FARM_GPU,
             "task_type": job["task_type"], "blend_path": job.get("blend_path")}
    if request_id:
        state["request_id"] = request_id
    job_state[job_id] = state
    if request_id:
        job_state[f"request:{request_id}"] = job_id
    coordinator = bake_job if job["task_type"] == "bake" else render_job
    try:
        token = uuid.uuid4().hex
        call = coordinator.spawn(job, job_id, token)
    except Exception as e:
        job_state[job_id] = {**state, "status": "failed",
                             "error": f"coordinator 启动失败: {e}",
                             "completed_at": time.time()}
        return {"error": f"coordinator 启动失败: {e}", "id": job_id}
    # call_id 存独立 key:job_state[job_id] 同时被 worker 写(running/completed),Modal Dict
    # 跨容器最终一致,merge 回写会撞 stale 覆盖 —— 分 key 各写各的,无竞态。
    job_state[f"{job_id}:call"] = {"id": call.object_id, "token": token}
    return {"id": job_id, "status": "queued", "gpu": FARM_GPU}


@app.function(image=farm_image, secrets=[farm_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-status")
def status_endpoint(request: "Request", job_id: str = "", request_id: str = "",
                    key: str = ""):
    deny = _check(_request_key(request, key))
    if deny:
        return deny
    if request_id:
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            return {"error": "request_id 必须是 32 位小写 hex"}
        job_id = job_state.get(f"request:{request_id}") or ""
    if not job_id:
        return {"error": "job not found", "id": ""}
    s = job_state.get(job_id)
    if not s:
        return {"error": "job not found", "id": job_id}
    reason = _stalled_reason(s, time.time())
    if reason:
        _fail_stalled_job(job_id, reason)
        s = job_state.get(job_id) or s
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
    if not s:
        return {"error": "job not found", "id": job_id}
    if s.get("status") in ("completed", "failed", "cancelled"):
        # 已完成的产物不能再伪装成 cancelled,否则客户端会失去 Download 入口。
        return {"id": job_id, **s, "already_terminal": True}
    was_running = s.get("status") == "running"
    # 先立取消旗:_fill 会停止 spawn;尚未写入 subcall 快照的 worker 被 launch gate
    # 挡住并在看到此 flag 后自杀,所以不再需要基于时间猜测 coordinator 是否仍在 fill。
    try:
        job_state[f"{job_id}:cancel"] = True
    except Exception as e:
        return {"id": job_id, "status": s.get("status") or "unknown",
                "error": f"cancel 旗写入失败: {e}(未执行任何取消,重试 cancel)",
                "was_running": was_running}

    # 先停快照内子任务,再停 coordinator。快照缝隙里的 worker 尚未过 launch gate。
    sub_failed = []
    for cid in (job_state.get(f"{job_id}:subcalls") or []):
        try:
            modal.FunctionCall.from_id(cid).cancel()
        except Exception as e:
            print(f"[farm] cancel subcall {cid} failed: {e}")
            sub_failed.append(cid)
    if sub_failed:
        # 不杀 coordinator:取消旗已立,它会在 ≤0.5s 的调度轮询/合成边界看见，
        # 再次 cancel 子调用并可靠写 cancelled。若这里同时杀掉 coordinator，
        # 某个子调用 cancel RPC 又失败，就可能无人负责写终态而卡 running。
        return {"id": job_id, "status": s.get("status") or "unknown",
                "error": (f"{len(sub_failed)} 个子任务取消 RPC 失败;"
                          "取消旗已立且 coordinator 正在收尾,可稍后重试 cancel"),
                "was_running": was_running}
    try:
        # 先落终态再杀 coordinator:若进程取消成功后 Dict 写入恰好失败，
        # coordinator 已不存在，会留下永久 running。
        job_state[job_id] = {**s, "status": "cancelled", "completed_at": time.time()}
    except Exception as e:
        return {"id": job_id, "status": s.get("status") or "unknown",
                "error": f"cancelled 终态写入失败: {e}(coordinator 保留并将自行收尾)",
                "was_running": was_running}
    call_id = _coordinator_call_id(job_state.get(f"{job_id}:call"))
    call_error = None
    if call_id:
        try:
            modal.FunctionCall.from_id(call_id).cancel()
        except Exception as e:
            print(f"[farm] cancel call {call_id} FAILED: {e}")
            call_error = str(e)
    if call_error:
        return {"id": job_id, "status": "cancelled",
                "error": (f"coordinator 取消失败: {call_error}"
                          "(取消旗已立,它将自行收尾;请稍后重试 cancel)"),
                "was_running": was_running}
    return {"id": job_id, "status": "cancelled", "was_running": was_running}


def _mark_artifact_fetched(job_id: str, path: str):
    """客户端已原子落盘并删远端文件;更新产物生命周期供 sweep 安全清理。"""
    try:
        state = job_state.get(job_id) or {}
        if not state:
            return
        fetched = set(state.get("fetched_paths") or [])
        fetched.add(path)
        expected = {o.get("volume_path") for o in (state.get("outputs") or [])
                    if o.get("volume_path")}
        pending = not expected.issubset(fetched)
        job_state[job_id] = {**state, "fetched_paths": sorted(fetched),
                             "artifacts_pending": pending,
                             **({"fetched_at": time.time()} if not pending else {})}
    except Exception as e:
        print(f"[farm] mark artifact fetched failed({job_id}/{path}): {e}")


@app.function(image=farm_image, volumes={"/vol": models_vol}, secrets=[farm_secret],
              timeout=600)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-fetch")
def fetch_endpoint(request: "Request", job_id: str, path: str, key: str = "", delete: int = 0,
                   delete_only: int = 0):
    """流式取回产物。path 必须在 _outputs/<job_id>/ 内(囚笼,拒绝逃逸);
    delete=1 → 发送完成后删文件并 commit;
    delete_only=1 → 不传输只删除(客户端原子落盘成功后再清远端)。"""
    deny = _check(_request_key(request, key))
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
    if delete_only:
        try:
            os.remove(local)
            models_vol.commit()
            _mark_artifact_fetched(job_id, path)
            return {"deleted": path}
        except Exception as e:
            return JSONResponse({"error": f"delete failed: {e}"}, status_code=500)
    cleanup = None
    if delete:
        def _cleanup(p=str(local)):
            try:
                os.remove(p)
                models_vol.commit()
                _mark_artifact_fetched(job_id, path)
            except Exception as e:
                print(f"[farm] fetch cleanup {p} failed: {e}")
        cleanup = BackgroundTask(_cleanup)
    return FileResponse(str(local), filename=Path(path).name, background=cleanup)


@app.function(image=farm_image, secrets=[farm_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-health")
def health_endpoint(request: "Request", key: str = ""):
    deny = _check(_request_key(request, key))
    if deny:
        return deny
    return {"healthy": True, "app": APP_NAME, "gpu": FARM_GPU,
            "bpy": BPY_VERSION, "version": DEPLOYED_VERSION,
            "protocol_version": PROTOCOL_VERSION, "max_parallel": MAX_PARALLEL,
            "frame_timeout": FRAME_TIMEOUT, "job_timeout": JOB_TIMEOUT}
