"""Pinned Karte HTML adapter and an owned, revocable read-only view.

The helper owns authorization and Markdown rendering. This module never reads a
record file, generates Markdown, or interprets document markup. Browser dispatch
is limited to the active session URL and an injected operating-system handoff.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import DesktopConfig
from .inspection import KARTE_MAX_STDOUT, InspectionError, _capture, _json, _token

RENDERER_COMMIT = "30e5dfbab03f92e2eb7c8a67e17936579075fe88"
KARTE_BASE_COMMIT = "9a46168ff7272185c3eca38d4fff645e836492b3"
GO_VERSION = "go1.27.0"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SUCCESS_FIELDS = {
    "schema_version", "ok", "task_id", "doc_id", "relative_path", "source_sha256",
    "markdown_sha256", "renderer_commit", "html", "metadata",
}
_PROVENANCE_FIELDS = {
    "schema_version", "helper_sha256", "renderer_commit", "karte_base_commit",
    "karte_patch_sha256", "go_version",
}
# Extended only when the fixed helper protocol explicitly defines another code.
_HELPER_ERRORS = {
    "invalid_request", "permission_denied", "invalid_record", "view_missing",
    "view_conflict", "v2_conflict", "read_failed", "render_failed", "output_limit",
}
_CSP = (
    "default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; "
    "font-src 'none'; object-src 'none'; media-src 'none'; worker-src 'none'; "
    "manifest-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; "
)
_WRAPPER_CSP = _CSP + "frame-src 'self'; frame-ancestors 'none'"
_DOCUMENT_CSP = _CSP + "frame-src 'none'; frame-ancestors 'self'; sandbox"

# Exact style element contents emitted by the pinned renderer's built-in
# fallback layout for the existing worklog generator. No theme/CSS override is
# accepted. The code below validates transport; it never rewrites any markup.
_STYLE_HASHES = {
    "karte-renderer-css": "c8d20297d27faa9abbe34c9e6c2876fce66b6d3436ab41209491271ec01a3d05",
    "karte-pagination-style": "f53301693e63013b69e49339109ceaeb0ec49b83934291a8e2a42e43a16ad538",
}
_HEAD_TOKENS = (
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    "<title>", '<style id="karte-renderer-css">', '<style id="karte-pagination-style">',
)
_TEXT_TAGS = {"title", "h1", "h3", "p", "code"}
_BODY_PARENTS = {
    "h1": {"body"}, "h3": {"body"}, "p": {"body", "blockquote"},
    "blockquote": {"body"}, "pre": {"body", "blockquote"}, "code": {"pre"}, "br": {"p"},
}


class _PassiveWorklogHTML(HTMLParser):
    """Reject anything outside the pinned worklog output's passive grammar.

    HTMLParser is deliberately not used as a browser-equivalent sanitizer.
    Strict nesting, exact lexical tokens, and full original-token reconstruction
    reject its recovery/normalization cases. Only known static CSS is accepted.
    Accepted bytes are returned by the caller without serialization or changes.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.phase = 0
        self.doctype = False
        self.head_index = 0
        self.body_started = False
        self.style_id = ""
        self.style_parts: list[str] = []

    @staticmethod
    def reject():
        raise InspectionError("karte_renderer_unsupported_html")

    def handle_decl(self, decl):
        if self.parts or self.stack or self.doctype or decl != "doctype html":
            self.reject()
        self.doctype = True
        self.parts.append("<!doctype html>")

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        parent = self.stack[-1] if self.stack else None
        if tag == "html":
            if parent is not None or self.phase != 0 or not self.doctype or raw != '<html lang="en">':
                self.reject()
            self.phase = 1
        elif tag == "head":
            if parent != "html" or self.phase != 1 or raw != "<head>":
                self.reject()
            self.phase = 2
        elif tag == "body":
            if parent != "html" or self.phase != 3 or raw != "<body>":
                self.reject()
            self.phase = 4
        elif parent == "head":
            if self.head_index >= len(_HEAD_TOKENS) or raw != _HEAD_TOKENS[self.head_index]:
                self.reject()
            self.head_index += 1
            if tag == "style":
                self.style_id = dict(attrs)["id"]
                self.style_parts = []
        elif tag in _BODY_PARENTS and parent in _BODY_PARENTS[tag] and raw == f"<{tag}>":
            if parent == "body":
                if (not self.body_started and tag != "h1") or (self.body_started and tag == "h1"):
                    self.reject()
                self.body_started = True
        else:
            self.reject()
        self.parts.append(raw)
        if tag not in {"meta", "br"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.reject()
        if tag == "style":
            css = "".join(self.style_parts).encode("utf-8")
            if hashlib.sha256(css).hexdigest() != _STYLE_HASHES[self.style_id]:
                self.reject()
            self.style_id = ""
            self.style_parts = []
        elif tag == "head":
            if self.head_index != len(_HEAD_TOKENS) or self.phase != 2:
                self.reject()
            self.phase = 3
        elif tag == "body":
            if not self.body_started or self.phase != 4:
                self.reject()
            self.phase = 5
        elif tag == "html":
            if self.phase != 5:
                self.reject()
            self.phase = 6
        self.stack.pop()
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        parent = self.stack[-1] if self.stack else None
        if parent == "style":
            self.style_parts.append(data)
        elif parent in _TEXT_TAGS:
            if "<" in data or "&" in data:
                self.reject()
        elif data.strip(" \t\r\n"):
            self.reject()
        self.parts.append(data)

    def handle_entityref(self, name):
        if not self.stack or self.stack[-1] not in _TEXT_TAGS or name not in {"amp", "lt", "gt", "quot", "apos"}:
            self.reject()
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        if (
            not self.stack or self.stack[-1] not in _TEXT_TAGS
            or re.fullmatch(r"(?:[0-9]{1,7}|[xX][0-9a-fA-F]{1,6})", name) is None
        ):
            self.reject()
        value = int(name[1:], 16) if name.startswith(("x", "X")) else int(name)
        if not 1 <= value <= 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            self.reject()
        self.parts.append("&#" + name + ";")

    def handle_startendtag(self, tag, attrs):
        self.reject()

    def handle_comment(self, data):
        self.reject()

    def handle_pi(self, data):
        self.reject()

    def unknown_decl(self, data):
        self.reject()

    def validate(self, source):
        try:
            self.feed(source)
            self.close()
        except (ValueError, AssertionError, RecursionError):
            self.reject()
        if self.phase != 6 or self.stack or "".join(self.parts) != source:
            self.reject()


def _digest(value) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _helper_path() -> Path:
    # __file__ resolves inside PyInstaller's bundled package as well as source.
    return Path(__file__).resolve().parent / "karte_renderer" / "karte-worklog-render.exe"


def _verified_helper() -> Path:
    binary = _helper_path()
    provenance = binary.parent / "provenance.json"
    try:
        if binary.is_symlink() or provenance.is_symlink() or not binary.is_file():
            raise InspectionError("karte_renderer_helper_missing")
        if provenance.stat().st_size > 4096:
            raise InspectionError("karte_renderer_identity")
        value = _json(provenance.read_bytes(), "karte_renderer_identity")
        if (
            set(value) != _PROVENANCE_FIELDS or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["renderer_commit"] != RENDERER_COMMIT
            or value["karte_base_commit"] != KARTE_BASE_COMMIT or value["go_version"] != GO_VERSION
            or not _digest(value["helper_sha256"]) or not _digest(value["karte_patch_sha256"])
            or not 0 < binary.stat().st_size <= 64 * 1024 * 1024
        ):
            raise InspectionError("karte_renderer_identity")
        with binary.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != value["helper_sha256"]:
            raise InspectionError("karte_renderer_identity")
    except FileNotFoundError:
        raise InspectionError("karte_renderer_helper_missing") from None
    except OSError:
        raise InspectionError("karte_renderer_identity") from None
    return binary


def _html_bytes(value) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InspectionError("karte_renderer_invalid_response")
    try:
        body = value.encode("utf-8")
    except UnicodeError:
        raise InspectionError("karte_renderer_invalid_response") from None
    if len(body) > KARTE_MAX_STDOUT:
        raise InspectionError("karte_output_limit")
    _PassiveWorklogHTML().validate(value)
    return body


class KarteRenderer:
    """Read one task through the bundled helper, never the configured writer CLI."""

    def __init__(self, config: DesktopConfig):
        self.config = config

    def render(self, task_id: str) -> dict:
        return self._request(task_id, snapshot=False)

    def snapshot(self, task_id: str) -> dict:
        """Read exact authorized derived Markdown; protocol 2 is not worklog v2."""
        return self._request(task_id, snapshot=True)

    def _request(self, task_id: str, *, snapshot: bool) -> dict:
        _token(task_id)
        if self.config.karte_binary is None or self.config.karte_root is None:
            raise InspectionError("karte_not_configured")
        binary = _verified_helper()
        request = {"schema_version": 2, "operation": "snapshot", "task_id": task_id} if snapshot else {"task_id": task_id}
        schema = 2 if snapshot else 1
        raw = _capture(
            [str(binary), "--root", str(self.config.karte_root)],
            json.dumps(request, separators=(",", ":")).encode("ascii"),
        )
        if len(raw) > KARTE_MAX_STDOUT:
            raise InspectionError("karte_output_limit")
        value = _json(raw, "karte_renderer_invalid_response")
        if type(value.get("schema_version")) is not int or value["schema_version"] != schema:
            raise InspectionError("karte_renderer_invalid_response")
        if value.get("ok") is False:
            if set(value) == {"schema_version", "ok", "error"} and isinstance(value["error"], str) and value["error"] in _HELPER_ERRORS:
                raise InspectionError("karte_renderer_" + value["error"])
            raise InspectionError("karte_renderer_invalid_response")
        fields = _SUCCESS_FIELDS | {"markdown_base64"} if snapshot else _SUCCESS_FIELDS
        if set(value) != fields or value.get("ok") is not True:
            raise InspectionError("karte_renderer_invalid_response")
        if value["task_id"] != task_id:
            raise InspectionError("karte_task_mismatch")
        if value["renderer_commit"] != RENDERER_COMMIT:
            raise InspectionError("karte_renderer_identity")
        _token(value["doc_id"])
        path = value["relative_path"]
        if not isinstance(path, str) or re.fullmatch(
            r"content/projects/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/note/worklog-"
            + re.escape(task_id) + r"\.md", path,
        ) is None:
            raise InspectionError("karte_renderer_invalid_response")
        if not _digest(value["source_sha256"]) or not _digest(value["markdown_sha256"]):
            raise InspectionError("karte_renderer_invalid_response")
        metadata = value["metadata"]
        if not isinstance(metadata, dict) or type(metadata.get("schemaVersion")) is not int or metadata["schemaVersion"] != 1:
            raise InspectionError("karte_renderer_invalid_response")
        _html_bytes(value["html"])
        if snapshot:
            encoded = value.pop("markdown_base64")
            if not isinstance(encoded, str) or not encoded or len(encoded) > KARTE_MAX_STDOUT:
                raise InspectionError("karte_renderer_invalid_response")
            try:
                markdown = base64.b64decode(encoded, validate=True)
                markdown.decode("utf-8")
            except (ValueError, UnicodeError, binascii.Error):
                raise InspectionError("karte_renderer_invalid_response") from None
            if (
                not markdown or len(markdown) > KARTE_MAX_STDOUT or b"\x00" in markdown
                or base64.b64encode(markdown).decode("ascii") != encoded
                or hashlib.sha256(markdown).hexdigest() != value["markdown_sha256"]
            ):
                raise InspectionError("karte_renderer_invalid_response")
            value["markdown"] = markdown
        return value


class _LimitedReader:
    """Bound the complete request line/headers, including slow input."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = 16 * 1024
        self.deadline = time.monotonic() + 2

    def readline(self, size=-1):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise OSError("request limit")
        data = self.stream.readline(min(self.remaining + 1, size if size >= 0 else self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0 or time.monotonic() >= self.deadline:
            raise OSError("request limit")
        return data

    def close(self):
        self.stream.close()


class _ViewServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, session):
        self.session = session
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(("127.0.0.1", 0), _ViewHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.session._lock:
            if not self.session.active:
                self.slots.release()
                self.shutdown_request(request)
                return
            self.session._connections.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.session._lock:
                self.session._connections.discard(request)
            self.slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.session._lock:
                self.session._connections.discard(request)
            self.slots.release()

    def handle_error(self, request, client_address):
        # Never put URLs, HTML or filesystem diagnostics in stderr or a log.
        pass


class _ViewHandler(BaseHTTPRequestHandler):
    server: _ViewServer
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(1)
        super().setup()
        self.rfile = _LimitedReader(self.rfile)

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._respond(code, b"Request unavailable.", "text/plain; charset=utf-8")

    def _respond(self, status, body, content_type, *, child=False):
        self.close_connection = True
        session = self.server.session
        if not session.active:
            return
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", _DOCUMENT_CSP if child else _WRAPPER_CSP)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN" if child else "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("X-DNS-Prefetch-Control", "off")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=(), fullscreen=(), clipboard-read=(), clipboard-write=()")
            self.send_header("Connection", "close")
            if not session.active:
                return
            self.end_headers()
            for offset in range(0, len(body), 16384):
                if not session.active:
                    return
                self.wfile.write(body[offset:offset + 16384])
        except OSError:
            # The client or close()/TTL may revoke our owned connection.
            return

    def do_GET(self):
        session = self.server.session
        origin = f"http://{session._host}"
        unique = ("Host", "Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest")
        if (
            any(len(self.headers.get_all(key, [])) > 1 for key in unique)
            or self.headers.get("Host") != session._host
            or self.headers.get("Origin") not in {None, origin}
            or self.headers.get("Sec-Fetch-Mode") != "navigate"
            or self.headers.get("Content-Length") not in {None, "0"}
            or "Transfer-Encoding" in self.headers or "Authorization" in self.headers
        ):
            self.send_error(403)
            return
        if self.path == session._path:
            if self.headers.get("Sec-Fetch-Dest") != "document" or self.headers.get("Sec-Fetch-Site") not in {"none", "same-origin"}:
                self.send_error(403)
                return
            body = (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width">'
                '<link rel="icon" href="data:,"><title>Karte read-only view</title>'
                '<style>body{margin:0;font:14px system-ui;background:#f3f5f8;color:#172333}'
                'header{padding:12px 18px}iframe{display:block;border:0;width:100%;height:88vh;'
                'background:white}</style><header><strong>KarteRenderer read-only snapshot</strong>'
                '<br>Existing derived Markdown; the task ledger remains authoritative. '
                'Reload checks current read permission. Closing or expiry revokes this URL; '
                'an already displayed page remains in your browser.</header>'
                f'<iframe sandbox="" referrerpolicy="no-referrer" title="Karte document" '
                f'src="{session._path}document"></iframe></html>'
            ).encode()
            self._respond(200, body, "text/html; charset=utf-8")
            return
        if self.path != session._path + "document":
            self.send_error(404)
            return
        if self.headers.get("Sec-Fetch-Dest") != "iframe" or self.headers.get("Sec-Fetch-Site") != "same-origin":
            self.send_error(403)
            return
        if not session._render_slot.acquire(blocking=False):
            self.send_error(429)
            return
        try:
            if not session.active:
                return
            value = session._read_view()
            body = _html_bytes(value.get("html") if isinstance(value, dict) else None)
            if session.active:
                self._respond(200, body, "text/html; charset=utf-8", child=True)
        except InspectionError:
            # Permission denial must never fall back to previously delivered HTML.
            self._respond(503, b"Karte view unavailable. Return to Console and reload.", "text/plain; charset=utf-8", child=True)
        finally:
            session._render_slot.release()


class KarteViewSession:
    """A revocable loopback URL. start/close never launch or control a browser."""

    def __init__(self, read_view: Callable[[], dict], ttl_seconds: float = 300):
        if not callable(read_view) or isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not 0 < ttl_seconds <= 300:
            raise ValueError("A callable and a positive TTL no greater than 300 seconds are required")
        self._read_view = read_view
        self._ttl = ttl_seconds
        self._closed = threading.Event()
        self._lock = threading.RLock()
        self._opening = False
        self._render_slot = threading.BoundedSemaphore(1)
        self._connections: set[socket.socket] = set()
        self._server: _ViewServer | None = None
        self._deadline = 0.0
        self._path = "/" + secrets.token_hex(32) + "/"
        self._host = ""
        self._url = ""

    @property
    def active(self) -> bool:
        return self._server is not None and not self._closed.is_set() and time.monotonic() < self._deadline

    def start(self) -> str:
        with self._lock:
            if self._closed.is_set():
                raise InspectionError("karte_view_closed")
            if self._server is not None:
                if self.active:
                    return self._url
                raise InspectionError("karte_view_closed")
            try:
                server = _ViewServer(self)
            except OSError:
                raise InspectionError("karte_view_unavailable") from None
            self._server = server
            self._deadline = time.monotonic() + self._ttl
            self._host = f"127.0.0.1:{server.server_port}"
            self._url = f"http://{self._host}{self._path}"
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            threading.Thread(target=self._expire, daemon=True).start()
            return self._url

    def open_in_browser(self, url: str, opener: Callable[[str], object]) -> None:
        """Serialize the exact owned-URL handoff with close/expiry; never accept aliases."""
        with self._lock:
            if (
                not self.active or self._opening or not callable(opener)
                or re.fullmatch(r"/[0-9a-f]{64}/", self._path) is None
                or self._host != f"127.0.0.1:{self._server.server_port}"
                or url != self._url or url != f"http://{self._host}{self._path}"
            ):
                raise InspectionError("karte_view_closed")
            self._opening = True
            try:
                if opener(url) is False:
                    raise InspectionError("karte_browser_failed")
            finally:
                self._opening = False

    def _expire(self):
        if not self._closed.wait(self._ttl):
            self.close()

    def close(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            connections = tuple(self._connections)
            server = self._server
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Already closed by the request handler.
        if server is not None:
            # shutdown waits for serve_forever; Tk never waits on a helper or client.
            threading.Thread(target=self._shutdown, args=(server,), daemon=True).start()

    @staticmethod
    def _shutdown(server):
        server.shutdown()
        server.server_close()
