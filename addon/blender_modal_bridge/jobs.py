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


class FarmJobItem(bpy.types.PropertyGroup):
    job_id: bpy.props.StringProperty()
    label: bpy.props.StringProperty()          # 显示名(文件名 + 帧范围)
    status: bpy.props.StringProperty(default="queued")
    step: bpy.props.IntProperty(default=0)
    total: bpy.props.IntProperty(default=0)
    s_it: bpy.props.FloatProperty(default=0.0)
    elapsed: bpy.props.IntProperty(default=0)
    error: bpy.props.StringProperty()
    warnings: bpy.props.StringProperty()       # 断链清单(分号拼接)
    out_dir: bpy.props.StringProperty()        # 下载目录(绝对路径)
    downloaded: bpy.props.BoolProperty(default=False)
    outputs_json: bpy.props.StringProperty(default="[]")   # 终态 outputs(下载用)
    xfer_sent: bpy.props.IntProperty(default=0)    # 上传/下载已传 MB(进度条)
    xfer_total: bpy.props.IntProperty(default=0)   # 上传/下载总量 MB
    gpu: bpy.props.StringProperty()                # 云端分配的 GPU(status 回传)


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
            _POLLING.add(jid)
            threading.Thread(target=_poll_once, args=(jid,), daemon=True).start()
    # 3) UI 重绘 + 决定 timer 去留
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    has_active = any(it.status in ("queued", "running", "uploading", "downloading")
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


def _poll_once(job_id: str):
    """后台线程:查一次状态,结果闭包回主线程。"""
    try:
        s = get_client().status(job_id)
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
        it.status = s.get("status") or it.status
        if s.get("gpu"):
            it.gpu = str(s["gpu"])
        p = s.get("progress") or {}
        if p:
            it.step, it.total = p.get("step", it.step), p.get("total", it.total)
            it.s_it, it.elapsed = p.get("s_it", it.s_it), p.get("elapsed", it.elapsed)
        if s.get("error"):
            it.error = str(s["error"])[:400]
        if s.get("warnings"):
            it.warnings = "; ".join(s["warnings"])[:800]
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
             "outputs_json": it.outputs_json, "error": it.error}
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
    for d in data[-20:]:                     # 最多恢复最近 20 条
        it = wm.farm_jobs.add()
        for k, v in d.items():
            setattr(it, k, v)
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
