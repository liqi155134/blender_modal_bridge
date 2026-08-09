"""ops.py — operators:测试连接 / 提交 / 取消 / 下载。
提交流程(全在后台线程,UI 不阻塞):pack 副本 → 本地预检 → upload → run → 转轮询。"""
import json
import os
import re
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path

import bpy

from . import jobs


def _scene_props(context):
    """从 Scene 自定义属性读提交参数(ui.py 注册;fps 尊重场景)。
    CUSTOM 模式走复合帧 spec 字符串(补渲散帧),其余两档仍是三元组。"""
    sc = context.scene
    # RNA 把 fps_base 存为 float32: UI 的 1.001 读回会是 1.0010000467。先按 UI
    # 有效精度归一,否则 23.976 会变成难读的 1283940/53551 而非 24000/1001。
    fps_base = Fraction(str(round(float(sc.render.fps_base), 6)))
    fps = (Fraction(sc.render.fps) / fps_base).limit_denominator(100000)
    base = {"output": sc.farm_output, "file_format": sc.farm_file_format,
            "fps_num": fps.numerator, "fps_den": fps.denominator}
    mode = sc.farm_frame_mode
    if mode == "CURRENT":
        return {**base, "frame_start": sc.frame_current, "frame_end": sc.frame_current,
                "frame_step": 1}
    if mode == "SCENE":
        return {**base, "frame_start": sc.frame_start, "frame_end": sc.frame_end,
                "frame_step": sc.frame_step}
    return {**base, "frames": sc.farm_frames_spec}   # CUSTOM


def _frames_label(render: dict) -> str:
    """job 列表显示用的帧范围描述。"""
    if render.get("frames"):
        return str(render["frames"])
    return f"{render['frame_start']}-{render['frame_end']}"


def _build_task(context) -> tuple[dict | None, str | None]:
    """按 farm_task 模式构建提交任务,返回 (task, None) 或 (None, 错误)。
    BAKE:对象 = 当前选中的网格(最 Blender 原生的选择方式)。"""
    sc = context.scene
    if sc.render.engine != "CYCLES":
        return None, f"当前 Scene {sc.name!r} 的引擎是 {sc.render.engine};请先切到 Cycles"
    if sc.farm_task == "BAKE":
        selected = [o for o in context.selected_objects if o.type == "MESH"]
        if not selected:
            return None, "Bake 需要先在视图里选中至少一个网格对象"
        no_uv = [o.name for o in selected if not o.data.uv_layers]
        if no_uv:
            return None, "Bake 对象尚未展 UV: " + ", ".join(no_uv[:8])
        objs = [o.name for o in selected]
        passes = [p for p, on in (
            ("NORMAL", sc.farm_bake_normal), ("AO", sc.farm_bake_ao),
            ("DIFFUSE", sc.farm_bake_diffuse), ("ROUGHNESS", sc.farm_bake_roughness),
            ("EMIT", sc.farm_bake_emit), ("COMBINED", sc.farm_bake_combined)) if on]
        if not passes:
            return None, "至少勾选一个 bake pass"
        bake = {"objects": objs, "passes": passes,
                "resolution": sc.farm_bake_resolution, "margin": sc.farm_bake_margin,
                "selected_to_active": sc.farm_bake_s2a,
                "isolation": sc.farm_bake_isolation,
                "file_format": sc.farm_bake_format}
        if sc.farm_bake_s2a:
            bake["cage_extrusion"] = sc.farm_bake_cage
            bake["max_ray_distance"] = sc.farm_bake_ray
            bad_low = [n for n in objs if not n.endswith("_low")]
            if bad_low:
                return None, "High → Low 模式只选中 <name>_low 目标: " + ", ".join(bad_low[:8])
            missing_high = [n[:-4] + "_high" for n in objs
                            if sc.objects.get(n[:-4] + "_high") is None]
            if missing_high:
                return None, "找不到配对高模: " + ", ".join(missing_high[:8])
            excluded_high = [n[:-4] + "_high" for n in objs
                             if (sc.objects.get(n[:-4] + "_high") is not None
                                 and context.view_layer.objects.get(
                                     n[:-4] + "_high") is None)]
            if excluded_high:
                return None, "高模被当前 View Layer 排除: " + ", ".join(excluded_high[:8])
        extra = [n.strip() for n in (sc.farm_bake_visible_extra or "").split(",") if n.strip()]
        if extra:
            missing_extra = [n for n in extra if sc.objects.get(n) is None]
            if missing_extra:
                return None, "Visible Extra 对象不存在: " + ", ".join(missing_extra[:8])
            excluded_extra = [n for n in extra
                              if context.view_layer.objects.get(n) is None]
            if excluded_extra and sc.farm_bake_isolation != "SCENE":
                return None, "Visible Extra 被当前 View Layer 排除: " + ", ".join(
                    excluded_extra[:8])
            bake["visible_extra"] = extra
        if len(objs) * len(passes) > 256:
            return None, f"Bake 最多 256 单元(当前 {len(objs) * len(passes)})"
        return {"task_type": "bake", "scene_name": sc.name,
                "view_layer_name": context.view_layer.name, "bake": bake}, None
    render = _scene_props(context)
    if sc.camera is None:
        return None, f"当前 Scene {sc.name!r} 没有活动相机"
    if render["output"] == "video" and render["file_format"] != "PNG":
        return None, "video 输出只支持 PNG;EXR 请切 output=frames"
    spec = render.get("frames")
    if spec is not None and not re.fullmatch(r"[\d\s,:\-]+", spec or ""):
        return None, f'帧范围形如 "3, 5-10, 47-327:2",收到 {spec!r}'
    try:
        count = _render_frame_count(render)
    except ValueError as e:
        return None, f"帧范围无效: {e}"
    if count > 2000:
        return None, f"单 job 最多 2000 帧(当前 {count});请分段提交"
    if render["output"] == "video" and not _render_frames_are_contiguous(render, count):
        return None, "Video 只支持连续帧(step=1);补渲散帧/跳帧请选 Frames"
    return {"task_type": "render", "scene_name": sc.name,
            "view_layer_name": context.view_layer.name, "render": render}, None


def _render_frame_count(render: dict) -> int:
    """提交前本地计数/语义校验,避免上传大文件后才被服务端拒绝。"""
    spec = render.get("frames")
    if spec is None:
        start, end, step = (render["frame_start"], render["frame_end"], render["frame_step"])
        if start < 0 or end > 99999 or end < start or step < 1:
            raise ValueError("需满足 0 ≤ start ≤ end ≤ 99999 且 step ≥ 1")
        return len(range(start, end + 1, step))
    frames = set()
    for raw in str(spec).split(","):
        part = raw.strip()
        if not part:
            raise ValueError("存在空分段")
        match = re.fullmatch(r"(\d+)(?:-(\d+))?(?::(\d+))?", part)
        if not match:
            raise ValueError(f"{part!r} 应为 7 / 1-20 / 1-20:2")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        step = int(match.group(3) or 1)
        if start < 0 or end > 99999 or end < start or step < 1:
            raise ValueError(f"{part!r} 越界或步长无效")
        for frame in range(start, end + 1, step):
            frames.add(frame)
            if len(frames) > 2000:
                return len(frames)
    if not frames:
        raise ValueError("不能为空")
    return len(frames)


def _render_frames_are_contiguous(render: dict, count: int) -> bool:
    spec = render.get("frames")
    if spec is None:
        return render["frame_step"] == 1
    bounds = []
    for raw in str(spec).split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?(?::(\d+))?\s*", raw)
        if not match:
            return False  # 具体语法错误由 _render_frame_count 返回
        start = int(match.group(1))
        end = int(match.group(2) or start)
        bounds.extend((start, end))
    return bool(bounds) and count == max(bounds) - min(bounds) + 1


def _task_label(task: dict) -> str:
    if task["task_type"] == "bake":
        b = task["bake"]
        return f"bake {len(b['objects'])}obj×{len(b['passes'])}pass"
    return _frames_label(task["render"])


def _confirmation_message(task: dict, warnings=None, scene=None) -> str:
    """把真正会计费的规模在提交前说清楚。"""
    scene = task["scene_name"]
    layer = task["view_layer_name"]
    if task["task_type"] == "bake":
        bake = task["bake"]
        units = len(bake["objects"]) * len(bake["passes"])
        detail = (f"Bake {len(bake['objects'])} 对象 × {len(bake['passes'])} pass"
                  f" = {units} GPU 单元, {bake['resolution']}² {bake['file_format']}")
    else:
        render = task["render"]
        frames = _render_frame_count(render)
        rate = render["fps_num"] / render["fps_den"]
        detail = (f"Render {frames} 帧, {render['output']} / {render['file_format']}, "
                  f"{rate:.3f} fps")
    if scene is not None:
        samples = getattr(getattr(scene, "cycles", None), "samples", "?")
        if task["task_type"] == "render":
            scale = scene.render.resolution_percentage / 100
            width = round(scene.render.resolution_x * scale)
            height = round(scene.render.resolution_y * scale)
            detail += f", {width}×{height}, {samples} samples"
        else:
            detail += f", {samples} samples"
    warning_text = ""
    if warnings:
        preview = "; ".join(warnings[:3])
        more = f" 等 {len(warnings)} 项" if len(warnings) > 3 else ""
        warning_text = f"\n⚠ 外部资产预检警告{more}: {preview[:360]}\n"
    return (f"Scene / View Layer: {scene} / {layer}\n{detail}\n{warning_text}"
            "Modal GPU 将按实际运行时长计费。确认打包、上传并启动任务？")


def _precheck_missing() -> list[str]:
    """本地预检:pack 不进去的外部资产(比云端更早暴露)。链接库必警告——不会上云。
    跳过孤儿数据块(users=0,无任何材质引用):迁移残留的死数据与渲染无关,
    报出来纯属吓人(2026-08-08 用户 swan 工程实锤 3 个 C4D 残留 roughness 贴图误报)。"""
    missing = []
    for img in bpy.data.images:
        if img.users - int(img.use_fake_user) <= 0:
            continue
        if img.source == "FILE" and img.filepath and not img.packed_file:
            if not Path(bpy.path.abspath(img.filepath)).exists():
                missing.append(f"image: {img.filepath}")
    # pack_all 不只处理图片;提前把其它常见文件数据块也报出来。
    for label, blocks in (
            ("font", bpy.data.fonts), ("sound", bpy.data.sounds),
            ("movieclip", bpy.data.movieclips), ("volume", bpy.data.volumes)):
        for block in blocks:
            if getattr(block, "users", 0) - int(getattr(block, "use_fake_user", False)) <= 0:
                continue
            path = getattr(block, "filepath", "")
            if (path and not path.startswith("<") and not getattr(block, "packed_file", None)
                    and not Path(bpy.path.abspath(path)).exists()):
                missing.append(f"{label}: {path}")
    # CacheFile(Alembic/MDD/USD 等)不属于 pack_all 可嵌入资产,即使本机存在也会
    # 在云端断开;分帧并行对未烘焙物理模拟也不保证逐帧状态连续。
    for cache in getattr(bpy.data, "cache_files", ()):
        if getattr(cache, "users", 0) and getattr(cache, "filepath", ""):
            missing.append(f"cache file(不会打包): {cache.filepath}")
    simulation_types = {"CLOTH", "FLUID", "SOFT_BODY", "DYNAMIC_PAINT"}
    for obj in bpy.data.objects:
        sims = sorted({mod.type for mod in obj.modifiers if mod.type in simulation_types})
        if getattr(obj, "particle_systems", None):
            sims.append("PARTICLES")
        if sims:
            missing.append(f"simulation(分帧前请先烘焙/转网格): {obj.name} [{', '.join(sims)}]")
    for scene in bpy.data.scenes:
        if getattr(scene, "rigidbody_world", None):
            missing.append(f"simulation(刚体缓存不会打包): Scene {scene.name}")
    for lib in bpy.data.libraries:
        if not lib.users_id:
            continue   # 空壳库引用(链接的数据块已全被删/purge):不参与渲染,不报
        missing.append(f"library(不会打包,云端必缺): {lib.filepath}")
    return missing


def _prepare_scene_copy() -> tuple[str, list[str], str]:
    """主线程只做工作态 save-copy;返回(副本,警告,Blender 可执行文件)。

    真正 pack 在后台线程启独立 Blender,避免大场景 pack 卡住主 UI。
    """
    temp_root = Path(tempfile.gettempdir())
    # Blender/机器强退时 daemon 线程来不及 finally;下次提交顺手清 24h 旧副本。
    for stale in temp_root.glob("farm_submit_*.blend*"):
        try:
            if time.time() - stale.stat().st_mtime > 86400:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
    warnings = _precheck_missing()
    import uuid as _uuid
    tmp = temp_root / f"farm_submit_{os.getpid()}_{_uuid.uuid4().hex[:8]}.blend"
    try:
        bpy.ops.wm.save_as_mainfile(
            filepath=str(tmp), copy=True, compress=False, relative_remap=True)
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            raise RuntimeError("save-copy 未产生有效 .blend")
        return str(tmp), warnings, bpy.app.binary_path
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _pack_scene_copy(tmp_path: str, blender_binary: str, cancel_check=None):
    """后台线程中调用:独立 Blender 只改副本,当前会话零 pack/unpack 变更。

    这样当前会话中的 Image/Font/Sound/分块图原始 packed 状态均不会被碰;
    pack 或二次保存失败也不需要靠不完整的逐类 unpack 回滚。
    """
    tmp = Path(tmp_path)
    marker = tmp.with_name(tmp.name + ".packed-ok")
    try:
        marker.unlink(missing_ok=True)
        code = (
            "import bpy\n"
            "pack_error = ''\n"
            "try:\n"
            "    result = bpy.ops.file.pack_all()\n"
            "    if 'FINISHED' not in result:\n"
            "        pack_error = f'pack_all result={result}'\n"
            "except Exception as e:\n"
            "    pack_error = str(e)\n"
            "    print('[farm] pack_all partial failure:', e)\n"
            f"bpy.ops.wm.save_as_mainfile(filepath={str(tmp)!r}, compress=True)\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(pack_error or 'ok')\n"
        )
        # 日志落临时文件而不是 PIPE:大场景日志超过 pipe buffer 时不会反压
        # 卡死子进程;文件句柄离开 with 自动清理。
        with tempfile.TemporaryFile(mode="w+") as log:
            proc = subprocess.Popen(
                [blender_binary, "--background", "--disable-autoexec", str(tmp),
                 "--python-expr", code],
                stdout=log, stderr=subprocess.STDOUT, text=True)
            started = time.monotonic()
            while proc.poll() is None:
                if cancel_check and cancel_check():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise RuntimeError("打包已被用户取消")
                if time.monotonic() - started > 1800:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError("独立 Blender 打包超时(1800s)")
                time.sleep(0.1)
            log.seek(0)
            detail = log.read()[-1200:]
            if (proc.returncode != 0 or not marker.is_file()
                    or not tmp.is_file() or tmp.stat().st_size < 1024):
                raise RuntimeError(f"独立 Blender 打包失败: {detail or 'unknown error'}")
            marker_result = marker.read_text(errors="replace")
        marker.unlink(missing_ok=True)
        return None if marker_result == "ok" else marker_result[:800]
    except Exception:
        marker.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        raise


class FARM_OT_test_connection(bpy.types.Operator):
    bl_idname = "farm.test_connection"
    bl_label = "Test Connection"
    bl_description = "检查 endpoint/farm_key 是否可用(/health)"

    def execute(self, context):
        try:
            client = jobs.get_client()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        jobs.ensure_timer()

        def work():
            try:
                h = client.health()
                protocol = int(h.get("protocol_version") or 1)
                ok = protocol >= client.REQUIRED_PROTOCOL
                prefix = "✓" if ok else "✗ 云端过旧,请重跑 farm_deploy.py —"
                msg = (f"{prefix} {h.get('app')} gpu={h.get('gpu')} bpy={h.get('bpy')} "
                       f"protocol=v{protocol}")
            except Exception as e:
                msg, ok = f"✗ {e}", False

            def apply():
                # operator 生命周期已结束,report 不可用 → 弹窗
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

    def invoke(self, context, event):
        p = jobs.prefs()
        if not p.endpoint or not p.farm_key:
            self.report({"ERROR"}, "先在 addon preferences 填 endpoint 和 farm_key")
            return {"CANCELLED"}
        if not bpy.data.filepath and (p.output_dir or "//render_farm/").startswith("//"):
            self.report({"ERROR"}, "当前 .blend 尚未保存;// 输出目录无法定位,请先保存文件")
            return {"CANCELLED"}
        task, terr = _build_task(context)
        if terr:
            self.report({"ERROR"}, terr)
            return {"CANCELLED"}
        try:
            jobs.get_client()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        warnings = _precheck_missing()
        return context.window_manager.invoke_confirm(
            self, event, title="Confirm Modal GPU Job",
            message=_confirmation_message(task, warnings, context.scene),
            confirm_text="Submit & Start Billing",
            icon="QUESTION")

    def execute(self, context):
        p = jobs.prefs()
        if not p.endpoint or not p.farm_key:
            self.report({"ERROR"}, "先在 addon preferences 填 endpoint 和 farm_key")
            return {"CANCELLED"}
        if not bpy.data.filepath and (p.output_dir or "//render_farm/").startswith("//"):
            self.report({"ERROR"}, "当前 .blend 尚未保存;// 输出目录无法定位,请先保存文件")
            return {"CANCELLED"}
        task, terr = _build_task(context)
        if terr:
            self.report({"ERROR"}, terr)
            return {"CANCELLED"}
        try:
            client = jobs.get_client()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        # 下载目录:// 相对当前 .blend 解析为绝对。启动计费前先验证可创建，
        # 避免任务跑完才发现本地目录无权限。
        out_root = bpy.path.abspath(p.output_dir or "//render_farm/")
        try:
            Path(out_root).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.report({"ERROR"}, f"输出目录不可创建: {e}")
            return {"CANCELLED"}
        # 主线程:save copy(bpy.ops 必须主线程);独立进程随后 pack。
        try:
            tmp_path, warnings, blender_binary = _prepare_scene_copy()
        except Exception as e:
            self.report({"ERROR"}, f"场景打包失败(工作会话未改动): {str(e)[-300:]}")
            return {"CANCELLED"}
        name = Path(bpy.data.filepath or "untitled.blend").name
        wm = context.window_manager
        it = wm.farm_jobs.add()
        it.job_id = f"local-{os.urandom(4).hex()}"   # 提交成功后换成云端 id
        it.label = f"{name}  {_task_label(task)}"
        it.task_type = task["task_type"]
        it.request_id = uuid.uuid4().hex
        it.task_json = json.dumps(task)
        it.output_root = str(Path(out_root))
        it.status = "uploading"
        if warnings:
            it.warnings = "; ".join(warnings)[:8000]
        wm.farm_jobs_index = len(wm.farm_jobs) - 1
        local_key = it.job_id
        request_id = it.request_id
        # client 已在主线程快照 endpoint/key;后台线程不读取 bpy Preferences。
        jobs.persist()  # request/task/output root 先落盘;Blender 此刻退出也不丢提交身份
        jobs.ensure_timer()

        def work():
            uploaded_path = None
            run_attempted = False
            def on_up(sent, total, _k=local_key):
                jobs._XFER[_k] = (sent, total)
            try:
                c = client
                health = c.health()
                if int(health.get("protocol_version") or 0) < c.REQUIRED_PROTOCOL:
                    raise RuntimeError(
                        f"云端协议过旧({health.get('protocol_version') or 1});"
                        "请先用当前仓库重跑 python3 farm_deploy.py")
                pack_warning = _pack_scene_copy(
                    tmp_path, blender_binary,
                    cancel_check=lambda: local_key in jobs._CANCEL_UPLOAD)
                if pack_warning:
                    warnings.append(f"pack_all 部分失败: {pack_warning}")

                    def show_pack_warning(_k=local_key, _warnings=tuple(warnings)):
                        it2 = jobs.find(_k)
                        if it2:
                            it2.warnings = "; ".join(_warnings)[:8000]
                    jobs.push_result(show_pack_warning)
                up = c.upload(tmp_path, name, progress_cb=on_up,
                              cancel_check=lambda: local_key in jobs._CANCEL_UPLOAD)
                uploaded_path = up["blend_path"]
                remembered = threading.Event()

                def remember_upload():
                    try:
                        it2 = jobs.find(local_key)
                        if it2:
                            it2.blend_path = uploaded_path
                            jobs.persist()
                    finally:
                        remembered.set()
                jobs.push_result(remember_upload)
                # /run 前必须确认恢复凭据已由主线程持久化;否则 Blender 恰好退出
                # 会再次制造拿不到 id 的孤儿计费任务。
                if not remembered.wait(timeout=10):
                    raise RuntimeError("本地恢复信息持久化超时;未启动云端任务,可重新提交")
                # 上传返回后、run 前必须再查一次取消:单发路径与最后一块
                # 传输期间的取消都落在这里兜住,否则云端照跑照计费
                if local_key in jobs._CANCEL_UPLOAD:
                    raise RuntimeError("上传已被用户取消")
                run_attempted = True
                d = c.run(task, uploaded_path, request_id=request_id)
                # /run 请求期间点的取消:远端 job 已创建,立即补发 cancel。
                # ⚠ 取消失败绝不能把 item 写成 cancelled:那会丢远端 id、丢 Cancel
                # 按钮,任务失联但云端还在计费。失败 → 保住远端 id,回 queued 让
                # 轮询接管,error 警示留在卡片上,用户可点 ✕ 重试
                if local_key in jobs._CANCEL_UPLOAD:
                    jobs._CANCEL_UPLOAD.discard(local_key)
                    note = ""
                    remote_status = "cancelled"
                    try:
                        r = c.cancel(d["id"])
                        if r.get("error"):
                            note = str(r["error"])[:200]
                        else:
                            remote_status = str(r.get("status") or "cancelled")
                    except Exception as ce:
                        note = str(ce)[:200]

                    def cancel_done(_id=d["id"], _n=note, _s=remote_status):
                        it2 = jobs.find(local_key)
                        if not it2:
                            return
                        it2.job_id = _id
                        # 只要远端 job 已存在就必须补齐下载目录;取消失败后任务可能完成。
                        it2.out_dir = str(Path(out_root) / _id)
                        if _n:
                            it2.status = "queued"   # 轮询刷新真实状态
                            it2.error = f"⚠ 远端取消失败,仍可能计费: {_n} —— 点 ✕ 重试"
                        elif _s == "cancelled":
                            it2.status, it2.error = "cancelled", ""
                        else:
                            # 请求到达时任务已 completed/failed;回 queued 轮询完整终态与 outputs。
                            it2.status = "queued"
                            it2.error = f"取消请求到达时任务已 {_s},正在刷新最终状态"
                        jobs.persist()
                    jobs.push_result(cancel_done)
                    return   # finally 仍会清理 tmp / _XFER
            except Exception as e:
                # ⚠ 必须在 except 块内先取值:Python 会在块退出时 del e,
                # 闭包延迟到 timer 执行时引用 e 会 NameError(被 tick 兜底吞掉,
                # 表现为任务永远卡 uploading —— 2026-08-08 实锤踩过)
                err = str(e)[:400]
                was_cancel = local_key in jobs._CANCEL_UPLOAD
                jobs._CANCEL_UPLOAD.discard(local_key)

                uncertain_remote = bool(run_attempted and uploaded_path)

                def fail(_err=err, _c=was_cancel, _blend=uploaded_path,
                         _uncertain=uncertain_remote):
                    it2 = jobs.find(local_key)
                    if it2:
                        if _blend:
                            it2.blend_path = _blend
                        if _c and not _uncertain:
                            # 走到这里的取消都发生在上传阶段(run 后的取消在
                            # try 内 inline 处理),远端从未建 job,干净取消
                            it2.status, it2.error = "cancelled", ""
                        else:
                            it2.status, it2.error = "failed", _err
                            if _c and _uncertain:
                                it2.error = ("取消发生在 /run 响应不确定期间;"
                                             "请先点恢复按钮找回原任务,再点取消")
                        jobs.persist()
                jobs.push_result(fail)
                return
            finally:
                jobs._XFER.pop(local_key, None)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

            def ok():
                it2 = jobs.find(local_key)
                if it2:
                    it2.blend_path = uploaded_path or ""
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
        if jid.startswith("local-"):
            # 还在上传:置取消标志,上传线程在下一个块边界(≤16MB)中止 → 状态转 cancelled
            jobs._CANCEL_UPLOAD.add(jid)
            self.report({"INFO"}, "取消中(在当前分块传完后生效)…")
            return {"FINISHED"}
        try:
            client = jobs.get_client()
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        jobs.ensure_timer()

        def work():
            try:
                r = client.cancel(jid)
            except Exception as e:
                r = {"error": str(e)}

            def apply():
                it = jobs.find(jid)
                if it is None:
                    return
                if r.get("error"):
                    it.error = f"取消失败(云端仍在计费!): {r['error']}"[:400]
                else:
                    remote_status = str(r.get("status") or "cancelled")
                    if remote_status == "cancelled":
                        it.status, it.error = "cancelled", ""
                    else:
                        # completed/failed 已是终态,不能在本地伪装成 cancelled;
                        # 先恢复轮询以取得完整 outputs/error。
                        it.status = "queued"
                        it.error = f"取消请求到达时任务已 {remote_status},正在刷新最终状态"
                        jobs.ensure_timer()
                jobs.persist()
            jobs.push_result(apply)
        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


class FARM_OT_recover_submission(bpy.types.Operator):
    bl_idname = "farm.recover_submission"
    bl_label = "Recover Submission"
    bl_description = "按原 request ID 找回响应丢失的任务;不存在才安全重试提交"

    job_id: bpy.props.StringProperty()

    def execute(self, _context):
        it = jobs.find(self.job_id)
        if (it is None or not it.request_id or not it.blend_path
                or not it.task_json or not it.output_root):
            self.report({"ERROR"}, "该记录没有可恢复的提交信息")
            return {"CANCELLED"}
        local_key = it.job_id
        request_id = it.request_id
        blend_path = it.blend_path
        output_root = it.output_root
        try:
            task = json.loads(it.task_json)
        except Exception:
            self.report({"ERROR"}, "本地任务参数已损坏,无法安全重试")
            return {"CANCELLED"}
        it.status = "recovering"
        it.error = "正在按 request ID 查找原任务…"
        try:
            client = jobs.get_client()
        except Exception as e:
            it.status = "failed"
            it.error = str(e)[:400]
            jobs.persist()
            return {"CANCELLED"}
        jobs.ensure_timer()

        def work():
            try:
                found = client.status_by_request(request_id)
                if found.get("error") == "job not found":
                    found = client.run(
                        task, blend_path, request_id=request_id)
                if "id" not in found or found.get("error"):
                    raise RuntimeError(found.get("error") or f"恢复响应异常: {found}")
                remote_id = str(found["id"])

                def apply_ok():
                    it2 = jobs.find(local_key)
                    if it2 is None:
                        return
                    it2.job_id = remote_id
                    # 无论远端目前是否终态,交给一次标准 status 轮询完整同步
                    # outputs/warnings/trace,不复制半份状态。
                    it2.status = "queued"
                    it2.error = ""
                    it2.out_dir = str(Path(output_root) / remote_id)
                    jobs.persist()
                    jobs.ensure_timer()
                jobs.push_result(apply_ok)
            except Exception as e:
                error = str(e)[:400]

                def apply_fail():
                    it2 = jobs.find(local_key)
                    if it2:
                        it2.status = "failed"
                        it2.error = f"恢复提交失败(可重试): {error}"
                        jobs.persist()
                jobs.push_result(apply_fail)

        threading.Thread(target=work, daemon=True).start()
        return {"FINISHED"}


def start_download(it):
    """下载 job 产物到 it.out_dir(jobs 轮询完成时自动调,或 Download 按钮手动调)。
    必须主线程调用(读 item 属性),网络在线程。"""
    jid, out_dir = it.job_id, it.out_dir
    outputs = json.loads(it.outputs_json or "[]")
    if not outputs:
        return
    it.status = "downloading"
    try:
        client = jobs.get_client()
    except Exception as e:
        it.status = "completed"
        it.error = f"下载配置无效: {e}"[:400]
        jobs.persist()
        return
    jobs.ensure_timer()

    def work():
        err = None

        def on_dl(sent, total, _k=jid):
            jobs._XFER[_k] = (sent, total)
        try:
            c = client
            for o in outputs:
                dest = Path(out_dir) / o["filename"]
                expected = int(o.get("size_bytes") or 0)
                # Blender 可能在“原子落盘成功 → 本地状态持久化”之间退出。
                # 尺寸与服务端清单一致时复用已下载文件,避免远端已删后重试只得 404。
                if dest.is_file() and expected and dest.stat().st_size == expected:
                    c.delete_remote(jid, o["volume_path"])
                    continue
                c.fetch(jid, o["volume_path"], str(dest), progress_cb=on_dl)
        except Exception as e:
            err = str(e)[:400]
        finally:
            jobs._XFER.pop(jid, None)

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


def _job_details_text(it) -> str:
    parts = [f"Job: {it.job_id}", f"Task: {it.task_type}", f"Status: {it.status}"]
    if it.gpu:
        parts.append(f"GPU: {it.gpu}")
    if it.render_device:
        parts.append(f"Cycles backend: {it.render_device}")
    if it.out_dir:
        parts.append(f"Output: {it.out_dir}")
    if it.error:
        parts.extend(("", "Error:", it.error))
    if it.trace:
        parts.extend(("", "Remote traceback:", it.trace))
    if it.warnings:
        parts.extend(("", "Warnings:", it.warnings.replace("; ", "\n")))
    return "\n".join(parts)


class FARM_OT_job_details(bpy.types.Operator):
    bl_idname = "farm.job_details"
    bl_label = "Job Details"
    bl_description = "查看完整错误/警告(可复制)"

    job_id: bpy.props.StringProperty()

    def invoke(self, context, _event):
        if jobs.find(self.job_id) is None:
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=620, title="Farm Job Details", confirm_text="Close")

    def draw(self, _context):
        it = jobs.find(self.job_id)
        if it is None:
            self.layout.label(text="Job no longer exists", icon="ERROR")
            return
        col = self.layout.column(align=True)
        for line in _job_details_text(it).splitlines():
            wrapped = textwrap.wrap(line, width=86, break_long_words=True) or [""]
            for part in wrapped:
                col.label(text=part)
        op = self.layout.operator("farm.copy_job_details", icon="COPYDOWN")
        op.job_id = self.job_id

    def execute(self, _context):
        return {"FINISHED"}


class FARM_OT_copy_job_details(bpy.types.Operator):
    bl_idname = "farm.copy_job_details"
    bl_label = "Copy Details"
    bl_description = "复制完整任务详情到剪贴板"

    job_id: bpy.props.StringProperty()

    def execute(self, context):
        it = jobs.find(self.job_id)
        if it is None:
            return {"CANCELLED"}
        context.window_manager.clipboard = _job_details_text(it)
        self.report({"INFO"}, "Farm job details copied")
        return {"FINISHED"}


class FARM_OT_open_output(bpy.types.Operator):
    bl_idname = "farm.open_output"
    bl_label = "Open Output Folder"
    bl_description = "在系统文件管理器中打开已下载产物目录"

    job_id: bpy.props.StringProperty()

    def execute(self, _context):
        it = jobs.find(self.job_id)
        path = Path(it.out_dir) if it and it.out_dir else None
        if path is None or not path.is_dir():
            self.report({"ERROR"}, "输出目录不存在(尚未下载或已移动)")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=str(path))
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
            FARM_OT_recover_submission, FARM_OT_download,
            FARM_OT_job_details, FARM_OT_copy_job_details,
            FARM_OT_open_output, FARM_OT_clear_finished)


def register():
    for c in _CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
