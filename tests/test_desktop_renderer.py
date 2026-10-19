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

# Test-only snapshot of the pinned renderer fallback head; no product CSS generator.
_OFFICIAL_PREFIX = (
    '<!doctype html>\n'
    '<html lang="en">\n'
    '<head>\n'
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<title>作業記録: Synthetic v9 official renderer integration</title>\n'
    '<style id="karte-renderer-css">\n'
    '/* Purple Color Palette theme\n'
    '   base colors: #cceaf3, #c2b4ff, #e3e2ff, #828ea6\n'
    '*/\n'
    '\n'
    '/* 全体 ------------------------------------------------------ */\n'
    '\n'
    'body {\n'
    '  font-family: "メイリオ", "Hiragino Kaku Gothic ProN", system-ui, sans-serif;\n'
    '  line-height: 1.7;\n'
    '  font-size: 14px;\n'
    '  color: #333344;\n'
    '  background: linear-gradient(180deg, #f9f9ff 0%, #f5f4ff 45%, #f7fbff 100%);\n'
    '  margin: 24px 32px;\n'
    '}\n'
    '\n'
    '/* 見出し ----------------------------------------------------- */\n'
    '\n'
    'h1, h2, h3, h4, h5, h6 {\n'
    '  font-weight: 700;\n'
    '  color: #4b4b66;\n'
    '  margin-top: 1.8em;\n'
    '  margin-bottom: 0.6em;\n'
    '}\n'
    '\n'
    '/* h1: メインタイトル */\n'
    'h1 {\n'
    '  font-size: 2.2em;\n'
    '  text-align: center;\n'
    '  padding-bottom: 0.4em;\n'
    '  border-bottom: 3px solid #c2b4ff;\n'
    '}\n'
    '\n'
    '/* h2: セクション */\n'
    'h2 {\n'
    '  font-size: 1.6em;\n'
    '  border-bottom: 2px solid #e3e2ff;\n'
    '  padding-bottom: 0.25em;\n'
    '}\n'
    '\n'
    '/* h3: 小見出し（細いボーダー＋淡い背景） */\n'
    'h3 {\n'
    '  font-size: 1.3em;\n'
    '  padding: 0.25em 0.4em;\n'
    '  border-left: 4px solid #c2b4ff;\n'
    '  background: rgba(227, 226, 255, 0.35);\n'
    '  border-radius: 6px;\n'
    '}\n'
    '\n'
    '/* 残りの見出し */\n'
    'h4 { font-size: 1.15em; }\n'
    'h5 { font-size: 1.05em; }\n'
    'h6 { font-size: 1em; opacity: 0.85; }\n'
    '\n'
    '/* 段落 ------------------------------------------------------ */\n'
    '\n'
    'p {\n'
    '  margin: 0.4em 0 0.9em;\n'
    '}\n'
    '\n'
    '/* リスト ----------------------------------------------------- */\n'
    '\n'
    'ul,\n'
    'ol {\n'
    '  margin: 0.3em 0 0.9em 1.6em;\n'
    '  padding-left: 0.4em;\n'
    '}\n'
    '\n'
    'li {\n'
    '  margin-bottom: 0.25em;\n'
    '}\n'
    '\n'
    'ul ul,\n'
    'ol ul,\n'
    'ul ol,\n'
    'ol ol {\n'
    '  margin-top: 0.2em;\n'
    '}\n'
    '\n'
    '/* チェックリスト */\n'
    'input[type="checkbox"] {\n'
    '  margin-right: 6px;\n'
    '  accent-color: #c2b4ff;\n'
    '}\n'
    '\n'
    '/* リンク ----------------------------------------------------- */\n'
    '\n'
    'a {\n'
    '  color: #828ea6;\n'
    '  text-decoration: underline;\n'
    '  text-decoration-thickness: 1px;\n'
    '  text-underline-offset: 2px;\n'
    '  transition: color 0.15s ease, background-color 0.15s ease;\n'
    '}\n'
    '\n'
    'a:hover {\n'
    '  color: #c2b4ff;\n'
    '  background-color: rgba(194, 180, 255, 0.12);\n'
    '}\n'
    '\n'
    '/* 引用 ------------------------------------------------------- */\n'
    '\n'
    'blockquote {\n'
    '  margin: 0.8em 0;\n'
    '  padding: 0.7em 1em;\n'
    '  border-left: 4px solid #c2b4ff;\n'
    '  background: rgba(204, 234, 243, 0.35);\n'
    '  border-radius: 8px;\n'
    '  color: #4c5460;\n'
    '}\n'
    '\n'
    '/* コード ----------------------------------------------------- */\n'
    '\n'
    '/* インラインコード */\n'
    'code {\n'
    '  background: #e3e2ff;\n'
    '  color: #383854;\n'
    '  padding: 0.1em 0.4em;\n'
    '  border-radius: 4px;\n'
    '  font-family: "SF Mono", "Menlo", "Consolas", "Courier New", monospace;\n'
    '  font-size: 0.95em;\n'
    '}\n'
    '\n'
    '/* コードブロック */\n'
    'pre {\n'
    '  background: #f3f3ff;\n'
    '  border-radius: 10px;\n'
    '  padding: 12px 14px;\n'
    '  border: 1px solid rgba(130, 142, 166, 0.35);\n'
    '  overflow-x: auto;\n'
    '  margin: 0.8em 0 1.2em;\n'
    '}\n'
    '\n'
    'pre code {\n'
    '  background: transparent;\n'
    '  padding: 0;\n'
    '  white-space: pre-wrap;\n'
    '}\n'
    '\n'
    'code[class*="language-"],\n'
    'pre[class*="language-"] {\n'
    '  word-break: normal;\n'
    '  white-space: pre-wrap;\n'
    '}\n'
    '\n'
    '/* テーブル --------------------------------------------------- */\n'
    '\n'
    'table {\n'
    '  border-collapse: collapse;\n'
    '  width: 100%;\n'
    '  margin: 1.2em 0;\n'
    '  font-size: 0.95em;\n'
    '}\n'
    '\n'
    'th,\n'
    'td {\n'
    '  border: 1px solid rgba(130, 142, 166, 0.35);\n'
    '  padding: 6px 10px;\n'
    '  text-align: left;\n'
    '  word-break: break-all;\n'
    '}\n'
    '\n'
    'th {\n'
    '  background: #e3e2ff;\n'
    '  color: #383854;\n'
    '  font-weight: 600;\n'
    '}\n'
    '\n'
    'tbody tr:nth-child(even) td {\n'
    '  background: rgba(204, 234, 243, 0.25);\n'
    '}\n'
    '\n'
    '/* 印刷 ------------------------------------------------------ */\n'
    '\n'
    '@page {\n'
    '  size: A4 portrait;\n'
    '  margin: 12mm 14mm;\n'
    '  background: linear-gradient(180deg, #f9f9ff 0%, #f5f4ff 45%, #f7fbff 100%);\n'
    '}\n'
    '\n'
    '@media print {\n'
    '  html {\n'
    '    -webkit-print-color-adjust: exact;\n'
    '    print-color-adjust: exact;\n'
    '  }\n'
    '\n'
    '  body {\n'
    '    margin: 0;\n'
    '    background: transparent;\n'
    '  }\n'
    '}\n'
    '\n'
    '/* finite printout でも行色を維持 */\n'
    'html[data-printout]:not([data-printout="infinite"]) {\n'
    '  -webkit-print-color-adjust: exact !important;\n'
    '  print-color-adjust: exact !important;\n'
    '}\n'
    '\n'
    'html[data-printout]:not([data-printout="infinite"]) body,\n'
    'html[data-printout]:not([data-printout="infinite"]) article,\n'
    'html[data-printout]:not([data-printout="infinite"]) .karte-print-page,\n'
    'html[data-printout]:not([data-printout="infinite"]) .karte-print-page-content,\n'
    'html[data-printout]:not([data-printout="infinite"]) table,\n'
    'html[data-printout]:not([data-printout="infinite"]) thead,\n'
    'html[data-printout]:not([data-printout="infinite"]) tbody,\n'
    'html[data-printout]:not([data-printout="infinite"]) tr,\n'
    'html[data-printout]:not([data-printout="infinite"]) th,\n'
    'html[data-printout]:not([data-printout="infinite"]) td {\n'
    '  -webkit-print-color-adjust: exact !important;\n'
    '  print-color-adjust: exact !important;\n'
    '}\n'
    '\n'
    'html[data-printout]:not([data-printout="infinite"]) th {\n'
    '  background: #e3e2ff !important;\n'
    '  color: #383854 !important;\n'
    '}\n'
    '\n'
    'html[data-printout]:not([data-printout="infinite"]) tbody tr:nth-child(even) td {\n'
    '  background: rgba(204, 234, 243, 0.25) !important;\n'
    '}\n'
    '\n'
    '/* 水平線 ----------------------------------------------------- */\n'
    '\n'
    'hr {\n'
    '  border: none;\n'
    '  height: 2px;\n'
    '  margin: 1.6em 0;\n'
    '  background-image: linear-gradient(to right, transparent, #c2b4ff, transparent);\n'
    '}\n'
    '\n'
    '/* 画像 ------------------------------------------------------- */\n'
    '\n'
    'img {\n'
    '  max-width: 100%;\n'
    '  border-radius: 10px;\n'
    '  border: 1px solid rgba(204, 234, 243, 0.8);\n'
    '  box-shadow: 0 4px 12px rgba(130, 142, 166, 0.18);\n'
    '  margin: 0.6em 0;\n'
    '}\n'
    '\n'
    '/* インライン強調 -------------------------------------------- */\n'
    '\n'
    'strong {\n'
    '  color: #4b4b66;\n'
    '}\n'
    '\n'
    'em {\n'
    '  color: #828ea6;\n'
    '}\n'
    '\n'
    '</style>\n'
    '\n'
    '<style id="karte-pagination-style">@media print{\n'
    '.karte-page-break,.karte-break-before{display:block;height:0;margin:0;border:0;break-before:page;page-break-before:always;}\n'
    '.karte-page-break-after,.karte-break-after{display:block;height:0;margin:0;border:0;break-after:page;page-break-after:always;}\n'
    'h1,h2,h3,h4,h5,h6,.karte-keep-with-next{break-after:avoid-page;page-break-after:avoid;}\n'
    'figure,figcaption,img,table,pre,blockquote,.katex-display,.karte-chart,.karte-keep-together{break-inside:avoid-page;page-break-inside:avoid;}\n'
    'figcaption,caption{break-before:avoid-page;page-break-before:avoid;}\n'
    'thead{display:table-header-group;}\n'
    '}</style></head>\n'
    '<body>'
)


def fixture_html(body="<h1>synthetic document</h1>"):
    return _OFFICIAL_PREFIX + body + "\n</body>\n</html>"


def success():
    return {
        "schema_version": 1, "ok": True, "task_id": "desktop-synthetic", "doc_id": "synthetic-doc",
        "relative_path": "content/projects/synthetic/note/worklog-desktop-synthetic.md",
        "source_sha256": "a" * 64, "markdown_sha256": "b" * 64,
        "renderer_commit": renderer.RENDERER_COMMIT,
        "html": fixture_html(),
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


def test_accepted_official_html_bytes_are_unmodified_by_host(view):
    session, url, _ = view
    body = fixture_html('<h1>日本語 😀</h1><p>&lt;script&gt; &amp; &#39; &#x27;</p>')
    value = copy.deepcopy(success())
    value["html"] = body
    session._read_view = lambda: value
    assert request(url, child=True)[2] == body.encode()
    # Browser execution/network isolation is independently tested in headless Chrome.


@pytest.mark.parametrize("body", [
    '<h1>日本語 &amp; &lt; &gt; &quot; &apos; &#1; &#9; &#10; &#13; &#39; &#x27; 😀</h1>',
    '<h1>記録</h1><p>https://example.invalid file:///secret &lt;script&gt;literal&lt;/script&gt;</p>',
    '<h1>記録</h1><blockquote><p>line 1<br>line 2</p><pre><code>    &lt;tag&gt; &amp; code\n</code></pre></blockquote>',
    '<h1>記録</h1><h3>start</h3><p>出所: host_observation</p><blockquote><p>本文</p></blockquote>',
])
def test_passive_profile_preserves_text_entities_and_local_worklog_blocks(body):
    source = fixture_html(body)
    assert renderer._html_bytes(source) == source.encode()


@pytest.mark.parametrize("payload", [
    '<script>window.BAD=1</script>', '<iframe src="http://127.0.0.1:1/"></iframe>',
    '<object data="http://127.0.0.1:1/"></object>', '<img src="http://127.0.0.1:1/">',
    '<link rel="preconnect" href="http://127.0.0.1:1/">',
    '<link rel="dns-prefetch" href="//private.invalid">',
    '<link rel="preload" as="image" href="http://127.0.0.1:1/">',
    '<a href="http://127.0.0.1:1/">link</a>', '<a href="file:///secret">file</a>',
    '<a href="custom:launch">custom protocol</a>', '<a href="data:text/html,unsafe">data</a>',
    '<form action="http://127.0.0.1:1/"><input name="secret"></form>',
    '<meta http-equiv="refresh" content="0;url=http://127.0.0.1:1/">',
    '<base href="http://127.0.0.1:1/">', '<template><img src="http://127.0.0.1:1/"></template>',
    '<svg><a href="http://127.0.0.1:1/">link</a></svg>', '<math><mtext>text</mtext></math>',
    '<p onclick="evil()">click</p>', '<p style="background:url(http://127.0.0.1:1/)">style</p>',
    '<p id="custom">extra attribute</p>', '<p class="karte">extra class</p>',
    '<style>@import "http://127.0.0.1:1/";</style>', '<style>p{color:red}</style>',
    '<!--[if IE]><img src="http://127.0.0.1:1/"><![endif]-->',
    '<![CDATA[<img src="http://127.0.0.1:1/">]]>', '<?xml version="1.0"?>',
    '<noscript><img src="http://127.0.0.1:1/"></noscript>',
    '<textarea>raw text</textarea>', '<plaintext><img src="http://127.0.0.1:1/">',
])
def test_active_resource_navigation_and_unknown_markup_rejected_before_delivery(view, payload):
    session, url, _ = view
    source = fixture_html('<h1>ATTACKER CONTENT MUST NOT BE DELIVERED</h1>' + payload)
    with pytest.raises(InspectionError, match="^karte_renderer_unsupported_html$"):
        renderer._html_bytes(source)
    session._read_view = lambda: {"html": source}
    status, _, body = request(url, child=True)
    assert status == 503 and b"ATTACKER" not in body and payload.encode() not in body


@pytest.mark.parametrize("body", [
    '<h1>text</h1><p>unclosed', '<h1>text</h1><p><p>implicit closing</p></p>',
    '<h1>text</h1><blockquote><h3>wrong parent</h3></blockquote>',
    '<h1>text</h1><pre><p>wrong parent</p></pre>',
    '<h1>text</h1><p><blockquote>wrong parent</blockquote></p>',
    '<h1>text</h1><p>text</P>', '<h1>text</h1><p>text</p >',
    '<H1>case recovery</H1>', '<h1 >whitespace recovery</h1>',
    '<h1>text</h1><p>line<br/>line</p>', '<h1>text</h1><p>line<br />line</p>',
    '<h1>text</h1><p>&amp missing semicolon</p>', '<h1>text</h1><p>&#60 missing semicolon</p>',
    '<h1>text</h1><p>&unknown;</p>', '<h1>text</h1><p>&NewLine;</p>',
    '<h1>text</h1><p>&#0;</p>', '<h1>text</h1><p>&#xD800;</p>',
    '<h1>text</h1><p>&#1114112;</p>', '<h1>text</h1><p>&#999999999999999;</p>',
    '<h1>text</h1><p>bare & character</p>', '<h1>text</h1><p>bare < character</p>',
    '<h1>text</h1><h1>duplicate document</h1>', '<p>missing document heading</p>',
])
def test_parser_recovery_ambiguous_entities_and_invalid_nesting_reject(body):
    with pytest.raises(InspectionError, match="^karte_renderer_unsupported_html$"):
        renderer._html_bytes(fixture_html(body))


@pytest.mark.parametrize(("before", "after"), [
    ("<!doctype html>", "<!DOCTYPE html>"), ("<!doctype html>", ""),
    ("<!doctype html>", '<!doctype html SYSTEM "http://private.invalid/entity">'),
    ('<html lang="en">', '<html lang="en" onload="evil()">'),
    ('<html lang="en">', '<html lang="en" lang="en">'),
    ('<html lang="en">', "<html lang='en'>"),
    ('<meta charset="utf-8">', '<meta charset="utf-7">'),
    ('<meta charset="utf-8">', '<meta charset="utf-8"/>'),
    ('<title>', '<title><script>evil()</script>'),
    ('<style id="karte-renderer-css">', '<style id="karte-renderer-css" nonce="x">'),
    ('body {', 'body{'), ("#cceaf3", "#cceaf4"),
    ("</head>", '<style>p{color:red}</style></head>'),
    ("</head>", '<link rel="preconnect" href="http://127.0.0.1:1/"></head>'),
    ("</html>", "</html><html>"), ("</html>", "</html><!--trailing comment-->"),
    ("<body>", "<body><body>"), ("</body>", "</body></body>"),
])
def test_only_exact_pinned_head_styles_and_document_envelope_allowed(before, after):
    source = fixture_html()
    assert before in source
    with pytest.raises(InspectionError, match="^karte_renderer_unsupported_html$"):
        renderer._html_bytes(source.replace(before, after, 1))


def test_initial_helper_result_is_validated_before_ui_can_publish_url(monkeypatch, config, helper):
    value = success()
    value["html"] = fixture_html('<h1>unsafe</h1><link rel="preconnect" href="http://127.0.0.1:1/">')
    monkeypatch.setattr(renderer, "_capture", lambda *args: json.dumps(value).encode())
    with pytest.raises(InspectionError, match="^karte_renderer_unsupported_html$"):
        KarteRenderer(config).render("desktop-synthetic")


def snapshot_success():
    import base64

    value = success()
    markdown = "# Synthetic 日本語\n".encode()
    value.update(schema_version=2, markdown_base64=base64.b64encode(markdown).decode(),
                 markdown_sha256=hashlib.sha256(markdown).hexdigest())
    return value, markdown


def test_snapshot_protocol_preserves_exact_authorized_bytes_and_v1(monkeypatch, config, helper):
    value, markdown = snapshot_success()
    calls = []
    monkeypatch.setattr(renderer, "_capture", lambda *args: calls.append(args) or json.dumps(value).encode())
    actual = KarteRenderer(config).snapshot("desktop-synthetic")
    assert actual["markdown"] == markdown
    assert "markdown_base64" not in actual
    assert actual["html"] == success()["html"]
    assert calls == [([str(helper), "--root", str(config.karte_root)],
                     b'{"schema_version":2,"operation":"snapshot","task_id":"desktop-synthetic"}')]
    monkeypatch.setattr(renderer, "_capture", lambda *args: json.dumps(success()).encode())
    assert KarteRenderer(config).render("desktop-synthetic") == success()


@pytest.mark.parametrize(("key", "bad"), [
    ("schema_version", 1), ("schema_version", True), ("ok", 1), ("task_id", "other"),
    ("doc_id", "../doc"), ("relative_path", "../outside.md"), ("renderer_commit", "wrong"),
    ("source_sha256", "z" * 64), ("markdown_sha256", "a" * 64), ("metadata", {"schemaVersion": True}),
    ("markdown_base64", None), ("markdown_base64", ""), ("markdown_base64", []),
    ("markdown_base64", "aGk=\n"), ("markdown_base64", "%%%="), ("markdown_base64", "aGl="),
    ("html", "<script>attack</script>"), ("extra", "unexpected"),
])
def test_snapshot_response_controls(monkeypatch, config, helper, key, bad):
    value, _markdown = snapshot_success()
    value[key] = bad
    monkeypatch.setattr(renderer, "_capture", lambda *args: json.dumps(value).encode())
    with pytest.raises(InspectionError):
        KarteRenderer(config).snapshot("desktop-synthetic")


@pytest.mark.parametrize("markdown", [b"", b"\xff", b"a\x00b"])
def test_snapshot_rejects_invalid_decoded_bytes_even_with_matching_hash(monkeypatch, config, helper, markdown):
    import base64

    value, _ = snapshot_success()
    value.update(markdown_base64=base64.b64encode(markdown).decode(), markdown_sha256=hashlib.sha256(markdown).hexdigest())
    monkeypatch.setattr(renderer, "_capture", lambda *args: json.dumps(value).encode())
    with pytest.raises(InspectionError, match="karte_renderer_invalid_response"):
        KarteRenderer(config).snapshot("desktop-synthetic")


def test_snapshot_errors_and_output_limit_never_fall_back(monkeypatch, config, helper):
    calls = []
    for code in sorted(renderer._HELPER_ERRORS):
        value = {"schema_version": 2, "ok": False, "error": code}
        monkeypatch.setattr(renderer, "_capture", lambda *args, value=value: calls.append(args) or json.dumps(value).encode())
        with pytest.raises(InspectionError, match="^karte_renderer_" + code + "$"):
            KarteRenderer(config).snapshot("desktop-synthetic")
    assert len(calls) == len(renderer._HELPER_ERRORS)
    monkeypatch.setattr(renderer, "_capture", lambda *args: b" " * (renderer.KARTE_MAX_STDOUT + 1))
    with pytest.raises(InspectionError, match="karte_output_limit"):
        KarteRenderer(config).snapshot("desktop-synthetic")


def test_owned_active_wrapper_handoff_and_exception_are_not_cached(view):
    session, url, _calls = view
    sent = []
    session.open_in_browser(url, sent.append)
    assert sent == [url]
    with pytest.raises(InspectionError, match="karte_browser_failed"):
        session.open_in_browser(url, lambda _url: False)
    with pytest.raises(OSError, match="synthetic"):
        session.open_in_browser(url, lambda _url: (_ for _ in ()).throw(OSError("synthetic")))
    assert session.active
    session.open_in_browser(url, sent.append)
    assert sent == [url, url]


@pytest.mark.parametrize("mutation", ["external", "host", "ipv6", "port", "token", "child", "query", "fragment",
                                      "userinfo", "encoded", "space", "newline", "file", "javascript", "https"])
def test_browser_guard_rejects_every_non_owned_url(view, mutation):
    session, url, _calls = view
    parsed = urlsplit(url)
    bad = {
        "external": "http://example.invalid/", "host": url.replace("127.0.0.1", "localhost"),
        "ipv6": url.replace("127.0.0.1", "[::1]"), "port": url.replace(str(parsed.port), str(parsed.port + 1)),
        "token": url[:-2] + ("0" if url[-2] != "0" else "1") + "/", "child": url + "document",
        "query": url + "?x", "fragment": url + "#x", "userinfo": url.replace("http://", "http://user@"),
        "encoded": url.replace(parsed.path, "/%" + parsed.path[1:]), "space": " " + url,
        "newline": url + "\n", "file": "file:///C:/private", "javascript": "javascript:alert(1)",
        "https": url.replace("http:", "https:"),
    }[mutation]
    sent = []
    with pytest.raises(InspectionError, match="karte_view_closed"):
        session.open_in_browser(bad, sent.append)
    assert sent == []


def test_browser_guard_rejects_unstarted_expired_closed_and_nested_calls(view):
    session, url, _calls = view
    sent = []
    unstarted = KarteViewSession(success)
    with pytest.raises(InspectionError):
        unstarted.open_in_browser(url, sent.append)

    def reentrant(value):
        sent.append(value)
        with pytest.raises(InspectionError):
            session.open_in_browser(url, sent.append)

    session.open_in_browser(url, reentrant)
    assert sent == [url]
    session._deadline = time.monotonic() - 1
    with pytest.raises(InspectionError):
        session.open_in_browser(url, sent.append)
    session.close()
    with pytest.raises(InspectionError):
        session.open_in_browser(url, sent.append)
    assert sent == [url]


def test_close_serializes_with_short_os_dispatch(view):
    session, url, _calls = view
    started, closed = threading.Event(), threading.Event()
    threads = []

    def close():
        started.set()
        session.close()
        closed.set()

    def opener(_url):
        thread = threading.Thread(target=close)
        threads.append(thread)
        thread.start()
        assert started.wait(2)
        assert not closed.wait(0.02)

    session.open_in_browser(url, opener)
    assert closed.wait(2)
    for thread in threads:
        thread.join(2)
    assert not session.active
