"""Bounded, read-only diagnostics; results stay in memory and are untrusted text."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import DesktopConfig, validate_endpoint

HEALTH_TIMEOUT = 2.0
HEALTH_MAX_BYTES = 64 * 1024
KARTE_TIMEOUT = 5.0
KARTE_MAX_STDOUT = 2 * 1024 * 1024
KARTE_MAX_STDERR = 64 * 1024
KARTE_QUERY = "ephy-worker desktop execution"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_STATUSES = {"running", "paused", "completed", "failed", "cancelled"}


class InspectionError(Exception):
    """A stable local reason code, never subprocess diagnostics or private values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(raw: bytes, code: str) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeError, ValueError, RecursionError):
        raise InspectionError(code) from None
    if not isinstance(value, dict):
        raise InspectionError(code)
    return value


async def _health(config: DesktopConfig) -> tuple[str, str]:
    try:
        parsed = urlsplit(validate_endpoint(config.endpoint))
        # Pin localhost to a loopback address; never resolve an arbitrary hostname.
        host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        host = f"[{host}]" if ":" in host else host
        origin = host if parsed.port is None else f"{host}:{parsed.port}"
        url = urlunsplit((parsed.scheme, origin, "/health", "", ""))
    except (TypeError, ValueError):
        return "error", "health_configuration"
    try:
        # The outer deadline also bounds slow headers and slowly arriving bodies.
        async with asyncio.timeout(HEALTH_TIMEOUT):
            async with httpx.AsyncClient(
                timeout=HEALTH_TIMEOUT, trust_env=False, follow_redirects=False,
            ) as client:
                async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                    if 300 <= response.status_code < 400:
                        return "error", "health_redirect"
                    if response.status_code not in {200, 503}:
                        return "unavailable", "health_http_error"
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return "error", "health_invalid_response"
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=4096):
                        if len(body) + len(chunk) > HEALTH_MAX_BYTES:
                            return "error", "health_output_limit"
                        body.extend(chunk)
                    value = _json(bytes(body), "health_invalid_response")
                    status = response.status_code
    except (httpx.RequestError, TimeoutError, OSError):
        return "unavailable", "health_unavailable"
    except InspectionError as exc:
        return "error", exc.code
    if (
        not isinstance(value.get("service"), str)
        or type(value.get("loaded")) is not bool
        or type(value.get("api_key")) is not bool
        or not isinstance(value.get("model"), str)
        or type(value.get("max_context")) is not int
    ):
        return "error", "health_invalid_response"
    if (
        value["service"] != "strata" or value["api_key"] is not False
        or value["model"] != config.model_id or value["max_context"] != config.context_window
    ):
        return "mismatch", "health_identity_mismatch"
    if value["loaded"] is False:
        return "starting", "health_starting"
    if status != 200:
        return "unavailable", "health_http_error"
    return "ready", "health_ready"


def probe_strata(config: DesktopConfig) -> dict:
    """Inspect only the configured origin's health; never start or change services."""
    state, code = asyncio.run(_health(config))
    return {"state": state, "checked_at": datetime.now(UTC).isoformat(), "code": code}


def _capture(argv: list[str], payload: bytes) -> bytes:
    """Drain both pipes with hard in-memory bounds and reap only our own child."""
    deadline = time.monotonic() + KARTE_TIMEOUT
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        raise InspectionError("karte_cli_missing") from None
    except OSError:
        raise InspectionError("karte_cli_error") from None
    failure = threading.Event()
    problems: list[str] = []
    stdout = bytearray()

    def drain(pipe, limit, target=None):
        size = 0
        try:
            while chunk := pipe.read(4096):
                size += len(chunk)
                if size > limit:
                    problems.append("karte_output_limit")
                    failure.set()
                    return
                if target is not None:
                    target.extend(chunk)
        except OSError:
            problems.append("karte_cli_error")
            failure.set()
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, KARTE_MAX_STDOUT, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, KARTE_MAX_STDERR), daemon=True),
    ]
    try:
        for reader in readers:
            reader.start()
        process.stdin.write(payload)
        process.stdin.close()
        while True:
            if failure.is_set():
                raise InspectionError(problems[0])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InspectionError("karte_timeout")
            if process.poll() is not None and not any(reader.is_alive() for reader in readers):
                break
            failure.wait(min(0.02, remaining))
        if failure.is_set():
            raise InspectionError(problems[0])
        if process.returncode != 0:
            raise InspectionError("karte_cli_error")
        return bytes(stdout)
    except OSError:
        raise InspectionError("karte_cli_error") from None
    finally:
        # No tree, service, PID lookup or unrelated process is ever terminated.
        if process.poll() is None:
            process.kill()
        process.wait()
        if not process.stdin.closed:
            process.stdin.close()
        for reader in readers:
            reader.join(timeout=0.2)


def _text(value, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (nonempty and not value.strip()):
        raise InspectionError("karte_invalid_response")
    try:
        valid = len(value.encode("utf-8")) <= limit
    except UnicodeError:
        valid = False
    if not valid:
        raise InspectionError("karte_invalid_response")
    return value


def _token(value) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise InspectionError("karte_invalid_response")
    return value


def _timestamp(value) -> str:
    value = _text(value, 64, nonempty=True)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InspectionError("karte_invalid_response") from None
    if parsed.tzinfo is None:
        raise InspectionError("karte_invalid_response")
    return value


def _summary(value) -> dict:
    if not isinstance(value, dict):
        raise InspectionError("karte_invalid_response")
    status = _text(value.get("status"), 20)
    count = value.get("event_count")
    if status not in _STATUSES or type(count) is not int or not 1 <= count <= 4096:
        raise InspectionError("karte_invalid_response")
    return {
        "task_id": _token(value.get("task_id")),
        "title": _text(value.get("title"), 512, nonempty=True), "status": status,
        "last_recorded_at": _timestamp(value.get("last_recorded_at")), "event_count": count,
    }


def _event(value, sequence: int) -> dict:
    if not isinstance(value, dict):
        raise InspectionError("karte_invalid_response")
    if type(value.get("sequence")) is not int or value["sequence"] != sequence:
        raise InspectionError("karte_invalid_page")
    kind = _text(value.get("kind"), 32)
    origin = _text(value.get("origin"), 32)
    if kind not in {"start", "checkpoint", "tool", "verification", "failure", "note", "finish", "resume"}:
        raise InspectionError("karte_invalid_response")
    if origin not in {"host_observation", "model_report"}:
        raise InspectionError("karte_invalid_response")
    result = {
        "event_id": _token(value.get("event_id")), "sequence": sequence, "kind": kind,
        "text": _text(value.get("text"), 16384, nonempty=True), "origin": origin,
        "recorded_at": _timestamp(value.get("recorded_at")),
    }
    for key, limit in (("next_action", 4096), ("outcome", 20)):
        if key in value:
            result[key] = _text(value[key], limit)
    if kind == "finish":
        if result.get("outcome") not in _STATUSES - {"running"}:
            raise InspectionError("karte_invalid_response")
    elif result.get("outcome"):
        raise InspectionError("karte_invalid_response")
    if "occurred_at" in value:
        result["occurred_at"] = _timestamp(value["occurred_at"])
    if "evidence" in value:
        evidence = value["evidence"]
        if not isinstance(evidence, list) or len(evidence) > 16:
            raise InspectionError("karte_invalid_response")
        result["evidence"] = []
        for item in evidence:
            if not isinstance(item, dict):
                raise InspectionError("karte_invalid_response")
            reference = {"reference": _text(item.get("reference"), 1024, nonempty=True)}
            if "sha256" in item:
                digest = _text(item["sha256"], 64)
                if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise InspectionError("karte_invalid_response")
                reference["sha256"] = digest
            result["evidence"].append(reference)
    return result


class KarteReader:
    """Use official read/search permission checks, never the writer or raw store."""

    def __init__(self, config: DesktopConfig):
        self.config = config

    def _call(self, command: str, arguments: dict) -> dict:
        if self.config.karte_binary is None or self.config.karte_root is None:
            raise InspectionError("karte_not_configured")
        if command not in {"search", "read"}:
            raise InspectionError("karte_cli_error")
        raw = _capture(
            [str(self.config.karte_binary), "--root", str(self.config.karte_root), command],
            json.dumps(arguments, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        )
        payload = _json(raw, "karte_invalid_response")
        if "error" in payload:
            raise InspectionError("karte_cli_error")
        if not isinstance(payload.get("value"), dict):
            raise InspectionError("karte_invalid_response")
        return payload["value"]

    def search(self) -> list[dict]:
        value = self._call("search", {"query": KARTE_QUERY, "limit": 20})
        items = value.get("results")
        if not isinstance(items, list) or len(items) > 20:
            raise InspectionError("karte_invalid_response")
        results = [_summary(item) for item in items]
        if len({item["task_id"] for item in results}) != len(results):
            raise InspectionError("karte_invalid_response")
        return results

    def read(self, task_id: str, offset: int = 0) -> dict:
        _token(task_id)
        if type(offset) is not int or not 0 <= offset <= 4096:
            raise InspectionError("karte_invalid_page")
        value = self._call("read", {"task_id": task_id, "offset": offset, "limit": 10})
        task = _summary(value.get("task"))
        if task["task_id"] != task_id:
            raise InspectionError("karte_task_mismatch")
        items, next_offset = value.get("events"), value.get("next_offset")
        count = task["event_count"]
        if offset > count or not isinstance(items, list) or len(items) != min(10, count - offset):
            raise InspectionError("karte_invalid_page")
        end = offset + len(items)
        expected_next = end if end < count else -1
        if type(next_offset) is not int or next_offset != expected_next:
            raise InspectionError("karte_invalid_page")
        events = [_event(item, offset + index + 1) for index, item in enumerate(items)]
        if len({item["event_id"] for item in events}) != len(events):
            raise InspectionError("karte_invalid_page")
        return {
            "task": task, "objective": _text(value.get("objective"), 8192, nonempty=True),
            "repository": _text(value.get("repository"), 1024),
            "base_revision": _text(value.get("base_revision"), 128),
            "events": events, "next_offset": next_offset,
        }
