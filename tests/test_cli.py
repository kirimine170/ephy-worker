"""Exercise the actual CLI process without external network services."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


def fixture_config(tmp_path):
    path = tmp_path / "設定 空白.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "search": {"base_url": "http://127.0.0.1:9", "engine": "fixture"},
                "model_profiles": {
                    "test": {
                        "family": "qwen",
                        "base_url": "http://127.0.0.1:9/v1",
                        "model_id": "fixture",
                        "output_mode": "tool",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_help_and_missing_profile(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "ephy_worker", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and "research" in result.stdout and "doctor" in result.stdout
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ephy_worker",
            "research",
            "--config",
            str(fixture_config(tmp_path)),
            "--profile",
            "missing",
            "--question",
            "public test",
            "--output-dir",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(
    os.name == "nt", reason="Parent-generated POSIX SIGINT fixture；Windows console Ctrl+C remains unverified"
)
def test_real_cli_sigint_saves_cancelled_and_reaps_process(tmp_path):
    config = fixture_config(tmp_path)
    output = tmp_path / "成果物 空白"
    code = """
import asyncio
from ephy_worker.research import ResearchExecutor
from ephy_worker.cli import main
async def paused(self, urls):
    self.event("fixture_wait")
    await asyncio.Future()
ResearchExecutor._execute = paused
raise SystemExit(main())
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            "research",
            "--config",
            str(config),
            "--profile",
            "test",
            "--question",
            "公開の試験",
            "--output-dir",
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events = list(output.glob("*/events.jsonl")) if output.exists() else []
            if events and "fixture_wait" in events[0].read_text(encoding="utf-8"):
                break
            assert process.poll() is None
            time.sleep(0.05)
        else:
            pytest.fail("CLI did not enter fixture wait")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 130, (stdout, stderr)
        result = json.loads(stdout)
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        assert report["state"] == "cancelled" and report["stop_reason"] == "cancelled"
        assert not report["claims"]
        assert report["metrics"]["requests"] == {
            "search_requests": 0,
            "fetch_requests": 0,
            "model_requests": 0,
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
