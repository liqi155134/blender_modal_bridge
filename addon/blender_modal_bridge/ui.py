"""ui.py — N 面板(3D 视图 → Sidebar → Farm)+ Scene 提交参数属性。"""
import bpy

from . import jobs


def _scene_props():
    S = bpy.types.Scene
    S.farm_task = bpy.props.EnumProperty(
        name="Task", default="RENDER",
        items=[("RENDER", "Render", "渲染(静帧/动画)"),
               ("BAKE", "Bake", "烘焙贴图(选中对象 × 勾选 pass 并行)")])
    S.farm_bake_normal = bpy.props.BoolProperty(name="Normal", default=True)
    S.farm_bake_ao = bpy.props.BoolProperty(name="AO", default=True)
    S.farm_bake_diffuse = bpy.props.BoolProperty(name="Diffuse", default=False,
                                                 description="albedo(自动关直接/间接光)")
    S.farm_bake_roughness = bpy.props.BoolProperty(name="Rough", default=False)
    S.farm_bake_emit = bpy.props.BoolProperty(name="Emit", default=False)
    S.farm_bake_combined = bpy.props.BoolProperty(name="Combined", default=False)
    S.farm_bake_resolution = bpy.props.IntProperty(
        name="Resolution", default=2048, min=64, max=8192,
        description="目标贴图分辨率(方形)")
    S.farm_bake_margin = bpy.props.IntProperty(name="Margin", default=16, min=1, max=64)
    S.farm_bake_s2a = bpy.props.BoolProperty(
        name="High → Low", default=False,
        description="高模→低模烘焙:按 <name>_low / <name>_high 命名约定自动配对")
    S.farm_bake_cage = bpy.props.FloatProperty(
        name="Cage Extrusion", default=0.05, min=0.0, precision=3)
    S.farm_bake_ray = bpy.props.FloatProperty(
        name="Max Ray Distance", default=0.0, min=0.0, precision=3,
        description="s2a 采样射线最大距离(0=不限;空壳件建议 ≈2×cage 防采到对面壁)")
    S.farm_bake_visible_extra = bpy.props.StringProperty(
        name="Visible Extra", default="",
        description="烘焙时额外保持可见的对象名(逗号分隔)。用于接触遮蔽参照:"
                    "相邻部件的高模等。默认只有目标对可见")
    S.farm_bake_isolation = bpy.props.EnumProperty(
        name="Isolation", default="TARGET",
        items=[("TARGET", "Target Only", "仅当前目标对 + Visible Extra"),
               ("SUBMITTED", "All Submitted", "所有本次提交对象 + Visible Extra"),
               ("SCENE", "Whole Scene", "尊重整个 Scene 原始渲染可见性")])
    S.farm_bake_format = bpy.props.EnumProperty(
        name="Format", default="PNG",
        items=[("PNG", "PNG", ""), ("OPEN_EXR", "OpenEXR", "")])
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
    for k in ("farm_task", "farm_bake_normal", "farm_bake_ao", "farm_bake_diffuse",
              "farm_bake_roughness", "farm_bake_emit", "farm_bake_combined",
              "farm_bake_resolution", "farm_bake_margin", "farm_bake_s2a",
              "farm_bake_cage", "farm_bake_ray", "farm_bake_visible_extra",
              "farm_bake_isolation", "farm_bake_format",
              "farm_frame_mode", "farm_frames_spec", "farm_output", "farm_file_format"):
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

        row = lay.row(align=True)
        row.prop(sc, "farm_task", expand=True)

        col = lay.column(align=True)
        col.label(text=f"Scene / Layer: {sc.name} / {context.view_layer.name}",
                  icon="SCENE_DATA")
        submit_ready = sc.render.engine == "CYCLES"
        if sc.farm_task == "BAKE":
            selected = [o for o in context.selected_objects if o.type == "MESH"]
            n_sel = len(selected)
            col.label(text=f"选中网格: {n_sel} 个" if n_sel else "先在视图选中要烘的网格",
                      icon="MESH_DATA" if n_sel else "ERROR")
            submit_ready = submit_ready and bool(selected)
            no_uv = [o.name for o in selected if not o.data.uv_layers]
            if no_uv:
                col.label(text=f"未展 UV: {', '.join(no_uv[:3])}", icon="ERROR")
                submit_ready = False
            grid = col.grid_flow(columns=3, align=True)
            for p in ("farm_bake_normal", "farm_bake_ao", "farm_bake_diffuse",
                      "farm_bake_roughness", "farm_bake_emit", "farm_bake_combined"):
                grid.prop(sc, p, toggle=True)
            row = col.row(align=True)
            row.prop(sc, "farm_bake_resolution")
            row.prop(sc, "farm_bake_margin")
            col.prop(sc, "farm_bake_format")
            col.prop(sc, "farm_bake_s2a")
            if sc.farm_bake_s2a:
                col.prop(sc, "farm_bake_cage")
                col.prop(sc, "farm_bake_ray")
            col.prop(sc, "farm_bake_isolation")
            extra_row = col.row()
            extra_row.enabled = sc.farm_bake_isolation != "SCENE"
            extra_row.prop(sc, "farm_bake_visible_extra")
            passes_on = any(getattr(sc, p) for p in (
                "farm_bake_normal", "farm_bake_ao", "farm_bake_diffuse",
                "farm_bake_roughness", "farm_bake_emit", "farm_bake_combined"))
            if not passes_on:
                col.label(text="至少勾选一个 Bake pass", icon="ERROR")
            submit_ready = submit_ready and passes_on
        else:
            col.prop(sc, "farm_frame_mode")
            if sc.farm_frame_mode == "CUSTOM":
                col.prop(sc, "farm_frames_spec")
            row = col.row(align=True)
            row.prop(sc, "farm_output", expand=True)
            col.prop(sc, "farm_file_format")
            if sc.camera is None:
                col.label(text="当前 Scene 没有活动相机", icon="ERROR")
                submit_ready = False
            if sc.farm_output == "video" and sc.farm_file_format != "PNG":
                col.label(text="Video 只支持 PNG;请换 PNG 或 Frames", icon="ERROR")
                submit_ready = False
            if sc.farm_output == "video" and sc.farm_frame_mode == "SCENE" \
                    and sc.frame_step != 1:
                col.label(text="Video 需要 Frame Step = 1;跳帧请选 Frames", icon="ERROR")
                submit_ready = False
            if sc.farm_output == "video" and sc.farm_frame_mode == "CUSTOM":
                col.label(text="Custom 稀疏帧可能改变时长;提交时会校验连续性", icon="INFO")
        if sc.render.engine != "CYCLES":
            col.label(text=f"引擎是 {sc.render.engine},农场只支持 Cycles", icon="ERROR")
        p = jobs.prefs()
        if not bpy.data.filepath and (p.output_dir or "//render_farm/").startswith("//"):
            col.label(text="先保存 .blend,才能使用 // 相对输出目录", icon="ERROR")
            submit_ready = False

        submit_row = lay.row()
        submit_row.enabled = submit_ready
        submit_row.operator("farm.submit", icon="RENDER_ANIMATION")  # ⚠ Blender 5.2 无 CLOUD icon
        row = lay.row(align=True)
        row.operator("farm.test_connection", icon="PLUGIN")
        row.operator("farm.clear_finished", icon="TRASH")

        wm = context.window_manager
        if not len(wm.farm_jobs):
            return
        for it in reversed(list(wm.farm_jobs)[-8:]):     # 最近 8 条,新的在上
            self.draw_job(lay.box(), it)

    def draw_job(self, box, it) -> None:
        """单个 job 的卡片:标题行(名字/id/GPU/操作)+ 进度条/状态行 + 警告行。"""
        row = box.row(align=True)
        row.label(text=it.label or it.job_id[:8],
                  icon=_STATUS_ICON.get(it.status, "QUESTION"))
        meta = "" if it.job_id.startswith("local-") else it.job_id[:8]
        if it.gpu:
            meta = f"{meta} · {it.gpu}" if meta else it.gpu
        if it.render_device:
            meta = f"{meta}/{it.render_device}"
        if meta:
            row.label(text=meta)
        if it.status in ("uploading", "queued", "running"):
            row.operator("farm.cancel", text="", icon="X").job_id = it.job_id
        elif it.status == "completed" and not it.downloaded:
            row.operator("farm.download", text="", icon="IMPORT").job_id = it.job_id
        elif it.status == "completed" and it.downloaded:
            row.operator("farm.open_output", text="", icon="FILE_FOLDER").job_id = it.job_id
        elif (it.status == "failed" and it.request_id and it.blend_path
              and it.task_json and it.output_root):
            row.operator("farm.recover_submission", text="", icon="FILE_REFRESH").job_id = it.job_id
        if it.error or it.trace or it.warnings:
            row.operator("farm.job_details", text="", icon="INFO").job_id = it.job_id

        if it.status == "uploading":
            if it.xfer_total:
                box.progress(factor=min(1.0, it.xfer_sent / it.xfer_total),
                             text=f"上传 {it.xfer_sent} / {it.xfer_total} MB", type="BAR")
            else:
                box.label(text="打包场景 / 准备上传…", icon="EXPORT")
        elif it.status == "downloading":
            if it.xfer_total:
                box.progress(factor=min(1.0, it.xfer_sent / it.xfer_total),
                             text=f"下载 {it.xfer_sent} / {it.xfer_total} MB", type="BAR")
            else:
                box.label(text="下载产物中…", icon="IMPORT")
        elif it.status == "recovering":
            box.label(text="按 request ID 查找/恢复原任务…", icon="FILE_REFRESH")
        elif it.status == "running":
            if it.total:
                unit = "单元" if it.task_type == "bake" else "帧"
                box.progress(factor=min(1.0, it.step / max(1, it.total)),
                             text=f"{it.step} / {it.total} {unit} · {it.s_it:.1f}s/{unit}"
                                  f" · 已 {it.elapsed}s", type="BAR")
            else:
                box.label(text="云端启动中(容器冷启 + 场景加载)…", icon="SORTTIME")
        elif it.status == "queued":
            box.label(text="已提交,排队中…", icon="SORTTIME")
        elif it.status == "completed":
            if it.downloaded:
                box.label(text=f"✓ 完成,已下载 → {it.out_dir}"[:72], icon="CHECKMARK")
            else:
                noun = "烘焙" if it.task_type == "bake" else "渲染"
                box.label(text=f"✓ {noun}完成(点 ⬇ 下载)", icon="CHECKMARK")
        elif it.status == "failed":
            err = it.error or "failed"
            box.label(text=err[:68], icon="ERROR")
            if len(err) > 68:
                box.label(text=err[68:136])
        elif it.status == "cancelled":
            box.label(text="已取消", icon="X")
        if it.error and it.status != "failed":
            # 非 failed 状态的 error 是风险警示(如「远端取消失败,仍可能计费」),
            # 必须显示 —— 只在 failed 分支显示 error 会把它吞掉
            box.label(text=it.error[:68], icon="ERROR")
        if it.warnings:
            n = it.warnings.count(";") + 1
            box.label(text=f"⚠ {n} 条警告: {it.warnings[:56]}"
                           f"{'… (点 ⓘ 查看全部)' if len(it.warnings) > 56 else ''}",
                      icon="LIBRARY_DATA_BROKEN")


_CLASSES = (FARM_PT_panel,)


def register():
    _scene_props()
    for c in _CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
    _del_scene_props()
