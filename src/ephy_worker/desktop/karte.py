"""Minimal host observations via the existing Karte CLI, with durable retries.

Only fixed lifecycle text and allowlisted metadata leave this adapter. It never
initializes a Karte root, records prompt/output bodies, or claims verification.
Queue mutations use a short process lock, separate from the delivery lease.
New observations are durable before delivery contention can return to a caller.
Retries reconstruct identical event inputs.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_KINDS = {
    "started": "Worker execution was requested; prompt and repository content are not collected.",
    "running": "Worker execution started.",
    "checkpoint": "Worker lifecycle checkpoint observed.",
    "tool_start": "Read-only tool started; arguments are not collected.",
    "tool_end": "Read-only tool ended; output is not collected.",
    "succeeded": "Worker execution ended; task completion and verification are unconfirmed.",
    "failed": "Worker failure observed; error details are not collected.",
    "cancelled": "Worker cancellation observed.",
    "paused": "Worker execution stopped; task completion and verification are unconfirmed.",
    "disconnected": "GUI event connection interrupted; worker outcome is unconfirmed.",
    "reconnected": "GUI event connection restored; no task was resubmitted.",
}
_TERMINAL = {"succeeded": "paused", "failed": "failed", "cancelled": "cancelled", "paused": "paused"}
_ALIASES = {"start": "started", "completed": "succeeded"}
_EVENT_FIELDS = {"version", "binding", "run_key", "event_id", "kind", "metadata", "occurred_at", "ack"}


class _DeliveryBusy(Exception):
    """Another process owns delivery; durable observations remain queued."""


class KarteObserver:
    MAX_PENDING = 4096
    MAX_BYTES = 5 * 1024 * 1024
    MAX_FLUSH = 32

    def __init__(self, binary, root, spool_root, *, runner=None):
        self.binary = Path(binary) if binary else None
        self.root = Path(root).resolve() if root else None
        self._runner = runner or subprocess.run
        self._lock = threading.RLock()
        binding = os.path.normcase(str(self.root)) if self.root else "disabled"
        self._binding = hashlib.sha256(binding.encode()).hexdigest()
        self.spool = Path(spool_root) / ("karte-observer-" + self._binding[:20])
        self._last = self._result("off" if not self.enabled else "unsaved", reason="no_events")
        self._last_item = None

    @property
    def enabled(self):
        return self.binary is not None and self.root is not None

    def record(self, run_id: str, kind: str, metadata: dict | None = None) -> dict[str, Any]:
        if not self.enabled:
            return self._result("off", reason="not_configured")
        kind = _ALIASES.get(kind, kind)
        if not isinstance(run_id, str) or not run_id or kind not in _KINDS:
            return self._result("failed", reason="invalid_observation")
        item = {
            "version": 1,
            "binding": self._binding,
            "run_key": hashlib.sha256(run_id.encode()).hexdigest(),
            "event_id": "gui-" + uuid.uuid4().hex,
            "kind": kind,
            "metadata": self._metadata(metadata or {}),
            "occurred_at": datetime.now(UTC).isoformat(),
            "ack": {"recorded": False, "views_ready": False},
        }
        try:
            with self._lock, ExitStack() as delivery:
                with self._queue_exclusive():
                    files = self._paths()
                    raw = self._encode(item)
                    size = sum(p.stat().st_size for p in files)
                    if len(files) >= self.MAX_PENDING or size + len(raw) > self.MAX_BYTES:
                        return self._result("failed", reason="spool_full", pending=len(files))
                    path = self.spool / f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
                    self._save(path, raw)
                    self._last_item = item
                    # The delivery lease is attempted only after fsync+replace.
                    # While this queue lock is held, no sender can remove the
                    # newly saved event before its pending snapshot is returned.
                    try:
                        delivery.enter_context(self._exclusive())
                    except _DeliveryBusy:
                        return self._state(item, "delivery_busy")
                return self._flush_locked(item)
        except (OSError, ValueError, TypeError, KeyError):
            return self._result("failed", reason="spool_unavailable", pending=self._pending_count())

    def flush(self) -> dict[str, Any]:
        if not self.enabled:
            return self._result("off", reason="not_configured")
        try:
            with self._lock, ExitStack() as delivery:
                with self._queue_exclusive():
                    try:
                        delivery.enter_context(self._exclusive())
                    except _DeliveryBusy:
                        return self._state(self._last_item, "delivery_busy")
                return self._flush_locked(self._last_item)
        except (OSError, ValueError, TypeError, KeyError):
            return self._result("failed", reason="spool_unavailable", pending=self._pending_count())

    def _flush_locked(self, target=None):
        # Only the delivery lease spans CLI calls. Queue readers/writers remain
        # available while another GUI is blocked in a slow transport.
        with self._queue_exclusive():
            paths = self._paths()
            if not paths:
                return self._state(target)
            if target is None:
                try:
                    target = self._load(paths[-1])
                except (OSError, ValueError, TypeError, KeyError):
                    return self._result("failed", reason="invalid_spool_retained", pending=len(paths))
        if not self.binary.is_absolute() or self.binary.name.lower() != "karte-worklog.exe":
            return self._result("failed", reason="explicit_karte_executable_required", pending=len(paths))
        if not self.binary.is_file():
            return self._result("failed", reason="karte_executable_missing", pending=len(paths))
        if not (self.root / ".mdsys/worklog/v1/config.json").is_file():
            return self._result("failed", reason="karte_root_not_initialized", pending=len(paths))
        for path in paths[: self.MAX_FLUSH]:
            try:
                with self._queue_exclusive():
                    item = self._load(path)
            except (OSError, ValueError, TypeError, KeyError):
                return self._result("failed", reason="invalid_spool_retained", pending=self._pending_count())
            command, payload = self._payload(item)
            try:
                response = self._runner(
                    [str(self.binary), "--root", str(self.root), "--observer", command],
                    input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    timeout=5.0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return self._snapshot(target, "cli_timeout")
            except OSError:
                return self._snapshot(target, "cli_unavailable")
            try:
                if len(response.stdout) > 2 * 1024 * 1024:
                    raise ValueError("oversized_response")
                envelope = json.loads(response.stdout)
                receipt = envelope.get("value")
                valid = (
                    isinstance(receipt, dict)
                    and receipt.get("task_id") == payload["task_id"]
                    and type(receipt.get("sequence")) is int
                    and receipt["sequence"] > 0
                    and type(receipt.get("recorded")) is bool
                    and type(receipt.get("views_ready")) is bool
                )
                if not valid:
                    return self._snapshot(target, "receipt_missing_or_mismatched")
                complete = receipt["recorded"] and receipt["views_ready"]
                with self._queue_exclusive():
                    if receipt["recorded"]:
                        item["ack"] = {"recorded": True, "views_ready": receipt["views_ready"]}
                        # Remember partial acknowledgements across restarts.
                        self._save(path, self._encode(item))
                    if target["event_id"] == item["event_id"]:
                        target = item
                    if complete and response.returncode == 0 and not envelope.get("error"):
                        path.unlink()
                if not complete or response.returncode != 0 or envelope.get("error"):
                    reason = "views_pending" if receipt["recorded"] else "record_not_acknowledged"
                    return self._snapshot(target, reason)
            except (ValueError, TypeError, AttributeError):
                return self._snapshot(target, "invalid_cli_response")
        return self._snapshot(target)

    def _snapshot(self, item, reason=None):
        with self._queue_exclusive():
            return self._state(item, reason)

    def _state(self, item, reason=None):
        """Read a snapshot while holding the queue lock (never around CLI I/O)."""
        pending = self._paths()
        items = [self._load(path) for path in pending]
        if item is None:
            item = items[-1] if items else self._last_item
        if item is None:
            return {**self._last, "pending": 0, "reason": reason or self._last["reason"]}
        stored = next((value for value in items if value["event_id"] == item["event_id"]), None)
        if stored is not None:
            item = stored
        # The badge describes the whole durable queue, not merely its oldest
        # event. Also include this record's receipt: unrelated delivery must not
        # turn an unacknowledged current record into a saved result.
        acknowledgements = [value["ack"] for value in items]
        record_acknowledgements = acknowledgements if stored is not None else [*acknowledgements, item["ack"]]
        ack = {
            "recorded": all(value["recorded"] for value in record_acknowledgements),
            "views_ready": all(value["views_ready"] for value in record_acknowledgements),
        }
        if stored is None and not item["ack"]["recorded"] and reason is None:
            reason = "record_ack_unavailable"
        state = "saved" if ack["views_ready"] and ack["recorded"] else (
            "saved_views_pending" if ack["recorded"] else "unsaved"
        )
        result = self._result(state, reason=reason, pending=len(pending))
        result["pending_unsaved"] = sum(not value["recorded"] for value in acknowledgements)
        result["pending_views"] = sum(
            value["recorded"] and not value["views_ready"] for value in acknowledgements
        )
        result.update(**ack, task_id="desktop-" + item["run_key"][:32], event_id=item["event_id"])
        self._last = result
        self._last_item = item
        return result

    @staticmethod
    def _result(state, *, reason=None, pending=0):
        return {
            "state": state,
            "pending": pending,
            "recorded": False,
            "views_ready": False,
            "reason": reason,
            "task_id": None,
            "event_id": None,
        }

    @staticmethod
    def _metadata(metadata):
        if not isinstance(metadata, dict):
            return {}
        clean = {}
        if isinstance(metadata.get("tool_name"), str) and metadata["tool_name"] in {"read", "grep", "find", "ls"}:
            clean["tool_name"] = metadata["tool_name"]
        if type(metadata.get("is_error")) is bool:
            clean["is_error"] = metadata["is_error"]
        if type(metadata.get("pid")) is int and 0 < metadata["pid"] <= 2**32 - 1:
            clean["pid"] = metadata["pid"]
        return clean

    def _load(self, path):
        if path.stat().st_size > 4096 or path.is_symlink():
            raise ValueError("invalid_spool")
        item = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(item, dict)
            or set(item) != _EVENT_FIELDS
            or item["version"] != 1
            or item["binding"] != self._binding
            or not re.fullmatch(r"[0-9a-f]{64}", item["run_key"])
            or not re.fullmatch(r"gui-[0-9a-f]{32}", item["event_id"])
            or item["kind"] not in _KINDS
            or not isinstance(item["metadata"], dict)
            or self._metadata(item["metadata"]) != item["metadata"]
            or not isinstance(item["ack"], dict)
            or set(item["ack"]) != {"recorded", "views_ready"}
            or any(type(value) is not bool for value in item["ack"].values())
        ):
            raise ValueError("invalid_spool")
        timestamp = datetime.fromisoformat(item["occurred_at"])
        if timestamp.tzinfo is None:
            raise ValueError("invalid_timestamp")
        return item

    @staticmethod
    def _payload(item):
        task = "desktop-" + item["run_key"][:32]
        payload = {"task_id": task, "event_id": item["event_id"]}
        kind = item["kind"]
        if kind == "started":
            payload.update(
                title="ephy-worker desktop execution",
                objective="Observe worker lifecycle only. Prompt, tool arguments, output, and error details are not collected.",
            )
            return "start", payload
        event_kind = "finish" if kind in _TERMINAL else "tool" if kind.startswith("tool_") else "checkpoint"
        text = _KINDS[kind]
        if item["metadata"]:
            text += " " + json.dumps(item["metadata"], sort_keys=True, separators=(",", ":"))
        payload.update(kind=event_kind, text=text, occurred_at=item["occurred_at"])
        if kind in _TERMINAL:
            payload["outcome"] = _TERMINAL[kind]
        return "append", payload

    def _paths(self):
        return sorted(self.spool.glob("*.json"))

    def _pending_count(self):
        try:
            return len(self._paths())
        except OSError:
            return None

    @staticmethod
    def _encode(item):
        return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _save(path, raw):
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _exclusive(self):
        with self._process_lock(".observer.lock", blocking=False):
            yield

    @contextmanager
    def _queue_exclusive(self):
        # Contention waits for the short filesystem transaction; it is never
        # converted into a lost observation. Process exit releases OS locks.
        with self._process_lock(".queue.lock", blocking=True):
            yield

    @contextmanager
    def _process_lock(self, filename, *, blocking):
        self.spool.mkdir(parents=True, exist_ok=True)
        with (self.spool / filename).open("a+b") as lock:
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if not blocking:
                        raise _DeliveryBusy from error
                    time.sleep(0.01)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
