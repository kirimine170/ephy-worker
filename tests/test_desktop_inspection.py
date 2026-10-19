"""Offline checks with mock HTTP transport and owned synthetic CLI processes."""

import asyncio
import copy
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime

import httpx
import pytest

from ephy_worker.desktop import inspection
from ephy_worker.desktop.config import DesktopConfig
from ephy_worker.desktop.inspection import InspectionError, KarteReader, probe_strata

STAMP = "2026-01-01T00:00:00Z"


@pytest.fixture
def config(tmp_path):
    return DesktopConfig(
        repository_path=tmp_path / "repository", karte_binary=tmp_path / "karte.exe",
        karte_root=tmp_path / "records", model_id="synthetic-model", context_window=32768,
    )


def health_body(**updates):
    return {"service": "strata", "api_key": False, "loaded": True,
            "model": "synthetic-model", "max_context": 32768, **updates}


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, body, delay=0):
        self.body, self.delay, self.closed = body, delay, False

    async def __aiter__(self):
        for offset in range(0, len(self.body), 1024):
            await asyncio.sleep(self.delay)
            yield self.body[offset:offset + 1024]

    async def aclose(self):
        self.closed = True


def mock_health(monkeypatch, body=None, *, status=200, headers=None, failure=None, delay=0):
    raw = json.dumps(health_body() if body is None else body).encode() if not isinstance(body, bytes) else body
    stream = BodyStream(raw, delay)
    requests, options = [], []

    def handler(request):
        requests.append(request)
        if failure:
            raise failure
        return httpx.Response(status, headers=headers, stream=stream)

    client_type = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return client_type(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(inspection.httpx, "AsyncClient", client)
    return requests, options, stream


def test_health_only_gets_fixed_loopback_health_without_proxy_or_redirect(monkeypatch, config):
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.example:9999")
    requests, options, stream = mock_health(monkeypatch)
    result = probe_strata(replace(config, endpoint="http://localhost:8081/v1/custom"))
    assert result["state"] == "ready"
    assert result["code"] == "health_ready"
    assert datetime.fromisoformat(result["checked_at"]).tzinfo is not None
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://127.0.0.1:8081/health"
    assert requests[0].content == b""
    assert options == [{"timeout": 2.0, "trust_env": False, "follow_redirects": False}]
    assert stream.closed


@pytest.mark.parametrize(("body", "status", "state", "code"), [
    (health_body(loaded=False), 200, "starting", "health_starting"),
    (health_body(loaded=False), 503, "starting", "health_starting"),
    (health_body(), 503, "unavailable", "health_http_error"),
    (health_body(service="other"), 200, "mismatch", "health_identity_mismatch"),
    (health_body(model="other"), 200, "mismatch", "health_identity_mismatch"),
    (health_body(max_context=8192), 200, "mismatch", "health_identity_mismatch"),
    (health_body(api_key=True), 200, "mismatch", "health_identity_mismatch"),
    (health_body(loaded=1), 200, "error", "health_invalid_response"),
    (health_body(max_context=True), 200, "error", "health_invalid_response"),
    ({}, 200, "error", "health_invalid_response"),
    ([], 200, "error", "health_invalid_response"),
    (b"not-json", 200, "error", "health_invalid_response"),
    (b"\xff", 200, "error", "health_invalid_response"),
    (b'{"loaded":false,"loaded":true}', 200, "error", "health_invalid_response"),
    (b" " * (inspection.HEALTH_MAX_BYTES + 1), 200, "error", "health_output_limit"),
    (health_body(), 500, "unavailable", "health_http_error"),
    (health_body(), 302, "error", "health_redirect"),
], ids=[
    "starting", "starting-503", "loaded-503", "wrong-service", "wrong-model", "wrong-context",
    "authentication", "loaded-type", "context-type", "missing-fields", "wrong-root", "invalid-json",
    "invalid-utf8", "duplicate-fields", "oversized-body", "server-error", "redirect",
])
def test_health_negative_controls(monkeypatch, config, body, status, state, code):
    requests, _, stream = mock_health(monkeypatch, body, status=status, headers={"Location": "https://invalid.example"})
    result = probe_strata(config)
    assert result["state"] == state
    assert result["code"] == code
    assert len(requests) == 1
    assert stream.closed


def test_health_rejects_compressed_body(monkeypatch, config):
    mock_health(monkeypatch, headers={"Content-Encoding": "gzip"})
    assert probe_strata(config)["code"] == "health_invalid_response"


@pytest.mark.parametrize("failure", [httpx.ConnectError("private detail"), httpx.ReadTimeout("private detail")])
def test_health_unavailable_does_not_disclose_raw_error(monkeypatch, config, failure):
    mock_health(monkeypatch, failure=failure)
    result = probe_strata(config)
    assert result["state"] == "unavailable"
    assert "private" not in str(result)


def test_health_outer_deadline_bounds_slow_stream(monkeypatch, config):
    monkeypatch.setattr(inspection, "HEALTH_TIMEOUT", 0.05)
    _, _, stream = mock_health(monkeypatch, b" " * 8192, delay=0.03)
    started = time.monotonic()
    assert probe_strata(config)["state"] == "unavailable"
    assert time.monotonic() - started < 1
    assert stream.closed


def summary(task_id="desktop-synthetic", count=2):
    return {"task_id": task_id, "title": "ephy-worker desktop execution", "status": "failed",
            "last_recorded_at": STAMP, "event_count": count, "doc_id": "not-exposed"}


def page(*, count=2, offset=0, task_id="desktop-synthetic"):
    end = min(offset + 10, count)
    return {"task": summary(task_id, count), "objective": "Synthetic observation",
            "repository": "synthetic-repository", "base_revision": "HEAD", "events": [
                {"event_id": f"synthetic-{index}", "sequence": index, "kind": "note",
                 "text": "Inert text https://invalid.example <script>not executed</script>",
                 "origin": "host_observation", "recorded_at": STAMP}
                for index in range(offset + 1, end + 1)
            ], "next_offset": end if end < count else -1}


def mock_cli(monkeypatch, value=None, *, raw=None):
    calls = []

    def capture(argv, payload):
        calls.append((argv, json.loads(payload)))
        return raw if raw is not None else json.dumps({"value": value}).encode()

    monkeypatch.setattr(inspection, "_capture", capture)
    return calls


def test_search_fixed_query_and_summary_fields(monkeypatch, config):
    calls = mock_cli(monkeypatch, {"results": [summary()]})
    result = KarteReader(config).search()
    assert result == [{key: value for key, value in summary().items() if key != "doc_id"}]
    assert calls == [([str(config.karte_binary), "--root", str(config.karte_root), "search"],
                      {"query": "ephy-worker desktop execution", "limit": 20})]


@pytest.mark.parametrize("count", [0, 20])
def test_search_accepts_empty_and_maximum_results(monkeypatch, config, count):
    mock_cli(monkeypatch, {"results": [summary(f"synthetic-{index}") for index in range(count)]})
    assert len(KarteReader(config).search()) == count


@pytest.mark.parametrize("value", [
    {}, {"results": None}, {"results": [summary()] * 21}, {"results": [summary(), summary()]},
    {"results": [{**summary(), "event_count": True}]},
    {"results": [{**summary(), "event_count": 4097}]},
    {"results": [{**summary(), "status": "verified"}]},
    {"results": [{**summary(), "title": "x" * 513}]},
    {"results": [{**summary(), "last_recorded_at": "2026-01-01"}]},
    {"results": [{**summary(), "task_id": "../escape"}]},
])
def test_search_rejects_invalid_results(monkeypatch, config, value):
    mock_cli(monkeypatch, value)
    with pytest.raises(InspectionError, match="^karte_invalid_response$"):
        KarteReader(config).search()


@pytest.mark.parametrize(("count", "offset", "next_offset"), [(2, 0, -1), (12, 0, 10), (12, 10, -1), (12, 12, -1)])
def test_read_identity_pagination_and_inert_event_text(monkeypatch, config, count, offset, next_offset):
    calls = mock_cli(monkeypatch, page(count=count, offset=offset))
    result = KarteReader(config).read("desktop-synthetic", offset)
    assert result["next_offset"] == next_offset
    assert len(result["events"]) == min(10, count - offset)
    assert all("<script>not executed</script>" in event["text"] for event in result["events"])
    assert calls == [([str(config.karte_binary), "--root", str(config.karte_root), "read"],
                      {"task_id": "desktop-synthetic", "offset": offset, "limit": 10})]
    assert set(result["task"]) == {"task_id", "title", "status", "last_recorded_at", "event_count"}


def test_read_optional_fields_remain_inert_and_allowlisted(monkeypatch, config):
    value = page()
    value["events"][1].update({"kind": "finish", "outcome": "failed", "occurred_at": STAMP,
                               "next_action": "do not run this", "arbitrary": "not exposed",
                               "evidence": [{"reference": "https://invalid.example", "sha256": "a" * 64}]})
    mock_cli(monkeypatch, value)
    result = KarteReader(config).read("desktop-synthetic")
    assert result["events"][1]["outcome"] == "failed"
    assert result["events"][1]["evidence"][0]["reference"] == "https://invalid.example"
    assert "arbitrary" not in result["events"][1]


@pytest.mark.parametrize(("change", "code"), [
    (lambda p: p["task"].update(task_id="other"), "karte_task_mismatch"),
    (lambda p: p.update(next_offset=0), "karte_invalid_page"),
    (lambda p: p.update(next_offset=True), "karte_invalid_page"),
    (lambda p: p.update(events=[]), "karte_invalid_page"),
    (lambda p: p.update(events=p["events"] * 6), "karte_invalid_page"),
    (lambda p: p["task"].update(event_count=11), "karte_invalid_page"),
    (lambda p: p["events"][0].update(sequence=2), "karte_invalid_page"),
    (lambda p: p["events"][0].update(sequence=True), "karte_invalid_page"),
    (lambda p: p["events"][1].update(event_id="synthetic-1"), "karte_invalid_page"),
    (lambda p: p["events"][0].update(text="x" * 16385), "karte_invalid_response"),
    (lambda p: p["events"][0].update(origin="verified"), "karte_invalid_response"),
    (lambda p: p["events"][0].update(kind="unknown"), "karte_invalid_response"),
    (lambda p: p["events"][0].update(outcome="completed"), "karte_invalid_response"),
    (lambda p: p["events"][0].update(evidence=[{"reference": "x"}] * 17), "karte_invalid_response"),
    (lambda p: p["events"][0].update(evidence=[{"reference": "x", "sha256": "invalid"}]),
     "karte_invalid_response"),
    (lambda p: p.update(objective=None), "karte_invalid_response"),
])
def test_read_rejects_unbound_or_invalid_pages(monkeypatch, config, change, code):
    value = copy.deepcopy(page())
    change(value)
    mock_cli(monkeypatch, value)
    with pytest.raises(InspectionError) as caught:
        KarteReader(config).read("desktop-synthetic")
    assert caught.value.code == code
    assert str(caught.value) == code


@pytest.mark.parametrize(("task_id", "offset"), [("../escape", 0), ("--help", 0), ("a\nread", 0),
                                                 ("a; echo bad", 0), ("ok", True), ("ok", -1), ("ok", 4097)])
def test_read_invalid_input_never_launches_cli(monkeypatch, config, task_id, offset):
    calls = mock_cli(monkeypatch)
    with pytest.raises(InspectionError):
        KarteReader(config).read(task_id, offset)
    assert calls == []


def test_read_rejects_replayed_page_at_later_offset(monkeypatch, config):
    mock_cli(monkeypatch, page(count=20))
    with pytest.raises(InspectionError, match="^karte_invalid_page$"):
        KarteReader(config).read("desktop-synthetic", 10)


@pytest.mark.parametrize(("raw", "code"), [(b"not-json", "karte_invalid_response"),
                                          (b"\xff", "karte_invalid_response"),
                                          (b'{"value":{},"value":{}}', "karte_invalid_response"),
                                          (b'{"value":[],"error":"private detail"}', "karte_cli_error")])
def test_reader_rejects_invalid_envelopes_without_leaking(monkeypatch, config, raw, code):
    mock_cli(monkeypatch, raw=raw)
    with pytest.raises(InspectionError) as caught:
        KarteReader(config).search()
    assert caught.value.code == code
    assert "private" not in str(caught.value)


def test_reader_not_configured_never_launches(monkeypatch, config):
    calls = mock_cli(monkeypatch)
    with pytest.raises(InspectionError, match="^karte_not_configured$"):
        KarteReader(replace(config, karte_binary=None, karte_root=None)).search()
    assert calls == []


def synthetic_capture(code):
    return inspection._capture([sys.executable, "-I", "-c", code], b'{"synthetic":true}')


def track_children(monkeypatch):
    original = subprocess.Popen
    children, arguments = [], []

    def popen(argv, **kwargs):
        arguments.append((argv, kwargs))
        child = original(argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(inspection.subprocess, "Popen", popen)
    return children, arguments


def test_capture_drains_both_streams_and_uses_hidden_shell_false(monkeypatch):
    children, arguments = track_children(monkeypatch)
    expected = b"ok\r\n" if sys.platform == "win32" else b"ok\n"
    assert synthetic_capture(
        "import sys; sys.stdin.buffer.read(); sys.stderr.write('diagnostic'); print('ok')",
    ) == expected
    assert children[0].returncode == 0
    assert arguments[0][1]["shell"] is False
    assert arguments[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert arguments[0][1]["stdin"] == subprocess.PIPE
    assert arguments[0][1]["stdout"] == subprocess.PIPE
    assert arguments[0][1]["stderr"] == subprocess.PIPE


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("extra", [0, 1])
def test_capture_enforces_byte_limits_during_capture(monkeypatch, stream, extra):
    children, _ = track_children(monkeypatch)
    monkeypatch.setattr(inspection, "KARTE_MAX_STDOUT", 4096)
    monkeypatch.setattr(inspection, "KARTE_MAX_STDERR", 4096)
    code = f"import sys; sys.stdin.buffer.read(); sys.{stream}.buffer.write(b'x' * {4096 + extra})"
    if extra:
        with pytest.raises(InspectionError, match="^karte_output_limit$"):
            synthetic_capture(code)
    else:
        result = synthetic_capture(code)
        assert result == (b"x" * 4096 if stream == "stdout" else b"")
    assert children[0].poll() is not None
    assert children[0].stdout.closed
    assert children[0].stderr.closed


def test_capture_kills_and_reaps_owned_flooding_child(monkeypatch):
    children, _ = track_children(monkeypatch)
    monkeypatch.setattr(inspection, "KARTE_MAX_STDOUT", 4096)
    monkeypatch.setattr(inspection, "KARTE_MAX_STDERR", 4096)
    code = "import os,sys; sys.stdin.buffer.read()\nwhile True: os.write(1,b'x'*4096); os.write(2,b'y'*4096)"
    with pytest.raises(InspectionError, match="^karte_output_limit$"):
        synthetic_capture(code)
    assert len(children) == 1
    assert children[0].poll() is not None


def test_capture_timeout_kills_and_reaps_only_owned_child(monkeypatch):
    children, _ = track_children(monkeypatch)
    monkeypatch.setattr(inspection, "KARTE_TIMEOUT", 0.2)
    started = time.monotonic()
    with pytest.raises(InspectionError, match="^karte_timeout$"):
        synthetic_capture("import sys,time; sys.stdin.buffer.read(); time.sleep(30)")
    assert time.monotonic() - started < 2
    assert len(children) == 1
    assert children[0].poll() is not None


def test_capture_missing_cli_error_is_sanitized(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("private path")

    monkeypatch.setattr(inspection.subprocess, "Popen", missing)
    with pytest.raises(InspectionError, match="^karte_cli_missing$"):
        inspection._capture(["synthetic-missing"], b"{}")


def test_capture_nonzero_exit_never_returns_plausible_json():
    with pytest.raises(InspectionError, match="^karte_cli_error$"):
        synthetic_capture("import sys; sys.stdin.buffer.read(); print('{\"value\":{}}'); sys.exit(1)")
