"""Display contracts that must stay honest independently of the Tk display."""

from unittest.mock import Mock

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
