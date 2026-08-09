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
_XFER: dict = {}                        # 传输进度:job 键 → (sent_bytes, total_bytes)。
                                        # 网络线程高频写(每块),timer 低频读 → UI,零锁
_CANCEL_UPLOAD: set = set()             # 用户点了取消的本地上传键;上传线程分块间检查


class FarmJobItem(bpy.types.PropertyGroup):
    job_id: bpy.props.StringProperty()
    label: bpy.props.StringProperty()          # 显示名(文件名 + 帧范围)
    status: bpy.props.StringProperty(default="queued")
    step: bpy.props.IntProperty(default=0)
    total: bpy.props.IntProperty(default=0)
    s_it: bpy.props.FloatProperty(default=0.0)
    elapsed: bpy.props.IntProperty(default=0)
    error: bpy.props.StringProperty()
    trace: bpy.props.StringProperty()             # 云端 traceback(详情弹窗/复制)
    warnings: bpy.props.StringProperty()       # 断链清单(分号拼接)
    out_dir: bpy.props.StringProperty()        # 下载目录(绝对路径)
    downloaded: bpy.props.BoolProperty(default=False)
    outputs_json: bpy.props.StringProperty(default="[]")   # 终态 outputs(下载用)
    xfer_sent: bpy.props.IntProperty(default=0)    # 上传/下载已传 MB(进度条)
    xfer_total: bpy.props.IntProperty(default=0)   # 上传/下载总量 MB
    gpu: bpy.props.StringProperty()                # 云端分配的 GPU(status 回传)
    render_device: bpy.props.StringProperty()      # Cycles 实际 backend:OPTIX/CUDA/CPU
    task_type: bpy.props.StringProperty(default="render")  # render / bake(UI 单元文案)
    request_id: bpy.props.StringProperty()        # /run 幂等键(响应全丢后找回)
    blend_path: bpy.props.StringProperty()         # 已上传的 Volume 场景路径
    task_json: bpy.props.StringProperty()           # 找不到远端 job 时用同 request_id 重试
    output_root: bpy.props.StringProperty()         # 恢复远端 id 后重建最终下载目录


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
    # 1.5) 传输进度:网络线程写 _XFER,这里搬进 item 字段(MB)供进度条
    for it in bpy.context.window_manager.farm_jobs:
        if it.status in ("uploading", "downloading"):
            prog = _XFER.get(it.job_id)
            if prog:
                it.xfer_sent, it.xfer_total = prog[0] >> 20, max(1, prog[1] >> 20)
    # 2) 对活跃 job 起轮询线程(在跑的不重复起;uploading 由提交线程自己推进,
    #    local- 前缀 = 还没拿到云端 id,不可查)
    active = [it.job_id for it in bpy.context.window_manager.farm_jobs
              if it.status in ("queued", "running")]
    for jid in active:
        if jid not in _POLLING and not jid.startswith("local-"):
            try:
                client = get_client()  # 主线程读取 Blender Preferences;线程只拿纯 stdlib 对象
            except Exception:
                continue
            _POLLING.add(jid)
            threading.Thread(target=_poll_once, args=(jid, client), daemon=True).start()
    # 3) UI 重绘 + 决定 timer 去留
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    has_active = any(it.status in (
        "queued", "running", "uploading", "downloading", "recovering")
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


def _poll_once(job_id: str, client):
    """后台线程:查一次状态,结果闭包回主线程。"""
    try:
        s = client.status(job_id)
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
        if s.get("error") and not s.get("status"):
            # job not found / 协议语义错误不是网络抖动;继续 queued 会永久轮询。
            it.status = "failed"
            it.error = str(s["error"])[:400]
            persist()
            return
        it.status = s.get("status") or it.status
        if s.get("task_type"):
            it.task_type = str(s["task_type"])
        if s.get("gpu"):
            it.gpu = str(s["gpu"])
        if s.get("render_device"):
            it.render_device = str(s["render_device"])
        p = s.get("progress") or {}
        if p:
            it.step, it.total = p.get("step", it.step), p.get("total", it.total)
            it.s_it, it.elapsed = p.get("s_it", it.s_it), p.get("elapsed", it.elapsed)
        if s.get("error"):
            it.error = str(s["error"])[:400]
        elif it.status in ("completed", "cancelled"):
            # 清掉取消重试/下载重试留下的瞬时警示,避免终态仍显示“正在计费”。
            it.error = ""
        if s.get("trace"):
            it.trace = str(s["trace"])[:2000]
        if s.get("warnings"):
            local = [w.strip() for w in it.warnings.split(";") if w.strip()]
            remote = [str(w).strip() for w in s["warnings"] if str(w).strip()]
            it.warnings = "; ".join(dict.fromkeys(local + remote))[:8000]
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
             "outputs_json": it.outputs_json, "error": it.error, "trace": it.trace,
             "warnings": it.warnings, "gpu": it.gpu,
             "render_device": it.render_device, "task_type": it.task_type,
             "request_id": it.request_id, "blend_path": it.blend_path,
             "task_json": it.task_json, "output_root": it.output_root,
             "step": it.step, "total": it.total, "s_it": it.s_it,
             "elapsed": it.elapsed}
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
    allowed = {"job_id", "label", "status", "out_dir", "downloaded", "outputs_json",
               "error", "trace", "warnings", "gpu", "render_device", "task_type",
               "request_id", "blend_path", "task_json", "output_root",
               "step", "total", "s_it",
               "elapsed"}
    for d in data[-20:] if isinstance(data, list) else []:  # 最多恢复最近 20 条
        if not isinstance(d, dict) or not isinstance(d.get("job_id"), str):
            continue
        it = wm.farm_jobs.add()
        for k, v in d.items():
            if k in allowed:
                try:
                    setattr(it, k, v)
                except (TypeError, ValueError):
                    pass
        # 进程死亡后不存在可恢复的本地上传/下载线程,绝不永久卡在
        # uploading/downloading。下载保留已完成终态和 outputs,用户可点按钮重试。
        if it.status in ("uploading", "recovering"):
            it.status = "failed"
            it.error = ("Blender 上次在提交/恢复完成前退出;请点恢复按钮重试"
                        if it.request_id and it.blend_path and it.task_json
                        else "Blender 上次在上传完成前退出;请重新提交")
        elif it.status == "downloading":
            it.status = "completed"
            it.error = "下载被 Blender 退出中断;点下载按钮重试"
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
