"""Offline helper protocol and owned ephemeral loopback host boundary checks."""

import copy
import hashlib
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from ephy_worker.desktop import renderer
from ephy_worker.desktop.config import DesktopConfig
from ephy_worker.desktop.inspection import InspectionError
from ephy_worker.desktop.renderer import KarteRenderer, KarteViewSession


def success():
    return {
        "schema_version": 1, "ok": True, "task_id": "desktop-synthetic", "doc_id": "synthetic-doc",
        "relative_path": "content/projects/synthetic/note/worklog-desktop-synthetic.md",
        "source_sha256": "a" * 64, "markdown_sha256": "b" * 64,
        "renderer_commit": renderer.RENDERER_COMMIT,
        "html": "<!doctype html><html><body>synthetic document</body></html>",
        "metadata": {"schemaVersion": 1},
    }


@pytest.fixture
def config(tmp_path):
    return DesktopConfig(repository_path=tmp_path / "repository", karte_binary=tmp_path / "writer.exe",
                         karte_root=tmp_path / "records")


@pytest.fixture
def helper(monkeypatch, tmp_path):
    binary = tmp_path / "karte-worklog-render.exe"
    binary.write_bytes(b"synthetic binary identity; never executed")
    provenance = {
        "schema_version": 1, "helper_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "renderer_commit": renderer.RENDERER_COMMIT, "karte_base_commit": renderer.KARTE_BASE_COMMIT,
        "karte_patch_sha256": "c" * 64, "go_version": renderer.GO_VERSION,
    }
    (tmp_path / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    monkeypatch.setattr(renderer, "_helper_path", lambda: binary)
    return binary


def test_only_fixed_verified_helper_receives_root_and_task(monkeypatch, config, helper):
    calls = []
    monkeypatch.setattr(renderer, "_capture", lambda *args: calls.append(args) or json.dumps(success()).encode())
    assert KarteRenderer(config).render("desktop-synthetic") == success()
    assert calls == [([str(helper), "--root", str(config.karte_root)], b'{"task_id":"desktop-synthetic"}')]
    assert str(config.karte_binary) not in calls[0][0]


@pytest.mark.parametrize("task_id", ["../outside", "-option", "a/b", "x\x00", "", "a" * 129])
def test_invalid_task_never_invokes_helper(monkeypatch, config, task_id):
    calls = []
    monkeypatch.setattr(renderer, "_capture", lambda *args: calls.append(args))
    with pytest.raises(InspectionError):
        KarteRenderer(config).render(task_id)
    assert calls == []


@pytest.mark.parametrize("mutation", ["missing", "hash", "renderer", "base", "version", "extra", "duplicate", "oversize"])
def test_helper_provenance_controls(monkeypatch, config, helper, mutation):
    path = helper.parent / "provenance.json"
    value = json.loads(path.read_text())
    if mutation == "missing":
        helper.unlink()
    elif mutation == "hash":
        helper.write_bytes(b"changed")
    elif mutation == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif mutation == "oversize":
        path.write_bytes(b" " * 4097)
    else:
        key = {"renderer": "renderer_commit", "base": "karte_base_commit", "version": "go_version", "extra": "extra"}[mutation]
        value[key] = "invalid"
        path.write_text(json.dumps(value))
    calls = []
    monkeypatch.setattr(renderer, "_capture", lambda *args: calls.append(args))
    with pytest.raises(InspectionError, match="karte_renderer_(helper_missing|identity)"):
        KarteRenderer(config).render("desktop-synthetic")
    assert calls == []


@pytest.mark.parametrize(("key", "bad"), [
    ("schema_version", True), ("ok", 1), ("task_id", "other"), ("doc_id", "../doc"),
    ("relative_path", "../secret.md"),
    ("relative_path", "content/projects/../note/worklog-desktop-synthetic.md"),
    ("relative_path", "content/projects/synthetic/note/worklog-other.md"),
    ("relative_path", "content/projects/synthetic/note/worklog-desktop-synthetic.md?query"),
    ("relative_path", "C:/content/projects/synthetic/note/worklog-desktop-synthetic.md"),
    ("renderer_commit", "wrong"), ("source_sha256", "g" * 64), ("markdown_sha256", "A" * 64),
    ("html", ""), ("html", "\x00"), ("html", "\ud800"), ("metadata", {"schemaVersion": True}),
    ("metadata", {"schemaVersion": 2}), ("extra", "unexpected"),
])
def test_response_identity_path_pin_and_type_controls(monkeypatch, config, helper, key, bad):
    value = success()
    value[key] = bad
    monkeypatch.setattr(renderer, "_capture", lambda *args: json.dumps(value).encode())
    with pytest.raises(InspectionError):
        KarteRenderer(config).render("desktop-synthetic")


@pytest.mark.parametrize("raw", [b"not-json", b'[]', b'{"schema_version":1,"schema_version":1}', b"\xff"])
def test_invalid_json_response(monkeypatch, config, helper, raw):
    monkeypatch.setattr(renderer, "_capture", lambda *args: raw)
    with pytest.raises(InspectionError, match="karte_renderer_invalid_response"):
        KarteRenderer(config).render("desktop-synthetic")


def test_response_limit_is_not_truncated(monkeypatch, config, helper):
    monkeypatch.setattr(renderer, "_capture", lambda *args: b" " * (renderer.KARTE_MAX_STDOUT + 1))
    with pytest.raises(InspectionError, match="karte_output_limit"):
        KarteRenderer(config).render("desktop-synthetic")


def test_stable_domain_error_and_untrusted_diagnostics(monkeypatch, config, helper):
    for code in sorted(renderer._HELPER_ERRORS):
        body = {"schema_version": 1, "ok": False, "error": code}
        monkeypatch.setattr(renderer, "_capture", lambda *args, body=body: json.dumps(body).encode())
        with pytest.raises(InspectionError, match="^karte_renderer_" + code + "$"):
            KarteRenderer(config).render("desktop-synthetic")
    for error in ["private secret detail", [], None]:
        body = {"schema_version": 1, "ok": False, "error": error}
        monkeypatch.setattr(renderer, "_capture", lambda *args, body=body: json.dumps(body).encode())
        with pytest.raises(InspectionError, match="^karte_renderer_invalid_response$"):
            KarteRenderer(config).render("desktop-synthetic")


def request(url, *, child=False, path=None, method="GET", changes=None):
    parsed = urlsplit(url)
    headers = {
        "Host": parsed.netloc, "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "iframe" if child else "document",
        "Sec-Fetch-Site": "same-origin" if child else "none",
    }
    for key, value in (changes or {}).items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request(method, path or parsed.path + ("document" if child else ""), headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def view():
    calls = []
    session = KarteViewSession(lambda: calls.append(1) or success())
    url = session.start()
    yield session, url, calls
    session.close()


def test_host_starts_only_explicitly_and_never_renders_wrapper_or_caches(view):
    session, url, calls = view
    assert session.active
    assert session.start() == url
    assert len(urlsplit(url).path.strip("/")) == 64
    assert urlsplit(url).hostname == "127.0.0.1"
    status, headers, body = request(url)
    assert status == 200 and calls == []
    assert b'sandbox=""' in body and b"allow-scripts" not in body
    assert b"Reload checks current read permission" in body
    assert b"already displayed page remains" in body
    assert "frame-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    for _ in range(2):
        status, headers, body = request(url, child=True)
        assert status == 200 and body == success()["html"].encode()
        assert headers["Content-Security-Policy"].endswith("frame-src 'none'; frame-ancestors 'self'; sandbox")
        for directive in ("script-src", "connect-src", "img-src", "font-src", "object-src", "form-action", "base-uri"):
            assert directive + " 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store, max-age=0"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-DNS-Prefetch-Control"] == "off"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers
    assert calls == [1, 1]


@pytest.mark.parametrize(("child", "changes"), [
    (False, {"Host": "localhost:1"}), (False, {"Host": "evil.example"}),
    (False, {"Origin": "https://evil.example"}), (False, {"Origin": "null"}),
    (False, {"Sec-Fetch-Site": "cross-site"}), (False, {"Sec-Fetch-Dest": "iframe"}),
    (False, {"Sec-Fetch-Mode": "cors"}), (False, {"Sec-Fetch-Dest": None}),
    (False, {"Content-Length": "1"}), (False, {"Transfer-Encoding": "chunked"}),
    (True, {"Sec-Fetch-Dest": "document"}), (True, {"Sec-Fetch-Site": "none"}),
    (True, {"Sec-Fetch-Site": "cross-site"}), (True, {"Authorization": "private"}),
])
def test_host_rejects_untrusted_navigation_and_cross_origin(view, child, changes):
    _, url, calls = view
    assert request(url, child=child, changes=changes)[0] == 403
    assert calls == []


@pytest.mark.parametrize("path", ["/", "/favicon.ico", "/secret", "/../file", "/%2e%2e/file", "?secret=1", "document?query=1"])
def test_host_has_no_file_or_query_route(view, path):
    _, url, calls = view
    path = path if path.startswith("/") else urlsplit(url).path + path
    assert request(url, path=path, child=True)[0] == 404
    assert calls == []


@pytest.mark.parametrize("method", ["POST", "PUT", "HEAD", "OPTIONS", "CONNECT", "TRACE", "DELETE"])
def test_host_get_only(view, method):
    _, url, calls = view
    assert request(url, method=method)[0] == 501
    assert calls == []


def test_host_current_permission_failure_has_no_stale_html():
    enabled = True

    def read():
        if not enabled:
            raise InspectionError("private detail must never be returned")
        return success()

    session = KarteViewSession(read)
    url = session.start()
    try:
        assert request(url, child=True)[0] == 200
        enabled = False
        status, _, body = request(url, child=True)
        assert status == 503 and b"synthetic document" not in body and b"private" not in body
    finally:
        session.close()


def test_close_revokes_pending_render_without_waiting():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    result = []

    def read():
        entered.set()
        release.wait(2)
        return success()

    session = KarteViewSession(read)
    url = session.start()

    def fetch():
        try:
            result.append(request(url, child=True))
        except (OSError, http.client.HTTPException):
            result.append(None)
        finally:
            done.set()

    threading.Thread(target=fetch, daemon=True).start()
    assert entered.wait(1)
    started = time.monotonic()
    session.close()
    assert time.monotonic() - started < 0.2
    assert not session.active
    release.set()
    assert done.wait(2)
    assert result == [None]
    session.close()
    with pytest.raises(InspectionError, match="karte_view_closed"):
        session.start()


def test_duplicate_child_render_is_rejected_not_queued():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def read():
        entered.set()
        release.wait(2)
        return success()

    session = KarteViewSession(read)
    url = session.start()

    def fetch():
        try:
            request(url, child=True)
        finally:
            done.set()

    threading.Thread(target=fetch, daemon=True).start()
    try:
        assert entered.wait(1)
        assert request(url, child=True)[0] == 429
        release.set()
        assert done.wait(2)
    finally:
        release.set()
        session.close()


def test_ttl_closes_listener_and_constructor_does_not_listen():
    calls = []
    session = KarteViewSession(lambda: calls.append(1) or success(), ttl_seconds=0.05)
    assert not session.active and session._server is None
    url = session.start()
    time.sleep(0.15)
    assert not session.active and calls == []
    with pytest.raises((OSError, http.client.HTTPException)):
        request(url)
    with pytest.raises(InspectionError, match="karte_view_closed"):
        session.start()


@pytest.mark.parametrize("ttl", [0, -1, True, 301, float("nan"), float("inf"), "300"])
def test_invalid_ttl_never_listens(ttl):
    with pytest.raises(ValueError):
        KarteViewSession(success, ttl_seconds=ttl)


def test_duplicate_host_headers_and_bounded_request_size(view):
    _, url, calls = view
    parsed = urlsplit(url)
    cases = [
        f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nHost: {parsed.netloc}\r\n\r\n",
        f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nX-Large: " + "x" * 17000 + "\r\n\r\n",
    ]
    for raw in cases:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2) as connection:
            connection.sendall(raw.encode())
            result = bytearray()
            try:
                while part := connection.recv(4096):
                    result.extend(part)
            except ConnectionResetError:
                pass
            assert b"200" not in result and b"iframe" not in result
    assert calls == []


def test_html_is_unmodified_and_not_interpreted_by_host(view):
    session, url, _ = view
    body = '<!doctype html><script>window.BAD=1</script><a href="file:///secret">link</a>'
    value = copy.deepcopy(success())
    value["html"] = body
    session._read_view = lambda: value
    assert request(url, child=True)[2] == body.encode()
    # Browser execution/network isolation is independently tested in headless Chrome.
