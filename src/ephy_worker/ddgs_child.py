"""Private short-lived DDGS parser process；stdout is bounded admission IPC only．"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from types import SimpleNamespace

from .ddgs_search import ENDPOINTS
from .search import SearchError, retry_after_seconds, validate_query


def _send(message):
    data = json.dumps(message, ensure_ascii=True, allow_nan=False).encode()
    if len(data) > 256 * 1024 - 2:
        raise SearchError("response_size_limit")
    sys.__stdout__.buffer.write(data + b"\n")
    sys.__stdout__.buffer.flush()


def run(spec):
    import primp
    from ddgs.engines import ENGINES
    from ddgs.http_client import HttpClient

    query = validate_query(spec["query"])
    backend = spec["backend"]
    if backend not in ENDPOINTS or backend not in ENGINES["text"]:
        raise SearchError("dependency_error")
    engine_class = ENGINES["text"][backend]
    if engine_class.disabled:
        raise SearchError("dependency_error")
    clients = []

    def initialize(self, proxy=None, timeout=None, *, verify=True):
        if proxy or verify is not True:
            raise SearchError("endpoint_refused")
        self.client = primp.Client(timeout=spec["timeout"], impersonate="chrome_146",
                                   impersonate_os="windows", follow_redirects=False,
                                   max_redirects=0, https_only=True, verify=True)
        clients.append(self.client)

    def request(self, method, url, **kwargs):
        if url != ENDPOINTS[backend] or method != ("POST" if backend == "duckduckgo" else "GET"):
            raise SearchError("endpoint_refused")
        _send({"type": "request", "method": method, "url": url})
        permission = sys.stdin.buffer.readline(128)
        if permission != b'{"permit":true}\n':
            raise SearchError("endpoint_refused")
        response = None
        try:
            response = self.client.request(method, url, stream=True, **kwargs)
            status = response.status_code
            retry = retry_after_seconds(response.headers.get("retry-after"))
            if status != 200:
                code = {202: "challenge", 429: "rate_limited", 403: "access_denied"}.get(
                    status, "redirect_refused" if 300 <= status < 400 else "http_error")
                raise SearchError(code, status=status, retry_after=retry)
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
                "text/html", "application/xhtml+xml"
            }:
                raise SearchError("invalid_content_type")
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > spec["bytes"]:
                    raise SearchError("response_size_limit")
                content.extend(chunk)
            text = bytes(content).decode("utf-8", errors="strict")
            lower = text.lower()
            if any(marker in lower for marker in (
                'id="anomaly-form"', 'id="challenge-form"', "g-recaptcha", "h-captcha",
                "verify you are human", "unusual traffic from your computer", "cf-chl-", "captcha-form",
            )):
                raise SearchError("challenge", retry_after=retry)
            return SimpleNamespace(status_code=status, text=text, content=bytes(content))
        except primp.TimeoutError:
            raise SearchError("timeout") from None
        except (UnicodeError, ValueError):
            raise SearchError("parse_error") from None
        except SearchError:
            raise
        except Exception:  # noqa: BLE001 — native transport exceptions never expose raw upstream data．
            raise SearchError("network_error") from None
        finally:
            if response is not None:
                response.close()

    HttpClient.__init__ = initialize
    HttpClient.request = request
    # Some DDGS engines select random UAs/tracking paths．Never transmit those．
    engine_class.headers_update = {}
    try:
        engine = engine_class(proxy=None, timeout=spec["timeout"], verify=True)
        payload = engine.build_payload(query, spec["region"], "moderate", None, 1)
        kwargs = {"params" if engine.search_method == "GET" else "data": payload}
        response = engine.http_client.request(engine.search_method, ENDPOINTS[backend], **kwargs)
        try:
            rows = engine.post_extract_results(engine.extract_results(response.text))
            results = [{key: getattr(item, key) for key in ("title", "href", "body")}
                       for item in rows[:spec["maximum"]]]
        except Exception:  # noqa: BLE001 — HTML parser errors cross the IPC boundary as stable codes．
            raise SearchError("parse_error") from None
        if not results:
            raise SearchError("indeterminate_results")
        _send({"type": "result", "results": results})
    finally:
        # primp has no Client.close；the owning short-lived process releases
        # native client pools on exit．Each streaming Response is closed above．
        clients.clear()


def main():
    try:
        line = sys.stdin.buffer.readline(16384)
        if len(line) >= 16384:
            raise SearchError("invalid_request")
        spec = json.loads(line)
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            run(spec)
    except SearchError as exc:
        _send({"type": "error", "code": exc.code, "status": exc.status, "retry_after": exc.retry_after})
    except Exception:  # noqa: BLE001 — the process boundary must never serialize arbitrary exceptions．
        _send({"type": "error", "code": "dependency_error"})


if __name__ == "__main__":
    main()
