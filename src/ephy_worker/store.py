"""Per-job UTF-8 artifacts outside Git．Fresh jobs never reuse an existing directory；
`JobStore.open_existing` is the explicit re-open path for resuming a recorded job."""

from __future__ import annotations

import hashlib
import json
import os
import stat
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
        self.opened_existing = False

    @classmethod
    def open_existing(cls, root: str | Path, job_id: str) -> JobStore:
        """Re-open a recorded job directory without creating it．

        The directory must already exist；otherwise FileNotFoundError is raised．
        ``seq`` is restored from the events recorded in ``events.jsonl`` so that
        appended events continue the sequence．
        """
        root = output_root(root)
        if not job_id or not job_id.replace("-", "").isalnum():
            raise ValueError("invalid job ID")
        directory = root / job_id
        if not directory.is_dir():
            raise FileNotFoundError(f"job directory does not exist: {directory}")
        store = cls.__new__(cls)
        store.job_id = job_id
        store.directory = directory
        store.opened_existing = True
        store._ensure_safe_artifacts()
        store.seq = cls._count_events(directory / "events.jsonl", job_id)
        return store

    def _ensure_safe_artifacts(self) -> None:
        """Reject linked job paths before reading or writing canonical artifacts."""
        root = output_root(self.directory.parent)
        directory = self.directory
        if (
            directory.is_symlink()
            or directory.is_junction()
            or not directory.resolve().is_relative_to(root)
        ):
            raise ValueError("unsafe job path")
        if not directory.is_dir():
            raise FileNotFoundError(f"job directory does not exist: {directory}")
        git_marker = directory / ".git"
        if git_marker.exists() or git_marker.is_symlink() or git_marker.is_junction():
            raise ValueError("job directory must be outside every Git checkout")
        for name in ("events.jsonl", "status.json", "report.json", "report.md", "metrics.json", "artifacts.json"):
            artifact = directory / name
            if (
                artifact.is_symlink()
                or artifact.is_junction()
                or not artifact.resolve().is_relative_to(directory.resolve())
            ):
                raise ValueError("unsafe job path")
            try:
                info = artifact.lstat()
            except FileNotFoundError:
                continue
            # A hard link keeps the same inode while pointing outside this job.
            # Fail closed if the filesystem cannot report a single link.
            link_count = getattr(info, "st_nlink", None)
            if not stat.S_ISREG(info.st_mode) or type(link_count) is not int or link_count != 1:
                raise ValueError("unsafe job path")

    @staticmethod
    def _count_events(path: Path, job_id: str) -> int:
        if not path.exists():
            return 0
        highest = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("job_id") != job_id:
                    raise ValueError(f"event log mixes job IDs: {path}")
                seq = record.get("seq")
                if type(seq) is not int:
                    raise ValueError(f"event log has invalid seq: {path}")
                highest = max(highest, seq)
        return highest

    def event(self, kind: str, **details) -> None:
        self._ensure_safe_artifacts()
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
        self._ensure_safe_artifacts()
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

    def load_report(self) -> Report:
        """Re-read the canonical ``report.json`` of this job．

        Raises FileNotFoundError when the job never saved a report and ValueError
        when the stored payload is missing，corrupted，or belongs to another job．
        """
        self._ensure_safe_artifacts()
        path = self.directory / "report.json"
        if not path.is_file():
            raise FileNotFoundError(f"report does not exist: {path}")
        try:
            manifest = json.loads((self.directory / "artifacts.json").read_text(encoding="utf-8"))
            expected_hash = manifest["sha256"]["report.json"]
            if manifest["job_id"] != self.job_id or not isinstance(expected_hash, str):
                raise ValueError("report artifact manifest does not match job")
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                raise ValueError("report hash mismatch")
            report = Report.model_validate_json(payload)
        except FileNotFoundError:
            raise ValueError("report artifact manifest is missing") from None
        except ValueError:
            raise
        except Exception as exc:  # corrupted artifact must surface as ValueError．
            raise ValueError(f"report is corrupted: {path}") from exc
        if report.job_id != self.job_id:
            raise ValueError(f"report job_id mismatch: {path}")
        return report

    def save(self, report: Report) -> None:
        from .report import render_markdown

        self._ensure_safe_artifacts()
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
