import json
from types import SimpleNamespace

import pytest

from ephy_worker.cli import main, parser
from ephy_worker.google_sheets_schema import SheetsConfig, SheetsResult


def test_cli_new_commands_and_existing_entrypoints():
    command_parser = parser()
    for command in ("doctor", "list-tabs", "read"):
        argv = ["google-sheets", command, "--config", "outside.yaml", "--source", "synthetic"]
        if command == "read":
            argv += ["--tab", "タスク", "--range", "A1:B2", "--output", "outside.json"]
        args = command_parser.parse_args(argv)
        assert args.command == "google-sheets" and args.sheets_command == command
    assert command_parser.parse_args(["doctor", "--config", "outside.yaml"]).command == "doctor"
    assert command_parser.parse_args(["research", "--config", "outside.yaml", "--profile", "q", "--question", "query",
                                     "--output-dir", "outside"]).command == "research"
    assert command_parser.parse_args(["coding", "run", "job.json"]).command == "coding"
    assert command_parser.parse_args(["manager", "workers", "--config", "outside.yaml"]).command == "manager"


def test_cli_never_logs_cell_body(tmp_path, monkeypatch, capsys):
    from ephy_worker.google_sheets_cli import run

    settings = SheetsConfig(sources={"synthetic": "sheet_fake"}, allowed_spreadsheet_ids=["sheet_fake"])
    output = tmp_path / "body.json"
    args = SimpleNamespace(sheets_command="read", source="synthetic", tab="タスク", range="A1", output=output)
    private_result = SheetsResult(source_id="synthetic", spreadsheet_id="sheet_fake", source_url="https://example.test",
                                 status="ok", diagnostic="connected", rows=[{"row": 1, "cells": [
                                     {"row": 1, "column": 1, "value": "PRIVATE CELL BODY 日本語"}]}])
    monkeypatch.setattr("ephy_worker.google_sheets_cli.SheetsAdapter.execute", lambda *a, **k: private_result)
    assert run(SimpleNamespace(google_sheets=settings), args) == 0
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert "PRIVATE CELL BODY 日本語" in output.read_text(encoding="utf-8")


def test_cli_missing_credential_exit_is_failure(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("google_sheets:\n  sources:\n    synthetic: sheet_fake\n  allowed_spreadsheet_ids: [sheet_fake]\n", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert main(["google-sheets", "doctor", "--config", str(config)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["diagnostic"] == "credential_missing"


def test_cli_sigterm_cancels_without_output_and_restores_handler(tmp_path, monkeypatch, capsys):
    import signal

    config = tmp_path / "config.yaml"
    config.write_text("google_sheets:\n  sources:\n    synthetic: sheet_fake\n  allowed_spreadsheet_ids: [sheet_fake]\n", encoding="utf-8")
    original_handler = signal.getsignal(signal.SIGTERM)

    def terminate(*args, **kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    monkeypatch.setattr("ephy_worker.google_sheets_cli.SheetsAdapter.execute", terminate)
    output = tmp_path / "cancelled.json"
    assert main(["google-sheets", "read", "--config", str(config), "--source", "synthetic", "--tab", "Tab",
                 "--range", "A1", "--output", str(output)]) == 130
    assert not output.exists()
    assert signal.getsignal(signal.SIGTERM) == original_handler
    assert "cancelled" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["list-tabs", "read"])
def test_cli_unknown_source_fails_before_adapter(tmp_path, monkeypatch, capsys, command):
    config = tmp_path / "config.yaml"
    config.write_text("google_sheets:\n  sources:\n    synthetic: sheet_fake\n  allowed_spreadsheet_ids: [sheet_fake]\n", encoding="utf-8")
    monkeypatch.setattr("ephy_worker.google_sheets_cli.SheetsAdapter", lambda *a: pytest.fail("adapter reached"))
    argv = ["google-sheets", command, "--config", str(config), "--source", "unknown"]
    if command == "read":
        argv += ["--tab", "Tab", "--range", "A1", "--output", str(tmp_path / "out.json")]
    assert main(argv) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "unknown_source"
