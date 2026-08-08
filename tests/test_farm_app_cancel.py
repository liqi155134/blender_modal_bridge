"""取消调度回归测试:不启动 Modal call,只用假的 FunctionCall/Dict 验证状态机。"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modal_app"))
import farm_app as fa  # noqa: E402


class FakeCall:
    def __init__(self, object_id, result=None, before_get=None):
        self.object_id = object_id
        self._result = result or {"warnings": [], "device": "fake"}
        self._before_get = before_get

    def get(self):
        if self._before_get:
            self._before_get()
        return self._result


def test_cancel_before_fill_spawns_nothing(monkeypatch):
    state = {"j:cancel": True}
    spawned = []
    monkeypatch.setattr(fa, "job_state", state)

    with pytest.raises(fa._JobCancelled):
        fa._sliding_schedule(lambda unit: spawned.append(unit), [1, 2], "j", time.time())

    assert spawned == []
    assert state["j:filling"] is False
    assert state["j:subcalls"] == []


def test_cancel_during_window_never_returns_partial_success(monkeypatch):
    state = {}
    monkeypatch.setattr(fa, "job_state", state)
    monkeypatch.setattr(fa, "MAX_PARALLEL", 1)  # window=2,第三个单元留在 pending
    calls = []

    def spawn(unit):
        before_get = (lambda: state.__setitem__("j:cancel", True)) if unit == 1 else None
        call = FakeCall(f"call-{unit}", {"unit": unit}, before_get)
        calls.append(call)
        return call

    with pytest.raises(fa._JobCancelled):
        fa._sliding_schedule(spawn, [1, 2, 3], "j", time.time())

    assert [c.object_id for c in calls] == ["call-1", "call-2"]
    # call-1 已完成并从 inflight 移除;快照只保留仍需 cancel 的 call-2。
    assert state["j:subcalls"] == ["call-2"]
    assert state["j:filling"] is False


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
