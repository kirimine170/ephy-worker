import json
import multiprocessing
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker.desktop.karte import KarteObserver


class FakeCli:
    def __init__(self):
        self.calls = []
        self.mode = "saved"
        self.observer = None

    def __call__(self, argv, **kwargs):
        payload = json.loads(kwargs["input"])
        self.calls.append((argv, payload, kwargs))
        # Delivery must never precede durable storage.
        files = list(self.observer.spool.glob("*.json"))
        assert any(json.loads(p.read_text())["event_id"] == payload["event_id"] for p in files)
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        receipt = {
            "task_id": payload["task_id"], "sequence": 1,
            "recorded": True, "views_ready": self.mode != "views_pending",
        }
        envelope = {"value": receipt}
        if self.mode == "views_pending":
            envelope["error"] = "private fixture diagnostic"
        if self.mode == "mismatch":
            receipt["task_id"] = "another-task"
        if self.mode == "invalid":
            return SimpleNamespace(returncode=0, stdout="[]")
        return SimpleNamespace(returncode=int(self.mode == "views_pending"), stdout=json.dumps(envelope))


def observer(tmp_path, cli):
    binary = tmp_path / "karte-worklog.exe"
    binary.touch(exist_ok=True)
    root = tmp_path / "root"
    config = root / ".mdsys/worklog/v1/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"enabled":true}')
    result = KarteObserver(binary, root, tmp_path / "spool", runner=cli)
    cli.observer = result
    return result


def test_saved_receipt_removes_queue_and_fixed_argv_contains_no_shell(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    result = subject.record("local-run-1", "started", {"prompt": "private input"})
    assert result["state"] == "saved"
    assert result["pending"] == 0
    assert result["recorded"] is True and result["views_ready"] is True
    argv, payload, options = cli.calls[0]
    assert argv == [str(subject.binary), "--root", str(subject.root), "--observer", "start"]
    assert options["shell"] is False
    assert options["timeout"] == 5.0
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "private input" not in json.dumps(payload)
    assert "local-run-1" not in json.dumps(payload)


def test_timeout_restart_reuses_exact_event_id_and_input(tmp_path):
    cli = FakeCli()
    cli.mode = "timeout"
    subject = observer(tmp_path, cli)
    result = subject.record("run1", "started")
    assert result["state"] == "unsaved"
    assert result["pending"] == 1
    first = cli.calls[0][1]
    subject = observer(tmp_path, cli)
    cli.mode = "saved"
    assert subject.flush()["state"] == "saved"
    assert cli.calls[1][1] == first
    assert not list(subject.spool.glob("*.json"))


def test_append_retry_preserves_entire_serialized_input_across_restart(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    subject.record("run1", "started")
    cli.mode = "timeout"
    subject.record("run1", "failed", {"pid": 123, "is_error": True, "tool_name": "read"})
    first_argv, first_payload, first_options = cli.calls[-1]
    assert first_payload["outcome"] == "failed"
    assert first_payload["occurred_at"]
    subject = observer(tmp_path, cli)
    cli.mode = "saved"
    result = subject.flush()
    retry_argv, retry_payload, retry_options = cli.calls[-1]
    assert result["state"] == "saved"
    assert retry_argv == first_argv
    assert retry_payload == first_payload
    assert retry_options["input"] == first_options["input"]


def test_partial_receipt_survives_restart_and_never_discards_pending_event(tmp_path):
    cli = FakeCli()
    cli.mode = "views_pending"
    subject = observer(tmp_path, cli)
    result = subject.record("run1", "started")
    assert result["state"] == "saved_views_pending"
    assert result["recorded"] is True
    assert result["views_ready"] is False
    assert result["pending"] == 1
    assert "private fixture" not in json.dumps(result)
    first = cli.calls[0][1]
    subject = observer(tmp_path, cli)
    cli.mode = "timeout"
    assert subject.flush()["state"] == "saved_views_pending"
    cli.mode = "saved"
    assert subject.flush()["pending"] == 0
    assert all(call[1] == first for call in cli.calls)


def test_partial_start_receipt_does_not_hide_unsaved_terminal_event(tmp_path):
    cli = FakeCli()
    cli.mode = "views_pending"
    subject = observer(tmp_path, cli)
    assert subject.record("run1", "started")["state"] == "saved_views_pending"
    result = subject.record("run1", "succeeded")
    assert result["state"] == "unsaved"
    assert result["recorded"] is False
    assert result["pending"] == 2
    assert result["pending_views"] == 1
    assert result["pending_unsaved"] == 1
    assert all(call[0][-1] == "start" for call in cli.calls)
    cli.mode = "saved"
    assert subject.flush()["state"] == "saved"
    assert cli.calls[-1][1]["outcome"] == "paused"


def test_terminal_outcome_and_privacy_allowlist(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    subject.record("run1", "started")
    subject.record("run1", "tool_end", {
        "tool_name": "read", "is_error": False, "pid": 123,
        "prompt": "secret-prompt", "arguments": {"path": "secret-path"},
        "output": "secret-output", "error": "secret-error", "text": "secret-text",
    })
    payload = cli.calls[-1][1]
    assert payload["kind"] == "tool"
    assert "secret" not in json.dumps(payload)
    assert '"tool_name":"read"' in payload["text"]
    subject.record("run1", "succeeded")
    payload = cli.calls[-1][1]
    assert payload["kind"] == "finish"
    assert payload["outcome"] == "paused"
    assert "status" not in payload
    assert "verification are unconfirmed" in payload["text"]


def test_failed_and_cancelled_have_distinct_outcomes(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    for outcome in ("failed", "cancelled"):
        subject.record(outcome, "started")
        subject.record(outcome, outcome)
        assert cli.calls[-1][1]["outcome"] == outcome


def test_invalid_receipt_is_not_acknowledged(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    for mode in ("mismatch", "invalid"):
        cli.mode = mode
        result = subject.record("run1", "started")
        assert result["state"] == "unsaved"
        assert result["recorded"] is False
        assert list(subject.spool.glob("*.json"))


@pytest.mark.parametrize("override", [
    {"sequence": 0}, {"sequence": -1}, {"sequence": True}, {"sequence": "1"},
    {"recorded": "true"}, {"views_ready": "true"},
    {"recorded": False, "views_ready": True},
    {"recorded": False, "views_ready": False},
])
def test_ack_requires_typed_positive_sequence_and_both_true(tmp_path, override):
    cli = FakeCli()
    subject = observer(tmp_path, cli)

    def altered_receipt(argv, **kwargs):
        response = cli(argv, **kwargs)
        envelope = json.loads(response.stdout)
        envelope["value"].update(override)
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope))

    subject._runner = altered_receipt
    result = subject.record("run1", "started")
    assert result["state"] == "unsaved"
    assert result["recorded"] is False
    assert result["pending"] == 1
    assert len(list(subject.spool.glob("*.json"))) == 1


def test_bounded_queue_retains_previous_events_on_overflow(tmp_path):
    cli = FakeCli()
    cli.mode = "timeout"
    subject = observer(tmp_path, cli)
    subject.MAX_PENDING = 1
    subject.record("run1", "started")
    before = {p: p.read_bytes() for p in subject.spool.glob("*.json")}
    result = subject.record("run1", "failed")
    assert result["state"] == "failed"
    assert result["reason"] == "spool_full"
    assert {p: p.read_bytes() for p in subject.spool.glob("*.json")} == before


def test_disabled_adapter_does_not_write_or_invoke(tmp_path):
    def no_call(*args, **kwargs):
        raise AssertionError("disabled observer launched a command")

    subject = KarteObserver(None, None, tmp_path / "spool", runner=no_call)
    assert subject.record("run1", "started")["state"] == "off"
    assert subject.flush()["state"] == "off"
    assert not (tmp_path / "spool").exists()


def test_missing_root_is_not_initialized_and_spool_retains_event(tmp_path):
    binary = tmp_path / "karte-worklog.exe"
    binary.touch()
    root = tmp_path / "nonexistent"
    subject = KarteObserver(binary, root, tmp_path / "spool")
    result = subject.record("run1", "started")
    assert result["reason"] == "karte_root_not_initialized"
    assert result["pending"] == 1
    assert not root.exists()


def test_failed_durable_write_never_sends_event(tmp_path, monkeypatch):
    cli = FakeCli()
    subject = observer(tmp_path, cli)

    def disk_failure(*args):
        raise OSError("private-path: disk full")

    monkeypatch.setattr(subject, "_save", disk_failure)
    result = subject.record("run1", "started")
    assert result["state"] == "failed"
    assert result["reason"] == "spool_unavailable"
    assert "private-path" not in json.dumps(result)
    assert not cli.calls


def test_corrupt_spool_is_retained_and_never_delivered(tmp_path):
    cli = FakeCli()
    cli.mode = "timeout"
    subject = observer(tmp_path, cli)
    subject.record("run1", "started")
    path = next(subject.spool.glob("*.json"))
    data = json.loads(path.read_text())
    data["metadata"]["tool_name"] = "shell private-payload"
    path.write_text(json.dumps(data))
    calls = len(cli.calls)
    result = subject.flush()
    assert result["state"] == "failed"
    assert result["reason"] == "invalid_spool_retained"
    assert len(cli.calls) == calls
    assert path.exists()


def test_malformed_existing_spool_does_not_crash_new_record(tmp_path):
    cli = FakeCli()
    cli.mode = "timeout"
    subject = observer(tmp_path, cli)
    subject.record("run1", "started")
    path = next(subject.spool.glob("*.json"))
    data = json.loads(path.read_text())
    data["run_key"] = None
    path.write_text(json.dumps(data))
    result = subject.record("run2", "started")
    assert result["state"] == "failed"
    assert result["pending"] == 2
    assert path.exists()
    assert len(cli.calls) == 1


def _process_observer(base, runner):
    base = Path(base)
    return KarteObserver(base / "karte-worklog.exe", base / "root", base / "spool", runner=runner)


def _wait_for_file(path, timeout=15):
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic process marker was not created")
        time.sleep(0.02)


def _write_process_result(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def _hold_synthetic_cli(base, entered, release, output):
    def blocked(argv, **kwargs):
        _write_process_result(output, kwargs["input"])
        Path(entered).touch()
        _wait_for_file(release, timeout=25)
        raise subprocess.TimeoutExpired(argv, 5.0)

    _process_observer(base, blocked).record("process-A", "started")


def _enqueue_lifecycle_in_process(base, output):
    def never_send(*args, **kwargs):
        raise AssertionError("contending producer acquired an occupied delivery lease")

    subject = _process_observer(base, never_send)
    results = [subject.record("process-B", "started"), subject.record("process-B", "failed")]
    _write_process_result(output, results)


def _flush_in_process(base, output):
    calls = []

    def acknowledged(argv, **kwargs):
        payload = json.loads(kwargs["input"])
        calls.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"value": {
            "task_id": payload["task_id"], "sequence": len(calls),
            "recorded": True, "views_ready": True,
        }}))

    result = _process_observer(base, acknowledged).flush()
    _write_process_result(output, {"result": result, "calls": calls})


def _enqueue_once_in_process(base, run_id, maximum_count, maximum_bytes, ready, output):
    def never_send(*args, **kwargs):
        raise AssertionError("delivery should remain held by test parent")

    subject = _process_observer(base, never_send)
    subject.MAX_PENDING = maximum_count
    subject.MAX_BYTES = maximum_bytes
    _wait_for_file(ready)
    _write_process_result(output, subject.record(run_id, "started"))


def _stop_test_processes(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(5)


def test_multiprocess_delivery_contention_persists_both_edges_and_recovers_after_crash(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    context = multiprocessing.get_context("spawn")
    # File IPC deliberately avoids shared synchronization objects: terminating
    # the synthetic sender must not strand a multiprocessing Event/Queue lock.
    entered, release = tmp_path / "entered", tmp_path / "release"
    first_input = tmp_path / "first-input.json"
    producer_output, replay_output = tmp_path / "producer.json", tmp_path / "replay.json"
    holder = context.Process(target=_hold_synthetic_cli, args=(str(tmp_path), entered, release, first_input))
    producer = context.Process(target=_enqueue_lifecycle_in_process, args=(str(tmp_path), producer_output))
    restarted = context.Process(target=_flush_in_process, args=(str(tmp_path), replay_output))
    processes = []
    try:
        holder.start()
        processes.append(holder)
        _wait_for_file(entered)
        first_wire_input = json.loads(first_input.read_text())
        producer.start()
        processes.append(producer)
        _wait_for_file(producer_output, timeout=10)
        results = json.loads(producer_output.read_text())
        producer.join(5)
        assert producer.exitcode == 0
        assert holder.is_alive(), "producer waited for the slow CLI instead of durably enqueuing"
        assert all(result["state"] == "unsaved" and result["reason"] == "delivery_busy" for result in results)
        assert results[0]["event_id"] != results[1]["event_id"]
        assert [result["pending"] for result in results] == [2, 3]
        pending = [subject._load(path) for path in subject._paths()]
        assert len(pending) == 3
        assert {result["event_id"] for result in results} <= {item["event_id"] for item in pending}
        exact_inputs = [
            json.dumps(subject._payload(item)[1], ensure_ascii=False, separators=(",", ":"))
            for item in pending
        ]
        assert exact_inputs[0] == first_wire_input

        # Kill only this test's synthetic sender, while it owns the delivery
        # lease. Its queued event and both other process events must survive.
        holder.terminate()
        holder.join(5)
        assert not holder.is_alive()
        restarted.start()
        processes.append(restarted)
        _wait_for_file(replay_output, timeout=10)
        replay = json.loads(replay_output.read_text())
        restarted.join(5)
        assert restarted.exitcode == 0
        assert replay["calls"] == exact_inputs
        assert replay["result"]["state"] == "saved"
        assert replay["result"]["event_id"] == results[-1]["event_id"]
        assert replay["result"]["pending"] == 0
        assert not subject._paths()
    finally:
        release.touch()
        _stop_test_processes(processes)


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_multiprocess_producers_preserve_queue_capacity_and_existing_events(tmp_path, limit):
    cli = FakeCli()
    cli.mode = "timeout"
    subject = observer(tmp_path, cli)
    subject.record("seed", "started")
    before = {path: path.read_bytes() for path in subject._paths()}
    maximum_count = 2 if limit == "count" else 100
    maximum_bytes = 5 * 1024 * 1024 if limit == "count" else sum(map(len, before.values())) * 2 + 8
    context = multiprocessing.get_context("spawn")
    ready = tmp_path / "ready"
    outputs = [tmp_path / f"producer-{index}.json" for index in range(3)]
    processes = []
    try:
        with subject._exclusive():
            for index in range(3):
                process = context.Process(target=_enqueue_once_in_process, args=(
                    str(tmp_path), f"producer-{index}", maximum_count, maximum_bytes, ready, outputs[index],
                ))
                process.start()
                processes.append(process)
            ready.touch()
            for output in outputs:
                _wait_for_file(output)
            results = [json.loads(output.read_text()) for output in outputs]
            for process in processes:
                process.join(5)
                assert process.exitcode == 0
            accepted = [result for result in results if result["state"] == "unsaved"]
            rejected = [result for result in results if result["state"] == "failed"]
            assert len(accepted) == 1
            assert accepted[0]["reason"] == "delivery_busy"
            assert len(rejected) == 2
            assert all(result["reason"] == "spool_full" for result in rejected)
            files = subject._paths()
            assert len(files) == 2
            assert len(files) <= maximum_count
            assert sum(path.stat().st_size for path in files) <= maximum_bytes
            assert all(path.read_bytes() == raw for path, raw in before.items())
            stored_ids = {subject._load(path)["event_id"] for path in files}
            assert accepted[0]["event_id"] in stored_ids
    finally:
        ready.touch()
        _stop_test_processes(processes)


def test_record_receipt_targets_current_event_and_flush_cannot_reuse_previous_saved_status(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    assert subject.record("run1", "started")["state"] == "saved"
    other = observer(tmp_path, FakeCli())
    with other._exclusive():
        result = subject.record("run1", "failed")
        assert result["state"] == "unsaved"
        assert result["reason"] == "delivery_busy"
        assert result["pending"] == 1
        assert subject.flush()["state"] == "unsaved"
    final = subject.flush()
    assert final["event_id"] == result["event_id"]
    assert final["state"] == "saved"
    assert final["pending"] == 0
    assert cli.calls[-1][0][-1] == "append"


def test_post_snapshot_enqueue_cannot_make_delivery_result_falsely_saved(tmp_path):
    cli = FakeCli()
    subject = observer(tmp_path, cli)
    other_cli = FakeCli()
    other = observer(tmp_path, other_cli)
    original = subject._runner
    later = []

    def simultaneous_enqueue(argv, **kwargs):
        later.append(other.record("other-run", "started"))
        return original(argv, **kwargs)

    subject._runner = simultaneous_enqueue
    result = subject.record("run1", "started")
    assert later[0]["reason"] == "delivery_busy"
    assert result["state"] == "unsaved"
    assert result["event_id"] != later[0]["event_id"]
    assert result["pending"] == 1
    assert result["pending_unsaved"] == 1
