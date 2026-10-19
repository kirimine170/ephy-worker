"""Thread-safe desktop lifecycle and bounded, display-safe stream projection."""

from __future__ import annotations

import asyncio
import copy
import math
import threading
import time
from collections import deque
from typing import Literal
from uuid import uuid4

from .adapter import DesktopAdapter
from .config import DesktopConfig

ExecutionState = Literal["idle", "starting", "running", "cancelling", "cancelled", "failed", "completed"]
MAX_TEXT = 2 * 1024 * 1024
MAX_EVENTS = 512


def _token(value):
    return value if type(value) is int and value >= 0 else None


class DesktopController:
    """One job at a time; detaching a view never re-submits the prompt."""

    def __init__(self, *, adapter: DesktopAdapter | None = None):
        self._adapter = adapter or DesktopAdapter()
        self._lock = threading.RLock()
        self._queue: deque[dict] = deque(maxlen=MAX_EVENTS)
        self._events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_event: asyncio.Event | None = None
        self._cancel_requested = False
        self._closing = False
        self._active = False
        self._connection = "connected"
        self._detached: dict | None = None
        self._started: float | None = None
        self._ended: float | None = None
        self._text_prefix = ""
        self._blocks: dict[int, str] = {}
        self._message_open = False
        self._data = self._empty()

    @staticmethod
    def _empty():
        return {
            "execution_state": "idle", "run_id": None, "text": "", "elapsed_seconds": None,
            "input_tokens": None, "output_tokens": None, "tokens_per_second": None,
            "turns": None, "tool_calls": None, "worker_pid": None, "artifact_dir": None,
            "error": None, "demo": False, "governance": "not_started",
            "karte": {"state": "off", "pending": 0, "recorded": False, "views_ready": False, "reason": None},
        }

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def _emit(self, kind: str, message: str, **fields):
        event = {"kind": kind, "message": message, **fields}
        event["at"] = round(time.monotonic() - self._started, 3) if self._started is not None else 0
        self._queue.append(event)
        if kind != "text":
            self._events.append(event)

    def _status(self, state: ExecutionState):
        self._data["execution_state"] = state
        self._emit("status", state, state=state)

    def start(self, config: DesktopConfig, prompt: str, demo: bool = False) -> bool:
        with self._lock:
            if self._active or self._closing:
                return False
            if not isinstance(config, DesktopConfig) or not isinstance(prompt, str) or not prompt.strip():
                self._data["error"] = "A desktop configuration and nonempty prompt are required."
                self._status("failed")
                return False
            # CodingJob reserves space for the fixed navigation instructions.
            if len(prompt) > 18000:
                self._data["error"] = "Prompt exceeds the 18000 character limit."
                self._status("failed")
                return False
            self._active = True  # Reserve synchronously before the worker thread starts.
            self._cancel_requested = False
            self._started = time.monotonic()
            self._ended = None
            self._connection = "connected"
            self._detached = None
            self._queue.clear()
            self._events.clear()
            self._text_prefix = ""
            self._blocks = {}
            self._message_open = False
            self._data = self._empty()
            self._data.update(run_id=f"desktop-{uuid4().hex}", demo=bool(demo), turns=0, tool_calls=0)
            self._status("starting")
            self._thread = threading.Thread(
                target=self._thread_main, args=(config, prompt.strip(), bool(demo)),
                name="ephy-desktop-worker", daemon=False,
            )
            try:
                self._thread.start()
            except RuntimeError:
                self._active = False
                self._data["error"] = "Worker thread could not start."
                self._status("failed")
                return False
            return True

    def cancel(self) -> bool:
        with self._lock:
            if not self._active or self._data["execution_state"] in {"completed", "failed", "cancelled"}:
                return False
            self._cancel_requested = True
            if self._data["execution_state"] != "cancelling":
                self._status("cancelling")
            if self._loop is not None and self._cancel_event is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._cancel_event.set)
            return True

    def disconnect(self):
        with self._lock:
            if self._connection == "disconnected":
                return
            self._detached = self._snapshot_locked()
            self._connection = "disconnected"
            self._emit("status", "View disconnected; worker continues.", state="disconnected")

    def reconnect(self):
        with self._lock:
            if self._connection != "disconnected":
                return
            self._connection = "reconnecting"
            self._detached = None
            self._emit("status", "Restoring accumulated worker state; no prompt resubmitted.", state="reconnecting")

    def close(self) -> bool:
        with self._lock:
            self._closing = True
            if self._active:
                self.cancel()
                return False
            return True

    def drain(self) -> list[dict]:
        with self._lock:
            if self._connection == "disconnected":
                return []
            events = list(self._queue)
            self._queue.clear()
            return copy.deepcopy(events)

    def _snapshot_locked(self) -> dict:
        snapshot = copy.deepcopy(self._data)
        snapshot["events"] = copy.deepcopy(list(self._events))
        snapshot["connection"] = self._connection
        snapshot["state"] = (
            self._connection if self._connection != "connected" else self._data["execution_state"]
        )
        snapshot["active"] = self._active
        if self._started is not None:
            snapshot["elapsed_seconds"] = max(0, (self._ended or time.monotonic()) - self._started)
        if self._connection == "disconnected" and self._detached is not None:
            for name in ("text", "events", "input_tokens", "output_tokens", "tokens_per_second", "turns", "tool_calls"):
                snapshot[name] = copy.deepcopy(self._detached[name])
        return snapshot

    def snapshot(self) -> dict:
        with self._lock:
            snapshot = self._snapshot_locked()
            if self._connection == "reconnecting":
                self._connection = "connected"
            return snapshot

    def _refresh_text(self):
        remaining = MAX_TEXT
        for index in sorted(self._blocks, reverse=True):
            self._blocks[index] = self._blocks[index][-remaining:] if remaining else ""
            remaining -= len(self._blocks[index])
        current = "".join(self._blocks[index] for index in sorted(self._blocks))
        self._data["text"] = (self._text_prefix + current)[-MAX_TEXT:]

    def _observe(self, event: dict):
        with self._lock:
            event_type = event.get("type")
            if event_type == "ephy_navigation_guard_ready":
                self._data["governance"] = "prepared"
            elif event_type == "ephy_desktop_governance_envelope":
                self._data["governance"] = "ack_pending"
            elif event_type == "ephy_desktop_governance_verified":
                self._data["governance"] = "ack_verified"
            if (
                event_type in {"turn_start", "message_start", "message_update", "tool_execution_start"}
                and self._data["execution_state"] == "starting"
            ):
                self._status("running")
            if event_type == "turn_start":
                self._data["turns"] += 1
            elif event_type == "tool_execution_start":
                self._data["tool_calls"] += 1
            elif event_type == "message_start" and event.get("message", {}).get("role") == "assistant":
                self._blocks = {}
                self._message_open = True
            elif event_type == "message_update":
                delta = event.get("assistantMessageEvent")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    index, text = delta.get("contentIndex", 0), delta.get("delta")
                    if type(index) is int and 0 <= index < 128 and isinstance(text, str):
                        self._message_open = True
                        self._blocks[index] = (self._blocks.get(index, "") + text)[-MAX_TEXT:]
                        self._refresh_text()
                        self._emit("text", "Assistant text updated.")
                return
            elif event_type == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, list):
                        final = "".join(
                            item["text"] for item in content
                            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
                        )
                        self._blocks = {0: final[-MAX_TEXT:]}
                    self._refresh_text()
                    if self._message_open or self._blocks:
                        self._text_prefix = (self._data["text"] + "\n\n")[-MAX_TEXT:]
                    self._blocks = {}
                    self._message_open = False
                    usage = message.get("usage")
                    if isinstance(usage, dict) and any(
                        (_token(usage.get(key)) or 0) > 0 for key in ("input", "output", "cacheRead", "cacheWrite")
                    ):
                        for field, source in (("input_tokens", "input"), ("output_tokens", "output")):
                            tokens = _token(usage.get(source))
                            if tokens is not None:
                                self._data[field] = (self._data[field] or 0) + tokens
                    # Pi's standard usage does not report generation timing.
                    # Do not turn wall-clock elapsed time into measured tokens/s.
                    timing = message.get("timings")
                    if isinstance(timing, dict):
                        rate = timing.get("predicted_per_second")
                        if type(rate) in {int, float} and math.isfinite(rate) and rate >= 0:
                            self._data["tokens_per_second"] = float(rate)
            safe_types = {
                "turn_start", "turn_end", "agent_end", "agent_settled", "auto_retry_start", "auto_retry_end",
                "message_start", "message_end", "tool_execution_start", "tool_execution_end",
                "ephy_navigation_guard_ready", "extension_error",
                "ephy_desktop_governance_envelope", "ephy_desktop_governance_verified",
            }
            if event_type in safe_types:
                fields = {"type": event_type}
                if event_type in {"tool_execution_start", "tool_execution_end"}:
                    tool = event.get("toolName")
                    allowed = {"read", "grep", "find", "ls", "governance_ack"}
                    fields["tool_name"] = tool if tool in allowed else "unsupported"
                    fields["is_error"] = event.get("isError") is True
                self._emit("event", str(event_type), **fields)

    def _process(self, pid: int | None):
        with self._lock:
            self._data["worker_pid"] = pid

    def _thread_main(self, config, prompt, demo):
        try:
            asyncio.run(self._execute(config, prompt, demo))
        except Exception as exc:  # noqa: BLE001 — report failures at the worker thread boundary.
            with self._lock:
                self._data["error"] = f"Worker failure: {type(exc).__name__}"
                self._status("failed")
        finally:
            with self._lock:
                self._loop = None
                self._cancel_event = None
                self._data["worker_pid"] = None
                self._ended = time.monotonic()
                self._active = False

    async def _execute(self, config, prompt, demo):
        from .karte import KarteObserver

        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._cancel_event = asyncio.Event()
            if self._cancel_requested:
                self._cancel_event.set()
            run_id = self._data["run_id"]
        # Synthetic execution never writes to a configured live Karte store.
        karte = KarteObserver(
            None if demo else config.karte_binary, None if demo else config.karte_root,
            config.artifact_root / "karte-spool",
        )
        status = await asyncio.to_thread(karte.record, run_id, "started", {})
        with self._lock:
            self._data["karte"] = status
        try:
            result, directory = await self._adapter.execute(
                config, prompt, run_id, self._cancel_event, self._observe, self._process, demo=demo,
            )
            with self._lock:
                self._data["artifact_dir"] = str(directory)
                state = {"succeeded": "completed", "cancelled": "cancelled", "failed": "failed"}[result.result.status]
                if result.changed_files:
                    state = "failed"
                    self._data["error"] = "Read-only navigation changed files; result rejected."
                elif result.result.failure is not None:
                    failure = result.result.failure
                    self._data["error"] = f"{failure.code} ({failure.stage}): {failure.detail or ''}"
                if self._cancel_requested and state == "completed":
                    self._emit("event", "Worker completed before cancellation took effect.", type="late_cancel")
                self._status(state)
        except Exception as exc:  # noqa: BLE001 — retain the failed run and its Karte terminal event.
            with self._lock:
                state = "cancelled" if self._cancel_requested else "failed"
                self._data["error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self._status(state)
        status = await asyncio.to_thread(karte.record, run_id, state, {})
        with self._lock:
            self._data["karte"] = status
