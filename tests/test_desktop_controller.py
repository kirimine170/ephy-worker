"""Lifecycle checks use synthetic adapters; the demo separately exercises the worker."""

import asyncio
import threading
import time
from types import SimpleNamespace

from ephy_worker.desktop import DesktopConfig, DesktopController
from ephy_worker.desktop.controller import MAX_EVENTS, MAX_TEXT


def _wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("controller did not reach expected state")


def _result(status="succeeded", failure=None, changed_files=None):
    return SimpleNamespace(
        result=SimpleNamespace(status=status, failure=failure), changed_files=changed_files or [],
    )


class ControlledAdapter:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.observe = None
        self.cancelled = False

    async def execute(self, config, prompt, run_id, cancel_event, observer, process_observer, *, demo=False):
        self.calls += 1
        self.observe = observer
        observer({"type": "turn_start"})
        self.started.set()
        while not self.release.is_set():
            if cancel_event.is_set():
                self.cancelled = True
                return _result("cancelled"), config.artifact_root / run_id
            await asyncio.sleep(0.005)
        return _result(), config.artifact_root / run_id


def _config(tmp_path):
    return DesktopConfig(repository_path=tmp_path / "source", artifact_root=tmp_path / "runs")


def _delta(text, index=0):
    return {"type": "message_update", "assistantMessageEvent": {
        "type": "text_delta", "contentIndex": index, "delta": text,
    }}


def test_start_reserves_immediately_and_close_waits_for_cancel(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    assert controller.start(_config(tmp_path), "Inspect README")
    assert not controller.start(_config(tmp_path), "Duplicate")
    assert controller.active
    assert controller.close() is False
    _wait(lambda: not controller.active)
    assert controller.close() is True
    assert not controller.start(_config(tmp_path), "After close")
    assert adapter.calls == 1
    assert adapter.cancelled
    assert controller.snapshot()["state"] == "cancelled"


def test_detach_reconnect_accumulates_without_second_submission(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    assert controller.start(_config(tmp_path), "Inspect README")
    _wait(adapter.started.is_set)
    adapter.observe(_delta("first"))
    run_id = controller.snapshot()["run_id"]
    controller.disconnect()
    adapter.observe(_delta(" second"))
    assert controller.drain() == []
    assert controller.snapshot()["text"] == "first"
    assert controller.snapshot()["state"] == "disconnected"
    controller.reconnect()
    restored = controller.snapshot()
    assert restored["state"] == "reconnecting"
    assert restored["text"] == "first second"
    assert restored["run_id"] == run_id
    assert controller.snapshot()["state"] == "running"
    adapter.release.set()
    _wait(lambda: not controller.active)
    assert adapter.calls == 1
    assert controller.snapshot()["state"] == "completed"


def test_cancel_keeps_final_text_and_disconnected_terminal_state(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    controller.start(_config(tmp_path), "Inspect README")
    _wait(adapter.started.is_set)
    adapter.observe(_delta("partial"))
    controller.disconnect()
    assert controller.cancel()
    _wait(lambda: not controller.active)
    assert controller.snapshot()["execution_state"] == "cancelled"
    assert not controller.cancel()
    controller.reconnect()
    assert controller.snapshot()["text"] == "partial"
    assert controller.snapshot()["state"] == "cancelled"


def test_projection_uses_content_order_and_final_authoritative_text(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    controller.start(_config(tmp_path), "Inspect README")
    _wait(adapter.started.is_set)
    adapter.observe({"type": "message_start", "message": {"role": "assistant"}})
    adapter.observe(_delta("B", 1))
    adapter.observe(_delta("A", 0))
    adapter.observe({"type": "message_update", "assistantMessageEvent": {
        "type": "thinking_delta", "delta": "PRIVATE THOUGHT",
    }})
    assert controller.snapshot()["text"] == "AB"
    assert controller.snapshot()["input_tokens"] is None
    adapter.observe({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Authoritative"},
        {"type": "thinking", "thinking": "PRIVATE THOUGHT"}], "usage": {"input": 17, "output": 3},
    }})
    adapter.observe({"type": "tool_execution_start", "toolName": "read", "args": {"path": "PRIVATE PATH"}})
    adapter.observe({"type": "tool_execution_end", "toolName": "read", "result": "PRIVATE RESULT"})
    snapshot = controller.snapshot()
    assert snapshot["text"] == "Authoritative"
    assert (snapshot["input_tokens"], snapshot["output_tokens"]) == (17, 3)
    assert snapshot["tokens_per_second"] is None
    assert "PRIVATE" not in str(snapshot)
    adapter.release.set()
    _wait(lambda: not controller.active)


def test_zero_usage_placeholder_is_unavailable_and_queues_are_bounded(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    controller.start(_config(tmp_path), "Inspect README")
    _wait(adapter.started.is_set)
    adapter.observe({"type": "message_end", "message": {
        "role": "assistant", "usage": {"input": 0, "output": 0}, "stopReason": "error",
    }})
    assert controller.snapshot()["input_tokens"] is None
    for index in range(MAX_EVENTS + 30):
        adapter.observe({"type": "tool_execution_start", "toolName": "read"})
    adapter.observe(_delta("x" * (MAX_TEXT + 1)))
    assert len(controller.snapshot()["text"]) == MAX_TEXT
    assert len(controller.snapshot()["events"]) == MAX_EVENTS
    assert len(controller.drain()) == MAX_EVENTS
    adapter.release.set()
    _wait(lambda: not controller.active)


def test_invalid_prompt_does_not_submit(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    assert not controller.start(_config(tmp_path), " ")
    assert not controller.start(_config(tmp_path), "x" * 18001)
    assert adapter.calls == 0
    assert controller.snapshot()["state"] == "failed"


def test_worker_failure_and_mutation_are_not_reported_completed(tmp_path):
    class FailedAdapter:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("private diagnostic")

    controller = DesktopController(adapter=FailedAdapter())
    controller.start(_config(tmp_path), "Inspect README")
    _wait(lambda: not controller.active)
    assert controller.snapshot()["state"] == "failed"
    assert controller.snapshot()["error"] == "RuntimeError"

    class MutatingAdapter:
        async def execute(self, *args, **kwargs):
            return _result(changed_files=["unexpected"]), tmp_path

    controller = DesktopController(adapter=MutatingAdapter())
    controller.start(_config(tmp_path), "Inspect README")
    _wait(lambda: not controller.active)
    assert controller.snapshot()["state"] == "failed"
    assert "Read-only" in controller.snapshot()["error"]


def test_late_cancel_does_not_relabel_finished_worker_as_cancelled(tmp_path):
    class AlreadyFinishedAdapter:
        async def execute(self, config, prompt, run_id, cancel_event, observer, process_observer, *, demo=False):
            await cancel_event.wait()
            # Simulate cancellation arriving after worker result was committed.
            return _result(), config.artifact_root / run_id

    controller = DesktopController(adapter=AlreadyFinishedAdapter())
    controller.start(_config(tmp_path), "Inspect README")
    assert controller.cancel()
    _wait(lambda: not controller.active)
    assert controller.snapshot()["state"] == "completed"
    assert any(event.get("type") == "late_cancel" for event in controller.snapshot()["events"])


def test_real_worker_demo_does_not_touch_source_or_karte(tmp_path):
    source = tmp_path / "not-a-repository"
    source.mkdir()
    config = DesktopConfig(repository_path=source, artifact_root=tmp_path / "demo-runs",
                           karte_binary=tmp_path / "not-run.exe", karte_root=tmp_path / "not-created")
    controller = DesktopController()
    assert controller.start(config, "Inspect repository", demo=True)
    _wait(lambda: not controller.active, timeout=20)
    snapshot = controller.snapshot()
    assert snapshot["state"] == "completed", snapshot["error"]
    assert snapshot["demo"] is True
    assert snapshot["worker_pid"] is None
    assert snapshot["karte"]["state"] == "off"
    assert snapshot["tokens_per_second"] is None
    assert "SYNTHETIC DEMO" in snapshot["text"]
    assert list(source.iterdir()) == []
    assert not config.karte_root.exists()
    assert (config.artifact_root / snapshot["run_id"] / "result.json").is_file()


def test_governance_preparation_and_ack_are_distinct_display_states(tmp_path):
    adapter = ControlledAdapter()
    controller = DesktopController(adapter=adapter)
    controller.start(_config(tmp_path), "Inspect README")
    _wait(adapter.started.is_set)
    adapter.observe({"type": "ephy_navigation_guard_ready", "version": 1})
    assert controller.snapshot()["governance"] == "prepared"
    adapter.observe({"type": "ephy_desktop_governance_envelope", "nonce": "do-not-display"})
    assert controller.snapshot()["governance"] == "ack_pending"
    adapter.observe({"type": "tool_execution_start", "toolName": "governance_ack", "args": {"private": True}})
    assert controller.snapshot()["governance"] == "ack_pending"
    adapter.observe({"type": "ephy_desktop_governance_verified", "version": 1})
    snapshot = controller.snapshot()
    assert snapshot["governance"] == "ack_verified"
    assert any(event.get("tool_name") == "governance_ack" for event in snapshot["events"])
    assert "do-not-display" not in str(snapshot) and "private" not in str(snapshot)
    adapter.release.set()
    _wait(lambda: not controller.active)
