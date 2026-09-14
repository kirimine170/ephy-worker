"""Per-job UTF-8 artifacts outside Git，never reuse an existing job directory."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .schema import Report, utc_now


def output_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    for parent in (root, *root.parents):
        if (parent / ".git").exists():
            raise ValueError("output_dir must be outside every Git checkout")
    return root


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            handle.write(text)
            handle.flush()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JobStore:
    def __init__(self, root: str | Path, job_id: str | None = None):
        root = output_root(root)
        self.job_id = job_id or f"research-{uuid4().hex}"
        if not self.job_id.replace("-", "").isalnum():
            raise ValueError("invalid job ID")
        self.directory = root / self.job_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.seq = 0

    def event(self, kind: str, **details) -> None:
        self.seq += 1
        event = {
            "schema_version": "0.1",
            "job_id": self.job_id,
            "seq": self.seq,
            "occurred_at": utc_now(),
            "type": kind,
            **details,
        }
        path = self.directory / "events.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            if os.name == "posix":
                os.chmod(path, 0o600)
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def status(self, report: Report) -> None:
        atomic_write(
            self.directory / "status.json",
            json.dumps(
                {
                    "schema_version": "0.1",
                    "job_id": report.job_id,
                    "state": report.state,
                    "stop_reason": report.stop_reason,
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def save(self, report: Report) -> None:
        from .report import render_markdown

        # JSON is canonical．Hash metadata is separate to avoid a circular hash．
        payload = report.model_dump_json(indent=2) + "\n"
        markdown = render_markdown(report)
        atomic_write(self.directory / "report.json", payload)
        atomic_write(self.directory / "report.md", markdown)
        atomic_write(
            self.directory / "metrics.json", json.dumps(report.metrics, ensure_ascii=False, indent=2) + "\n"
        )
        atomic_write(
            self.directory / "artifacts.json",
            json.dumps(
                {
                    "job_id": report.job_id,
                    "sha256": {
                        "report.json": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                        "report.md": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                    },
                },
                indent=2,
            )
            + "\n",
        )
        self.status(report)
