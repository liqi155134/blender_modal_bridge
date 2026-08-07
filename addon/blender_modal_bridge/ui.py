"""ui.py — N 面板(3D 视图 → Sidebar → Farm)+ Scene 提交参数属性。"""
import bpy

from . import jobs


def _scene_props():
    S = bpy.types.Scene
    S.farm_frame_mode = bpy.props.EnumProperty(
        name="Frames", default="SCENE",
        items=[("SCENE", "Scene Range", "用场景的 frame_start/end/step"),
               ("CURRENT", "Current Frame", "只渲当前帧(look dev 快查)"),
               ("CUSTOM", "Custom", "复合帧范围,如 3, 5-10, 47-327:2(补渲散帧)")])
    S.farm_frames_spec = bpy.props.StringProperty(
        name="Frames", default="1-48",
        description='复合帧范围:"47" / "1-30" / "3, 5-10, 47-327:2"(逗号分段,:step 跳帧)')
    S.farm_output = bpy.props.EnumProperty(
        name="Output", default="video",
        items=[("video", "Video (mp4)", "帧渲完 ffmpeg 合成 mp4"),
               ("frames", "Frames (zip)", "帧序列打 zip(EXR 用这个)")])
    S.farm_file_format = bpy.props.EnumProperty(
        name="Format", default="PNG",
        items=[("PNG", "PNG", ""), ("OPEN_EXR", "OpenEXR", "需 Output=Frames")])


def _del_scene_props():
    S = bpy.types.Scene
    for k in ("farm_frame_mode", "farm_frames_spec", "farm_output", "farm_file_format"):
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
            col.prop(sc, "farm_frames_spec")
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
