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
# bake pass 白名单(常用 PBR 集;DIFFUSE 由 worker 自动关 direct/indirect = albedo)
BAKE_PASSES = ("NORMAL", "AO", "DIFFUSE", "ROUGHNESS", "EMIT", "COMBINED")
MAX_BAKE_UNITS = 256    # 单 job 上限:对象数 × pass 数(费用护栏)


def normalize_job(payload: dict) -> tuple[dict | None, str | None]:
    """校验提交 payload,返回 (扁平 job dict, None) 或 (None, 错误信息)。
    payload 形态:{task_type?, blend_path?, render: {…}}。
    blend_path=None 表示内置 demo 场景(不碰 Volume,链路冒烟)。
    resolution/samples/camera 不给 = 尊重 .blend 场景设置(艺术家文件是真源)。"""
    if not isinstance(payload, dict):
        return None, "payload 必须是对象"
    task_type = str(payload.get("task_type") or "render").lower()
    if task_type not in ("render", "bake"):
        return None, f"task_type={task_type!r} 暂未支持(可用: render / bake)"
    blend_path = payload.get("blend_path")
    if blend_path is not None:
        norm = os.path.normpath(str(blend_path)).replace("\\", "/")
        if (not isinstance(blend_path, str) or not blend_path.startswith("scenes/")
                or ".." in blend_path or blend_path != norm):
            return None, "blend_path 必须是 Volume 上 scenes/ 下的相对路径(由 /upload 返回)"
    if task_type == "bake":
        return _normalize_bake(payload.get("bake") or {}, blend_path)
    r = payload.get("render") or {}
    if not isinstance(r, dict):
        return None, "render 必须是对象"
    # 帧范围两种来源:frames(复合 spec 字符串,优先;补渲散帧用)或 frame_start/end/step 三元组
    frames_spec = r.get("frames")
    frames = None
    if frames_spec is not None:
        try:
            frames = expand_frame_spec(str(frames_spec))
        except ValueError as e:
            return None, f'frames 无效(形如 "3, 5-10, 47-327:2"): {e}'
        start, end, step = frames[0], frames[-1], 1   # 仅供显示;真帧列表走 frames_spec
        n = len(frames)
    else:
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
        n = len(range(start, end + 1, step))
    if start < 0 or end > MAX_FRAME_NO:
        return None, f"帧号范围 0..{MAX_FRAME_NO}"
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
    if frames_spec is not None:
        job["frames_spec"] = str(frames_spec).strip()
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


def _normalize_bake(b: dict, blend_path) -> tuple[dict | None, str | None]:
    """bake 参数校验 → 扁平 job。对象 × pass 是并行单元。"""
    if not isinstance(b, dict):
        return None, "bake 必须是对象"
    objects = b.get("objects")
    if (not isinstance(objects, list) or not objects
            or not all(isinstance(o, str) and o.strip() for o in objects)):
        return None, "objects 必填:要烘焙的对象名列表(非空字符串)"
    if len(objects) > 128:
        return None, "objects 最多 128 个"
    objects = [o.strip() for o in objects]
    raw_passes = b.get("passes") or ["NORMAL", "AO"]
    passes = []
    for p in raw_passes:
        p = str(p).upper()
        if p not in BAKE_PASSES:
            return None, f"pass {p!r} 不支持(可用: {', '.join(BAKE_PASSES)})"
        if p not in passes:
            passes.append(p)
    n = len(objects) * len(passes)
    if n > MAX_BAKE_UNITS:
        return None, f"单 job 最多 {MAX_BAKE_UNITS} 个烘焙单元(对象×pass;现 {n});请分批提交"
    fmt = str(b.get("file_format") or "PNG").upper()
    if fmt not in ("PNG", "OPEN_EXR"):
        return None, "file_format 只能是 PNG 或 OPEN_EXR"
    job = {"task_type": "bake", "blend_path": blend_path,
           "objects": objects, "passes": passes,
           "selected_to_active": bool(b.get("selected_to_active", False)),
           "file_format": fmt}
    try:
        res = int(b.get("resolution", 2048))
        margin = int(b.get("margin", 16))
        cage = float(b.get("cage_extrusion", 0.0))
        ray = float(b.get("max_ray_distance", 0.0))
    except (TypeError, ValueError):
        return None, "resolution/margin 必须是整数,cage_extrusion/max_ray_distance 必须是数"
    if not 64 <= res <= 8192:
        return None, "resolution 范围 64..8192"
    if not 1 <= margin <= 64:
        return None, "margin 范围 1..64"
    if cage < 0 or ray < 0:
        return None, "cage_extrusion / max_ray_distance 必须 ≥ 0"
    job.update(resolution=res, margin=margin, cage_extrusion=cage, max_ray_distance=ray)
    samples = b.get("samples")
    if samples is not None:
        try:
            samples = int(samples)
        except (TypeError, ValueError):
            return None, "samples 必须是整数"
        if samples < 1:
            return None, "samples 必须 ≥ 1"
        job["samples"] = samples
    return job, None


def bake_units(job: dict) -> list[tuple[str, str]]:
    """bake job → (object, pass) 并行单元列表(顺序稳定:对象外层、pass 内层)。"""
    return [(o, p) for o in job["objects"] for p in job["passes"]]


def high_name(low: str) -> str | None:
    """`<name>_low` → `<name>_high`(高低模命名约定,只换最后一个后缀);不含 _low 返回 None。"""
    if low.endswith("_low"):
        return low[: -len("_low")] + "_high"
    return None


def frames_list(job: dict) -> list[int]:
    """规范化后的 job → 要渲染的帧号列表(复合 spec 优先)。"""
    if job.get("frames_spec"):
        return expand_frame_spec(job["frames_spec"])
    return list(range(job["frame_start"], job["frame_end"] + 1, job["frame_step"]))


def expand_frame_spec(spec: str) -> list[int]:
    """复合帧范围 → 去重升序帧列表。"3, 5-10, 47-327:2":逗号分段,段=帧号|区间|区间:step。
    补渲散帧(镜头返修)靠它。非法抛 ValueError。"""
    s = str(spec).strip()
    if not s:
        raise ValueError("空 spec")
    frames: set[int] = set()
    for part in s.split(","):
        start, end, step = parse_frame_spec(part)
        if step < 1:
            raise ValueError(f"step 必须 ≥ 1: {part.strip()!r}")
        if end < start:
            raise ValueError(f"区间尾 < 头: {part.strip()!r}")
        frames.update(range(start, end + 1, step))
    return sorted(frames)


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
            # crf 20 + 关键帧间隔 18:对齐 Flamenco 官方出片参数(x264 默认 crf23 偏低)
            "-c:v", "libx264", "-crf", "20", "-g", "18", "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            out_path]


def safe_scene_name(name: str) -> str:
    """上传文件名清洗:取 basename、危险字符换 _,空则兜底。upload 端点用。"""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or "scene.blend"
