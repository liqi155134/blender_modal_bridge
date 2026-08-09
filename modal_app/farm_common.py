"""
farm_common.py — 提交协议的纯函数层(校验 / 帧列表 / ffmpeg 命令 / 文件名清洗)。

零第三方依赖(纯 stdlib):云端 farm_app(容器内以顶层名 import)、Blender addon、
单测三方共用。协议改动先改这里的校验再动别处。
task_type 支持 render / bake;两者共用上传、调度、取消、状态与下载骨架。
"""
import hashlib
import math
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
                or ".." in blend_path or blend_path != norm or len(blend_path) > 255):
            return None, "blend_path 必须是 Volume 上 scenes/ 下的相对路径(由 /upload 返回)"
    scene_name = payload.get("scene_name")
    if scene_name is not None:
        if not isinstance(scene_name, str) or not scene_name.strip():
            return None, "scene_name 必须是非空字符串(.blend 里的 Scene 名)"
        scene_name = scene_name.strip()
        if len(scene_name) > 255:
            return None, "scene_name 过长(最多 255 字符)"
    view_layer_name = payload.get("view_layer_name")
    if view_layer_name is not None:
        if not isinstance(view_layer_name, str) or not view_layer_name.strip():
            return None, "view_layer_name 必须是非空字符串"
        view_layer_name = view_layer_name.strip()
        if len(view_layer_name) > 255:
            return None, "view_layer_name 过长(最多 255 字符)"
    if task_type == "bake":
        return _normalize_bake(
            payload.get("bake") or {}, blend_path, scene_name, view_layer_name)
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
    # ffmpeg glob 会把缺帧压成连续画面,不会保留原时间轴。拒绝稀疏帧生成
    # 一个时长/动作速度都错误、却看起来能播放的 mp4。
    if output == "video" and (step != 1 or n != end - start + 1):
        return None, "video 只支持连续帧(step=1);补渲散帧/跳帧请用 output=frames"
    try:
        fps_num = int(r.get("fps_num", r.get("fps", 24)))
        fps_den = int(r.get("fps_den", 1))
    except (TypeError, ValueError):
        return None, "fps_num / fps_den 必须是整数"
    # 用整数交叉比较,不把外部传入的超大 int 转 float(可 OverflowError)。
    if fps_num < 1 or fps_den < 1 or fps_num < fps_den or fps_num > 240 * fps_den:
        return None, "帧率范围 1..240 fps(fps_num/fps_den 须为正整数)"
    job = {"task_type": task_type, "blend_path": blend_path,
           "frame_start": start, "frame_end": end, "frame_step": step,
           "output": output, "file_format": fmt,
           "fps_num": fps_num, "fps_den": fps_den}
    if scene_name is not None:
        job["scene_name"] = scene_name
    if view_layer_name is not None:
        job["view_layer_name"] = view_layer_name
    if frames_spec is not None:
        job["frames_spec"] = str(frames_spec).strip()
    limits = {"resolution_x": 32768, "resolution_y": 32768,
              "resolution_percentage": 100, "samples": 1_000_000}
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
        if v > limits[k]:
            return None, f"{k} 必须 ≤ {limits[k]}"
        job[k] = v
    cam = r.get("camera")
    if cam is not None:
        if not isinstance(cam, str) or not cam.strip():
            return None, "camera 必须是非空字符串(场景里的相机对象名)"
        job["camera"] = cam.strip()
    return job, None


def _normalize_bake(b: dict, blend_path, scene_name=None,
                    view_layer_name=None) -> tuple[dict | None, str | None]:
    """bake 参数校验 → 扁平 job。对象 × pass 是并行单元。"""
    if not isinstance(b, dict):
        return None, "bake 必须是对象"
    objects = b.get("objects")
    if (not isinstance(objects, list) or not objects
            or not all(isinstance(o, str) and o.strip() for o in objects)):
        return None, "objects 必填:要烘焙的对象名列表(非空字符串)"
    if len(objects) > 128:
        return None, "objects 最多 128 个"
    objects = list(dict.fromkeys(o.strip() for o in objects))   # 去重保序:重复对象=并发写同一输出
    raw_passes = b.get("passes", ["NORMAL", "AO"])
    if not isinstance(raw_passes, list) or not raw_passes:
        return None, "passes 必须是非空列表"
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
    s2a = b.get("selected_to_active", False)
    if not isinstance(s2a, bool):   # bool("false") is True —— 字符串一律拒绝
        return None, "selected_to_active 必须是布尔值"
    extra = b.get("visible_extra") or []
    if (not isinstance(extra, list) or len(extra) > 64
            or not all(isinstance(o, str) and o.strip() for o in extra)):
        return None, "visible_extra 须是对象名列表(≤64,非空字符串)"
    isolation = str(b.get("isolation") or "TARGET").upper()
    if isolation not in ("TARGET", "SUBMITTED", "SCENE"):
        return None, "isolation 只能是 TARGET / SUBMITTED / SCENE"
    job = {"task_type": "bake", "blend_path": blend_path,
           "objects": objects, "passes": passes,
           "selected_to_active": s2a,
           "visible_extra": [o.strip() for o in extra],
           "isolation": isolation,
           "file_format": fmt}
    if scene_name is not None:
        job["scene_name"] = scene_name
    if view_layer_name is not None:
        job["view_layer_name"] = view_layer_name
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
    if not (math.isfinite(cage) and math.isfinite(ray)):
        return None, "cage_extrusion / max_ray_distance 必须是有限数"
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
        if start < 0 or end > MAX_FRAME_NO:
            raise ValueError(f"帧号范围 0..{MAX_FRAME_NO}: {part.strip()!r}")
        # 不可先 frames.update(超大 range) 再检查长度:恶意/误输的范围会在
        # normalize_job 有机会返回错误前吃光 endpoint 或 Blender 的内存。
        for frame in range(start, end + 1, step):
            frames.add(frame)
            if len(frames) > MAX_FRAMES:
                raise ValueError(f"单 job 最多 {MAX_FRAMES} 帧")
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


def ffmpeg_cmd(frames_dir: str, out_path: str, fps_num: int,
               fps_den: int = 1) -> list[str]:
    """PNG 帧序列 → H.264 mp4。glob 按字典序 = 帧序(帧名固定 %05d 零填充)。
    pad 滤镜兜底奇数分辨率(yuv420p 要求偶数,场景分辨率是艺术家定的,不该因此失败)。"""
    rate = str(fps_num) if fps_den == 1 else f"{fps_num}/{fps_den}"
    return ["ffmpeg", "-y", "-framerate", rate, "-pattern_type", "glob",
            "-i", f"{frames_dir}/*.png",
            # crf 20 + 关键帧间隔 18:对齐 Flamenco 官方出片参数(x264 默认 crf23 偏低)
            "-c:v", "libx264", "-crf", "20", "-g", "18", "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            out_path]


def safe_scene_name(name: str) -> str:
    """上传文件名清洗:取 basename、危险字符换 _,空则兜底。upload 端点用。"""
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not base:
        return "scene.blend"
    # Volume 内容名还会加 64-byte SHA-256 前缀;封顶后保持常见 255-byte
    # 文件名限制,且统一扩展名避免 .BLEND 被无意义拒绝。
    if base.lower().endswith(".blend"):
        base = base[:-6][:154] + ".blend"
    else:
        base = base[:160]
    return base


def bake_output_stem(name: str) -> str:
    """对象名 → 可识别且实际不碰撞的贴图文件名片段。

    仅替换危险字符会让 ``A/B`` 与 ``A\\B`` 都变成 ``A_B``;全部带原名
    SHA-256 短后缀,同时规避 macOS/Windows 大小写不敏感文件系统的覆盖。
    """
    raw = str(name)
    safe = re.sub(r"[/\\\x00-\x1f]", "_", raw).strip() or "obj"
    # Blender ID 通常不长,仍封顶避免 UTF-8 文件名超过常见 255-byte 上限。
    while len(safe.encode("utf-8")) > 180:
        safe = safe[:-1]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe}--{digest}"


# .blend 容器魔数。⚠ 只认 b"BLENDER" 是错的:Blender 3.0+ 的 compress=True 存的是
# Zstandard 容器,2.9 及更早是 gzip —— addon 提交时一律压缩,只认未压缩头会把
# 所有真实场景拒之门外(2026-08-09 实锤:8K 烘焙任务在末块合并时 HTTP 400)。
BLEND_MAGICS = (
    b"BLENDER",            # 未压缩
    b"\x28\xb5\x2f\xfd",   # Zstandard(Blender 3.0+ compress=True)
    b"\x1f\x8b",           # gzip(Blender ≤ 2.9 的旧压缩格式)
)


def looks_like_blend(head: bytes) -> bool:
    """文件头 → 是否是 .blend(含压缩容器)。传入至少前 7 字节。"""
    return any(bytes(head).startswith(m) for m in BLEND_MAGICS)
