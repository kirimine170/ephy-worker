"""Pytest probe for the report-JSON strict UTF-8 evaluation.

This module is loaded as an external pytest plugin.  It mutates only the
already-written report artifact, so each probe exercises the candidate test's
real read path rather than a checker-owned surrogate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ephy_worker.store import JobStore

MODE = os.environ.get("EPHY_REPORT_JSON_PROBE_MODE", "")
SENTINEL = "strict-utf8-😀"
TAVILY_FIXTURE_KEY = "tvly-fixture-not-a-real-key"
_original_save = JobStore.save


def _probed_save(self: JobStore, report: object) -> None:
    _original_save(self, report)
    report_path = Path(self.directory) / "report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["__encoding_probe__"] = SENTINEL
    if MODE == "semantic-negative":
        payload["state"] = "__probe_invalid_state__"
        payload["__secret_probe__"] = TAVILY_FIXTURE_KEY
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if MODE == "invalid-utf8":
        sentinel = SENTINEL.encode("utf-8")
        if sentinel not in encoded:
            raise RuntimeError("encoding probe sentinel was not serialized")
        encoded = encoded.replace(sentinel, b"strict-utf8-\xff", 1)
    report_path.write_bytes(encoded)


def pytest_configure() -> None:
    if MODE not in {"valid-utf8", "invalid-utf8", "semantic-negative"}:
        raise RuntimeError(f"unsupported EPHY_REPORT_JSON_PROBE_MODE: {MODE!r}")
    JobStore.save = _probed_save


def pytest_unconfigure() -> None:
    JobStore.save = _original_save
