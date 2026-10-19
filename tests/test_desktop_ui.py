"""Display contracts that must stay honest independently of the Tk display."""

import json
from unittest.mock import Mock

import pytest

from ephy_worker.desktop.app import (
    KARTE_SCOPE,
    UNAVAILABLE,
    WorkerConsole,
    build_identifier,
    event_line,
    format_bytes,
    format_elapsed,
    format_number,
    karte_status_text,
)


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
