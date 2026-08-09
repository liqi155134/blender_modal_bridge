"""Blender Modal Bridge — 云端渲染农场(Modal serverless GPU)。
N 面板(3D 视图 → Farm 页签)提交当前文件,进度可视,产物自动取回。"""
bl_info = {
    "name": "Blender Modal Bridge (Render Farm)",
    "author": "liqi",
    "version": (0, 2, 0),
    "blender": (5, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Farm",
    "description": "Submit Cycles renders to Modal serverless GPUs (L40S/OptiX)",
    "category": "Render",
}

import bpy  # noqa: E402 — bl_info 必须先于 import(Blender addon 惯例)

from . import jobs, ops, ui  # noqa: E402


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
