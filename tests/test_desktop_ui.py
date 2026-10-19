"""Display contracts that must stay honest independently of the Tk display."""

import json
from unittest.mock import Mock

import pytest

from ephy_worker.desktop.app import (
    EXAMPLE_PROMPT,
    HEALTH_LABELS,
    KARTE_SCOPE,
    UNAVAILABLE,
    WorkerConsole,
    build_identifier,
    event_line,
    execution_error_text,
    format_bytes,
    format_elapsed,
    format_number,
    karte_status_text,
)
from ephy_worker.desktop.config import DesktopConfig
from ephy_worker.desktop.inspection import InspectionError
from ephy_worker.desktop.records import InspectionRequest


def inspection_widgets(app):
    app._health_request = InspectionRequest()
    app._health_state = "unchecked"
    app._health_checked_at = app._health_code = ""
    app._records_window = None
    for name in ("health_label", "health_button", "karte_root_value", "records_button", "karte_badge"):
        setattr(app, name, Mock())


def test_bundle_identifier_never_displays_private_or_malformed_metadata(tmp_path):
    metadata = tmp_path / "build-info.json"
    assert build_identifier(metadata) == "build unavailable"
    metadata.write_text('{"build_id":"20261009-v3"}', encoding="utf-8")
    assert build_identifier(metadata) == "20261009-v3"
    for invalid in ('[]', '{"build_id":"C:\\\\Users\\\\private"}', '{"build_id":42}', 'bad json'):
        metadata.write_text(invalid, encoding="utf-8")
        assert build_identifier(metadata) == "build unavailable"


def test_karte_status_explicitly_limits_saved_claim_to_host_observation():
    assert karte_status_text({"state": "saved"}) == "Karte ホスト観測：保存済み"
    assert "未確認" in karte_status_text({"state": "unsaved", "pending": 1})
    assert "未確認" in karte_status_text({"state": "unexpected"})
    assert KARTE_SCOPE == "開始・終了のみ／モデル自己申告・生成本文は未保存"


def test_unavailable_measurements_never_look_like_zero():
    for missing in (None, "unknown", float("nan"), float("inf"), True):
        assert format_number(missing, "%") == UNAVAILABLE
        assert format_bytes(missing) == UNAVAILABLE
        assert format_elapsed(missing) == UNAVAILABLE
    assert format_number(0, "%", 1) == "0.0%"
    assert format_bytes(0) == "0 MiB"


def test_metric_units_and_process_cpu_are_preserved():
    assert format_bytes(8 * 1024**3) == "8.0 GiB"
    assert format_bytes(512 * 1024**2) == "512 MiB"
    assert format_number(230.5, "%", 1) == "230.5%"
    assert format_number(18.36, " tok/s", 1) == "18.4 tok/s"
    assert format_elapsed(125.9) == "02:05"


def test_event_display_excludes_raw_tool_arguments_and_results():
    rendered = event_line({"at": 12, "kind": "event", "type": "tool_start", "tool_name": "read",
                           "message": "tool_start",
                           "arguments": {"private": "SECRET"}, "result": "PRIVATE CONTENT"})
    assert "00:12" in rendered
    assert "read" in rendered
    assert "SECRET" not in rendered
    assert "PRIVATE" not in rendered
    assert "tool_start  ·  read" in rendered
    assert "governance_ack" in event_line({"type": "tool_execution_end", "message": "tool_execution_end",
                                           "tool_name": "governance_ack"})


def test_live_input_requires_an_explicit_config_before_calling_worker():
    app = WorkerConsole.__new__(WorkerConsole)
    app.controller = Mock(active=False)
    app._submitting = app._closing = False
    app.prompt = Mock()
    app.prompt.get.return_value = "Read the public repository"
    app.demo = Mock()
    app.demo.get.return_value = False
    app.config = None
    app.notice = Mock()
    assert app._start() is False
    app.controller.start.assert_not_called()


def test_duplicate_ui_submission_does_not_reach_controller():
    app = WorkerConsole.__new__(WorkerConsole)
    app.controller = Mock(active=True)
    app._submitting = app._closing = False
    assert app._start() is False
    app.controller.start.assert_not_called()


def test_close_waits_for_worker_cleanup_before_destroying_window():
    app = WorkerConsole.__new__(WorkerConsole)
    app.controller = Mock(active=True)
    app.controller.close.return_value = False
    app.root = Mock()
    app.notice = Mock()
    app._closing = False
    app.config = None
    inspection_widgets(app)
    app._on_close(confirm=False)
    assert app._closing is True
    app.controller.close.assert_called_once()
    app.root.destroy.assert_not_called()


@pytest.fixture
def config_app():
    app = WorkerConsole.__new__(WorkerConsole)
    app.controller = Mock(active=False)
    app._submitting = app._closing = False
    app.config = None
    app.demo = Mock()
    app.demo.get.return_value = False
    for name in ("config_label", "config_summary", "timeout_label", "notice", "prompt",
                 "mode_badge", "banner", "run_button"):
        setattr(app, name, Mock())
    app.prompt.get.return_value = "Synthetic repository input"
    inspection_widgets(app)
    return app


def test_loaded_timeout_display_tracks_mode_and_rejects_stale_invalid_configuration(config_app, tmp_path):
    app = config_app
    app._mode_changed()
    assert app.timeout_label.configure.call_args.kwargs["text"] == "実行制限：未設定（上限 1200 秒）"
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"repository_path": "repository", "timeout_seconds": 600}), encoding="utf-8")
    assert app.load_config(path)
    assert app.timeout_label.configure.call_args.kwargs["text"] == "実行制限：600 秒（上限 1200 秒）"
    app.controller.start.assert_not_called()
    app.demo.get.return_value = True
    app._mode_changed()
    assert app.timeout_label.configure.call_args.kwargs["text"] == "デモ実行制限：300 秒（固定）"
    app.demo.get.return_value = False
    app._mode_changed()
    assert app.timeout_label.configure.call_args.kwargs["text"] == "実行制限：600 秒（上限 1200 秒）"
    path.write_text(json.dumps({"repository_path": "repository", "timeout_seconds": "600"}), encoding="utf-8")
    assert not app.load_config(path)
    assert app.config is None
    assert app.timeout_label.configure.call_args.kwargs["text"] == "実行制限：未設定（上限 1200 秒）"
    assert not app._start()
    app.controller.start.assert_not_called()
    app.demo.get.return_value = True
    assert not app.load_config(path)
    assert app.timeout_label.configure.call_args.kwargs["text"] == "デモ実行制限：300 秒（固定）"


@pytest.fixture
def deferred_inspection(monkeypatch):
    pending = []

    def thread(*, target, **_kwargs):
        pending.append(target)
        return Mock()

    monkeypatch.setattr("ephy_worker.desktop.records.threading.Thread", thread)
    return pending


@pytest.mark.parametrize("state", ["ready", "starting", "unavailable", "mismatch", "error"])
def test_manual_health_check_state_and_duplicate_guard(config_app, deferred_inspection, monkeypatch, state):
    app = config_app
    app.config = DesktopConfig(repository_path=".")
    probe = Mock(return_value={"state": state, "checked_at": "2026-10-10T00:00:00Z", "code": "health_test"})
    monkeypatch.setattr("ephy_worker.desktop.app.probe_strata", probe)
    app._poll_inspections()
    probe.assert_not_called()
    assert "unchecked" in app.health_label.configure.call_args.kwargs["text"]
    assert app._check_connection()
    assert app._health_state == "checking"
    assert not app._check_connection()
    probe.assert_not_called()
    app.health_button.state.assert_called_with(["disabled"])
    deferred_inspection.pop(0)()
    assert app._health_state == "checking"
    app._poll_inspections()
    assert app._health_state == state
    assert HEALTH_LABELS[state] in app.health_label.configure.call_args.kwargs["text"]
    assert "2026-10-10T00:00:00Z" in app.health_label.configure.call_args.kwargs["text"]
    probe.assert_called_once_with(app.config)
    app.controller.start.assert_not_called()
    app.controller.cancel.assert_not_called()
    app.prompt.delete.assert_not_called()
    app.prompt.insert.assert_not_called()
    app.karte_badge.configure.assert_not_called()


@pytest.mark.parametrize("valid", [True, False])
def test_configuration_switch_invalidates_health_and_closes_records(config_app, deferred_inspection,
                                                                   monkeypatch, tmp_path, valid):
    app = config_app
    app.config = DesktopConfig(repository_path=".", model_id="old")
    probe = Mock(return_value={"state": "ready", "checked_at": "OLD", "code": "health_ready"})
    monkeypatch.setattr("ephy_worker.desktop.app.probe_strata", probe)
    assert app._check_connection()
    viewer = Mock()
    app._records_window = viewer
    viewer.close.side_effect = app._records_closed
    config_path = tmp_path / "new.json"
    config_path.write_text(json.dumps({"repository_path": ".", "model_id": "new"}) if valid else "bad json",
                           encoding="utf-8")
    assert app.load_config(config_path) is valid
    viewer.close.assert_called_once()
    assert app._records_window is None
    assert app._health_state == "unchecked"
    # The old bounded request retains its slot until it finishes, avoiding duplicate I/O.
    assert not app._check_connection()
    deferred_inspection.pop(0)()
    app._poll_inspections()
    assert app._health_state == "unchecked"
    assert "OLD" not in app.health_label.configure.call_args.kwargs["text"]
    assert app._health_checked_at == ""
    app.karte_root_value.set.assert_called_with("未設定")
    app.controller.start.assert_not_called()
    app.karte_badge.configure.assert_not_called()
    if valid:
        assert app._check_connection()


def test_failed_health_clears_previous_success_without_leaking_exception(config_app, deferred_inspection,
                                                                        monkeypatch):
    app = config_app
    app.config = DesktopConfig(repository_path=".")
    app._health_state, app._health_checked_at = "ready", "OLD"
    monkeypatch.setattr("ephy_worker.desktop.app.probe_strata", Mock(side_effect=RuntimeError("PRIVATE")))
    assert app._check_connection()
    assert app._health_checked_at == ""
    deferred_inspection.pop(0)()
    app._poll_inspections()
    assert app._health_state == "error"
    assert "inspection_failed" in app.health_label.configure.call_args.kwargs["text"]
    assert "PRIVATE" not in app.health_label.configure.call_args.kwargs["text"]


def test_close_invalidates_inspection_before_waiting_for_worker(config_app, deferred_inspection, monkeypatch):
    app = config_app
    app.root = Mock()
    app.config = DesktopConfig(repository_path=".")
    monkeypatch.setattr("ephy_worker.desktop.app.probe_strata",
                        Mock(return_value={"state": "ready", "checked_at": "OLD"}))
    assert app._check_connection()
    app.controller.active = True
    app.controller.close.return_value = False
    viewer = Mock()
    app._records_window = viewer
    viewer.close.side_effect = app._records_closed
    app._on_close(confirm=False)
    assert app._health_request.closed
    viewer.close.assert_called_once()
    deferred_inspection.pop(0)()
    app._poll_inspections()
    assert app._health_state == "unchecked"
    assert not app._check_connection()
    app.root.destroy.assert_not_called()
    app.controller.start.assert_not_called()


def test_record_window_uses_effective_root_preserves_badge_and_rejects_duplicate(config_app, monkeypatch, tmp_path):
    app = config_app
    app.root = Mock()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"repository_path": ".", "karte_root": "records", "karte_binary": "karte.exe"}),
                    encoding="utf-8")
    window = Mock()
    factory = Mock(return_value=window)
    probe = Mock()
    monkeypatch.setattr("ephy_worker.desktop.app.RecordsWindow", factory)
    monkeypatch.setattr("ephy_worker.desktop.app.probe_strata", probe)
    assert app.load_config(path)
    app.karte_root_value.set.assert_called_with(str((tmp_path / "records").resolve()))
    assert app._open_records()
    assert not app._open_records()
    factory.assert_called_once_with(app.root, app.config, on_close=app._records_closed)
    window.window.lift.assert_called_once()
    app.controller.start.assert_not_called()
    app.controller.cancel.assert_not_called()
    app.karte_badge.configure.assert_not_called()
    app.prompt.delete.assert_not_called()
    probe.assert_not_called()


def test_record_open_error_is_honest_and_does_not_dispatch(config_app, monkeypatch):
    app = config_app
    app.root = Mock()
    app.config = DesktopConfig(repository_path=".", karte_root="records", karte_binary="karte")
    monkeypatch.setattr("ephy_worker.desktop.app.RecordsWindow",
                        Mock(side_effect=InspectionError("karte_cli_missing")))
    assert not app._open_records()
    assert "karte_cli_missing" in app.notice.set.call_args.args[0]
    assert app._records_window is None
    app.karte_badge.configure.assert_not_called()
    app.controller.start.assert_not_called()


@pytest.mark.parametrize("code,hint", [("model_endpoint_unavailable", "接続確認"),
                                      ("model_endpoint_loading", "読込み完了"),
                                      ("path_directory", "具体的な通常ファイル")])
def test_execution_failure_hint_preserves_original_error(code, hint):
    original = f"navigation or provider failed ({code})"
    text = execution_error_text(original)
    assert original in text
    assert hint in text
    assert execution_error_text("other failure") == "実行エラー: other failure"


def test_new_window_example_names_regular_files_only():
    assert "README.md" in EXAMPLE_PROMPT
    assert "pyproject.toml" in EXAMPLE_PROMPT
    assert "通常ファイル専用" in EXAMPLE_PROMPT
    assert "ディレクトリーの読取りは未対応" in EXAMPLE_PROMPT


def test_directory_failure_unlocks_input_and_only_explicit_start_dispatches_new_task(config_app, monkeypatch):
    app = config_app
    app.config = DesktopConfig(repository_path=".", timeout_seconds=600)
    app._last_run_id = app._previous_state = None
    app._displayed_text = app._displayed_events = ""
    for name in ("state_badge", "run_label", "config_button", "demo_toggle", "cancel_button",
                 "detach_button", "reconnect_button"):
        setattr(app, name, Mock())
    app.values = {name: Mock() for name in ("elapsed", "input_tokens", "output_tokens", "rate", "turns", "tools")}
    probe = Mock()
    monkeypatch.setattr("ephy_worker.desktop.app.probe_strata", probe)
    failure = "navigation guard rejected tool access (path_directory)"
    snapshot = {"state": "failed", "error": failure, "karte": {"state": "saved"}}
    app._render(snapshot)
    app._render(snapshot)
    app.prompt.configure.assert_called_with(state="normal")
    app.run_button.state.assert_called_with(["!disabled"])
    assert failure in app.notice.set.call_args.args[0]
    assert "新しいタスク" in app.notice.set.call_args.args[0]
    app.controller.start.assert_not_called()
    app.prompt.delete.assert_not_called()
    app.prompt.insert.assert_not_called()
    app.prompt.get.return_value = "README.md と pyproject.toml を読んでください．"
    app.controller.start.return_value = True
    app.controller.snapshot.return_value = {"state": "starting"}
    assert app._start()
    app.controller.start.assert_called_once_with(app.config, app.prompt.get.return_value, demo=False)
    probe.assert_not_called()


@pytest.mark.parametrize("settings,expected", [({}, 240), ({"timeout_seconds": 1}, 1),
                                             ({"timeout_seconds": 1200}, 1200)])
def test_timeout_display_uses_validated_json_value(config_app, tmp_path, settings, expected):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"repository_path": "repository", **settings}), encoding="utf-8")
    assert config_app.load_config(path)
    assert config_app.timeout_label.configure.call_args.kwargs["text"] == (
        f"実行制限：{expected} 秒（上限 1200 秒）"
    )


@pytest.mark.parametrize("state", ["active", "submitting", "closing", "idle"])
def test_execution_locks_configuration_controls(config_app, state):
    app = config_app
    app.controller.active = state == "active"
    app._submitting = state == "submitting"
    app._closing = state == "closing"
    app._last_run_id = app._previous_state = None
    app._displayed_text = app._displayed_events = ""
    for name in ("state_badge", "karte_badge", "run_label", "config_button", "demo_toggle",
                 "cancel_button", "detach_button", "reconnect_button"):
        setattr(app, name, Mock())
    app.values = {name: Mock() for name in ("elapsed", "input_tokens", "output_tokens", "rate", "turns", "tools")}
    app._render({"state": "idle"})
    expected = ["!disabled"] if state == "idle" else ["disabled"]
    app.run_button.state.assert_called_once_with(expected)
    app.config_button.state.assert_called_once_with(expected)
    app.demo_toggle.state.assert_called_once_with(expected)
    app.prompt.configure.assert_called_once_with(state="normal" if state == "idle" else "disabled")


@pytest.mark.parametrize("active,closing", [(True, False), (False, True)])
def test_running_or_closing_ui_cannot_replace_configuration(config_app, tmp_path, active, closing):
    app = config_app
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"repository_path": "repository", "timeout_seconds": 600}), encoding="utf-8")
    assert app.load_config(path)
    initial = app.config
    app.controller.active, app._closing = active, closing
    path.write_text(json.dumps({"repository_path": "repository", "timeout_seconds": 1}), encoding="utf-8")
    assert not app.load_config(path)
    assert app.config is initial
    assert app.timeout_label.configure.call_args.kwargs["text"] == "実行制限：600 秒（上限 1200 秒）"
