"""ops.py — operators:测试连接 / 提交 / 取消 / 下载。
提交流程(全在后台线程,UI 不阻塞):pack 副本 → 本地预检 → upload → run → 转轮询。"""
import json
import os
import re
import tempfile
import threading
from pathlib import Path

import bpy

from . import jobs


def _scene_props(context):
    """从 Scene 自定义属性读提交参数(ui.py 注册;fps 尊重场景)。
    CUSTOM 模式走复合帧 spec 字符串(补渲散帧),其余两档仍是三元组。"""
    sc = context.scene
    fps = round(sc.render.fps / sc.render.fps_base)
    base = {"output": sc.farm_output, "file_format": sc.farm_file_format, "fps": fps}
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

    def execute(self, context):
        p = jobs.prefs()
        if not p.endpoint or not p.farm_key:
            self.report({"ERROR"}, "先在 addon preferences 填 endpoint 和 farm_key")
            return {"CANCELLED"}
        render = _scene_props(context)
        if render["output"] == "video" and render["file_format"] != "PNG":
            self.report({"ERROR"}, "video 输出只支持 PNG;EXR 请切 output=frames")
            return {"CANCELLED"}
        spec = render.get("frames")
        if spec is not None and not re.fullmatch(r"[\d\s,:\-]+", spec or ""):
            # 粗校验挡明显非法(细校验在云端);别等上传完才发现 spec 打错
            self.report({"ERROR"}, f'帧范围形如 "3, 5-10, 47-327:2",收到 {spec!r}')
            return {"CANCELLED"}
        # 主线程:pack + save copy(bpy.ops 必须主线程)
        tmp_path, warnings = _pack_and_save_copy()
        name = Path(bpy.data.filepath or "untitled.blend").name
        # 下载目录:// 相对当前 .blend 解析为绝对
        out_root = bpy.path.abspath(p.output_dir or "//render_farm/")

        wm = context.window_manager
        it = wm.farm_jobs.add()
        it.job_id = f"local-{os.urandom(4).hex()}"   # 提交成功后换成云端 id
        it.label = f"{name}  {_frames_label(render)}"
        it.status = "uploading"
        if warnings:
            it.warnings = "; ".join(warnings)[:800]
        wm.farm_jobs_index = len(wm.farm_jobs) - 1
        local_key = it.job_id
        jobs.ensure_timer()

        def work():
            def on_up(sent, total, _k=local_key):
                jobs._XFER[_k] = (sent, total)
            try:
                c = jobs.get_client()
                up = c.upload(tmp_path, name, progress_cb=on_up)
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
                jobs._XFER.pop(local_key, None)
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
    jid, out_dir = it.job_id, it.out_dir
    outputs = json.loads(it.outputs_json or "[]")
    if not outputs:
        return
    it.status = "downloading"
    jobs.ensure_timer()

    def work():
        err = None

        def on_dl(sent, total, _k=jid):
            jobs._XFER[_k] = (sent, total)
        try:
            c = jobs.get_client()
            for o in outputs:
                c.fetch(jid, o["volume_path"], str(Path(out_dir) / o["filename"]),
                        progress_cb=on_dl)
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
