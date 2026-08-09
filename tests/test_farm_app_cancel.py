"""取消调度回归测试:不启动 Modal call,只用假的 FunctionCall/Dict 验证状态机。"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modal_app"))
import farm_app as fa  # noqa: E402


class FakeCall:
    def __init__(self, object_id, result=None, before_get=None, token=None):
        self.object_id = object_id
        self._result = result or {"warnings": [], "device": "fake"}
        self._before_get = before_get
        self.token = token
        self.cancelled = False

    def get(self, timeout=None):
        if self._before_get:
            self._before_get()
        return self._result

    def cancel(self):
        self.cancelled = True


def test_cancel_before_fill_spawns_nothing(monkeypatch):
    state = {"j:cancel": True}
    spawned = []
    monkeypatch.setattr(fa, "job_state", state)

    with pytest.raises(fa._JobCancelled):
        fa._sliding_schedule(
            lambda unit, _token: spawned.append(unit), [1, 2], "j", time.time())

    assert spawned == []
    assert state["j:subcalls"] == []


def test_coordinator_never_runs_without_registered_call_id(monkeypatch):
    monkeypatch.setattr(fa, "job_state", {})
    monkeypatch.setattr(fa.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="call id 未登记"):
        fa._await_call_key("j", "token-a")


def test_late_coordinator_cannot_use_recovery_call_id(monkeypatch):
    state = {"j:call": {"id": "new-call", "token": "token-new"}}
    monkeypatch.setattr(fa, "job_state", state)
    with pytest.raises(RuntimeError, match="取代"):
        fa._await_call_key("j", "token-old")
    assert fa._await_call_key("j", "token-new") == "new-call"


def test_cancel_during_window_never_returns_partial_success(monkeypatch):
    state = {}
    monkeypatch.setattr(fa, "job_state", state)
    monkeypatch.setattr(fa, "MAX_PARALLEL", 1)  # window=2,第三个单元留在 pending
    calls = []

    def spawn(unit, token):
        before_get = (lambda: state.__setitem__("j:cancel", True)) if unit == 1 else None
        call = FakeCall(f"call-{unit}", {"unit": unit}, before_get, token)
        calls.append(call)
        return call

    with pytest.raises(fa._JobCancelled):
        fa._sliding_schedule(spawn, [1, 2, 3], "j", time.time())

    assert [c.object_id for c in calls] == ["call-1", "call-2"]
    # call-1 已完成并从 inflight 移除;快照只保留仍需 cancel 的 call-2。
    assert state["j:subcalls"] == ["call-2"]


def test_scheduler_collects_completed_call_without_fifo_head_block(monkeypatch):
    class SlowCall(FakeCall):
        def get(self, timeout=None):
            if timeout is not None:
                raise fa.modal.exception.TimeoutError()
            return super().get(timeout)

    state = {}
    monkeypatch.setattr(fa, "job_state", state)
    monkeypatch.setattr(fa, "MAX_PARALLEL", 1)
    slow = SlowCall("slow")
    fast = FakeCall("fast", {"unit": "fast"})
    spawned = []

    def spawn(unit, _token):
        call = slow if unit == 1 else fast
        spawned.append(call)
        return call

    # 快 call 应先被收割,然后才在第二轮取消;若 FIFO 会永远卡在 slow。
    seen = []

    def cancel_after_fast():
        seen.append("fast")
        state["j:cancel"] = True

    fast._before_get = cancel_after_fast
    with pytest.raises(fa._JobCancelled):
        fa._sliding_schedule(spawn, [1, 2], "j", time.time())
    assert spawned == [slow, fast]
    assert seen == ["fast"]


def test_scheduler_does_not_swallow_worker_timeout_subclass(monkeypatch):
    class TimedOutCall(FakeCall):
        def get(self, timeout=None):
            raise fa.modal.exception.FunctionTimeoutError("frame exceeded timeout")

    state = {}
    monkeypatch.setattr(fa, "job_state", state)
    with pytest.raises(fa.modal.exception.FunctionTimeoutError):
        fa._sliding_schedule(
            lambda _unit, _token: TimedOutCall("timed-out"), [1], "j", time.time())


def test_worker_is_released_only_after_call_id_snapshot(monkeypatch):
    state = {}
    monkeypatch.setattr(fa, "job_state", state)

    def spawn(unit, token):
        key = fa._launch_key("j", token)
        assert state[key] == "pending"

        def verify_release_order():
            assert state["j:subcalls"] == ["call-1"]
            assert state[key] == "ready"

        return FakeCall("call-1", {"unit": unit}, verify_release_order, token)

    results, _warnings, _device = fa._sliding_schedule(spawn, [1], "j", time.time())
    assert results == [{"unit": 1}]


def test_launch_gate_cancel_and_ready_are_fail_closed(monkeypatch):
    token = "abc"
    key = fa._launch_key("j", token)
    state = {key: "pending", "j:cancel": True}
    monkeypatch.setattr(fa, "job_state", state)

    with pytest.raises(fa._JobCancelled):
        fa._await_launch_gate("j", token)
    assert key not in state

    state[key] = "ready"
    state.pop("j:cancel")
    fa._await_launch_gate("j", token)
    assert key not in state


def test_launch_gate_dict_read_failure_does_not_release_ready_worker(monkeypatch):
    class RejectCancelRead(dict):
        def get(self, key, default=None):
            if key == "j:cancel":
                raise OSError("dict unavailable")
            return super().get(key, default)

    state = RejectCancelRead({fa._launch_key("j", "abc"): "ready"})
    monkeypatch.setattr(fa, "job_state", state)
    monkeypatch.setattr(fa, "LAUNCH_GATE_TIMEOUT_S", 0.0)

    with pytest.raises(RuntimeError, match="launch gate 超时"):
        fa._await_launch_gate("j", "abc")


def test_snapshot_write_failure_never_releases_worker(monkeypatch):
    class RejectSnapshot(dict):
        def __setitem__(self, key, value):
            if key == "j:subcalls":
                raise OSError("dict unavailable")
            super().__setitem__(key, value)

    state = RejectSnapshot()
    monkeypatch.setattr(fa, "job_state", state)
    calls = []

    def spawn(_unit, token):
        call = FakeCall("call-1", token=token)
        calls.append(call)
        return call

    with pytest.raises(RuntimeError, match="拒绝放行"):
        fa._sliding_schedule(spawn, [1], "j", time.time())

    assert calls[0].cancelled is True
    assert state[fa._launch_key("j", calls[0].token)] == "pending"


def test_cancel_flag_wins_over_failed_and_completed(monkeypatch):
    state = {"j": {"status": "running"}, "j:cancel": True, "j:subcalls": []}
    monkeypatch.setattr(fa, "job_state", state)

    try:
        raise fa._JobCancelled("cancelled")
    except fa._JobCancelled as exc:
        fa._fail_job("j", exc)
    assert state["j"]["status"] == "cancelled"

    assert fa._complete_job("j", [{"path": "partial"}], [], "fake") is False
    assert state["j"]["status"] == "cancelled"
    assert "outputs" not in state["j"]


def test_watchdog_only_flags_active_jobs_past_hard_timeout(monkeypatch):
    monkeypatch.setattr(fa, "JOB_TIMEOUT", 100)
    monkeypatch.setattr(fa, "WATCHDOG_GRACE_S", 10)
    active = {"status": "running", "queued_at": 1000}
    assert fa._stalled_reason(active, 1110) is None
    assert "watchdog" in fa._stalled_reason(active, 1110.01)
    assert fa._stalled_reason({**active, "status": "completed"}, 9999) is None
    assert fa._stalled_reason({"status": "running"}, 9999) is None


def test_cpu_fallback_is_visible_warning(monkeypatch):
    state = {"j": {"status": "running"}}
    monkeypatch.setattr(fa, "job_state", state)
    assert fa._complete_job("j", [], [], "CPU") is True
    assert state["j"]["render_device"] == "CPU"
    assert any("CPU" in warning for warning in state["j"]["warnings"])


def test_artifacts_remain_pending_until_every_output_is_fetched(monkeypatch):
    outputs = [{"volume_path": "_outputs/j/a.zip"},
               {"volume_path": "_outputs/j/b.json"}]
    state = {"j": {"status": "completed", "outputs": outputs,
                   "artifacts_pending": True}}
    monkeypatch.setattr(fa, "job_state", state)
    fa._mark_artifact_fetched("j", "_outputs/j/a.zip")
    assert state["j"]["artifacts_pending"] is True
    fa._mark_artifact_fetched("j", "_outputs/j/b.json")
    assert state["j"]["artifacts_pending"] is False
    assert state["j"]["fetched_at"] > 0


class _PollCall:
    """假 call:前 n 次 get 抛内置 TimeoutError(= modal 的"还没出结果"),之后返回。"""

    def __init__(self, object_id, misses, result):
        self.object_id, self._misses, self._result = object_id, misses, result

    def get(self, timeout=None):
        if self._misses > 0:
            self._misses -= 1
            raise TimeoutError()          # ⚠ 内置,不是 modal.exception.TimeoutError
        return self._result


def test_poll_treats_builtin_timeout_as_not_ready():
    """回归:modal poll_function 抛内置 TimeoutError,不能被当成任务失败。"""
    call = _PollCall("c1", misses=2, result={"unit": 1})
    assert fa._poll_call(call, 0) == (False, None)
    assert fa._poll_call(call, 0) == (False, None)
    assert fa._poll_call(call, 0) == (True, {"unit": 1})


def test_poll_propagates_function_timeout():
    """worker 自身超时必须冒泡:当成"还没好"会无限等下去。"""
    import modal.exception

    class Boom:
        def get(self, timeout=None):
            raise modal.exception.FunctionTimeoutError("worker 超时")

    with pytest.raises(modal.exception.FunctionTimeoutError):
        fa._poll_call(Boom(), 0)


def test_schedule_completes_when_calls_poll_busy(monkeypatch):
    """整条调度链:所有单元先"未就绪"再完成,不得把 job 打成 failed。"""
    state = {}
    monkeypatch.setattr(fa, "job_state", state)
    made = []

    def spawn(unit, _token):
        c = _PollCall(f"c{unit}", misses=2, result={"unit": unit, "warnings": [], "device": "L40S"})
        made.append(c)
        return c

    results, _w, device = fa._sliding_schedule(spawn, [1, 2, 3], "j", time.time())
    assert sorted(r["unit"] for r in results) == [1, 2, 3]
    assert device == "L40S"
