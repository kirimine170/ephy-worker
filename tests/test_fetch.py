from __future__ import annotations

import asyncio
import gzip
import socket
from contextlib import asynccontextmanager

import pytest

from ephy_worker.budget import Budget
from ephy_worker.fetch import PublicFetcher, UnsafeURL, _PinnedResolver, validate_public_url
from ephy_worker.schema import Candidate, Limits

HTML = b"""<!doctype html><html><head><title>Fixture research</title></head>
<body><article><h1>Public evidence</h1><p>Alpha version 2 applies only after September 2026.
This complete body contains the actual research evidence, not the search snippet.</p>
<p>Independent testing remains unavailable and limitations must be preserved.</p></article></body></html>"""


class Response:
    def __init__(self, status=200, body=HTML, headers=None, delay=0):
        self.status = status
        self.body = body
        self.headers = headers or {"content-type": "text/html"}
        self.delay = delay

    async def iter_bytes(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        for start in range(0, len(self.body), 31):
            yield self.body[start : start + 31]


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    @asynccontextmanager
    async def request(self, url, addresses, timeout):
        self.calls.append((url, tuple(addresses), timeout))
        yield next(self.responses)

    async def aclose(self):
        self.closed = True


async def public_dns(host, port):
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/a",
        "http://user:password@example.org/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/",
        "http://10.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://localhost/",
        "http://server.internal/",
        "https://example.org\\@127.0.0.1",
        "https://example.org/\n",
        "http://[fe80::1%25en0]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
    ],
)
def test_disallowed_url_forms(url):
    with pytest.raises(UnsafeURL):
        validate_public_url(url)


@pytest.mark.parametrize("url", ["http://2130706433/", "http://0177.0.0.1/", "http://0x7f000001/"])
async def test_numeric_ipv4_variants_resolve_to_private_without_http(url):
    budget = Budget(Limits())
    transport = Transport([])
    fetcher = PublicFetcher(budget, transport=transport)
    source = await fetcher.fetch(Candidate(url=url), "s1")
    assert source.status == "blocked"
    assert not transport.calls


async def test_dns_answers_all_checked_before_request():
    async def mixed_dns(host, port):
        return ["93.184.216.34", "127.0.0.1"]

    transport = Transport([])
    source = await PublicFetcher(Budget(Limits()), transport=transport, resolver=mixed_dns).fetch(
        Candidate(url="https://example.org/"), "s1"
    )
    assert source.status == "blocked"
    assert not transport.calls


async def test_private_redirect_rejected_without_second_request():
    budget = Budget(Limits())
    transport = Transport([Response(302, headers={"location": "http://169.254.169.254/latest"})])
    fetcher = PublicFetcher(budget, transport=transport, resolver=public_dns)
    source = await fetcher.fetch(Candidate(url="https://example.org/"), "s1")
    assert source.status == "blocked"
    assert len(transport.calls) == budget.counts["fetch_requests"] == 1


async def test_redirect_dns_is_checked_again():
    hosts = []

    async def dns(host, port):
        hosts.append(host)
        return ["93.184.216.34"] if host == "example.org" else ["192.168.0.1"]

    transport = Transport([Response(302, headers={"location": "https://evil.example/"})])
    source = await PublicFetcher(Budget(Limits()), transport=transport, resolver=dns).fetch(
        Candidate(url="https://example.org/"), "s1"
    )
    assert source.status == "blocked"
    assert hosts == ["example.org", "evil.example"]
    assert len(transport.calls) == 1


async def test_checked_dns_is_pinned_without_second_resolution(monkeypatch):
    calls = []

    async def dns(host, port):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    transport = Transport([Response(403)])
    fetcher = PublicFetcher(Budget(Limits()), transport=transport, resolver=dns)
    source = await fetcher.fetch(Candidate(url="https://example.org/"), "s1")
    assert source.status == "http_error"
    assert calls == ["example.org"]
    pinned = _PinnedResolver("example.org", 443, transport.calls[0][1])
    result = await pinned.resolve("example.org", 443, socket.AF_UNSPEC)
    assert [r["host"] for r in result] == ["93.184.216.34"]
    with pytest.raises(UnsafeURL):
        await pinned.resolve("evil.example", 443, socket.AF_UNSPEC)


async def test_html_from_pdf_named_url_preserves_actual_kind_and_body():
    budget = Budget(Limits())
    transport = Transport([Response()])
    source = await PublicFetcher(budget, transport=transport, resolver=public_dns).fetch(
        Candidate(url="https://example.org/download.pdf", snippet="FAKE SEARCH CLAIM", query_id="q1"), "s7"
    )
    assert source.kind == "html" and source.status == "ok"
    assert source.passages and source.body_hash
    assert all(p.source_id == "s7" for p in source.passages)
    assert "FAKE SEARCH CLAIM" not in "".join(p.text for p in source.passages)
    assert budget.counts["cache_bytes"] > len(HTML)
    assert source.query_ids == ["q1"]


async def test_false_pdf_content_type_detects_html():
    transport = Transport([Response(headers={"content-type": "application/pdf"})])
    source = await PublicFetcher(Budget(Limits()), transport=transport, resolver=public_dns).fetch(
        Candidate(url="https://example.org/paper"), "s1"
    )
    assert source.kind == "html" and source.status == "ok"
    assert any("HTML" in limitation for limitation in source.limitations)


async def test_non_pdf_binary_is_not_pdf_success():
    transport = Transport([Response(body=b"broken bytes", headers={"content-type": "application/pdf"})])
    source = await PublicFetcher(Budget(Limits()), transport=transport, resolver=public_dns).fetch(
        Candidate(url="https://example.org/a.pdf"), "s1"
    )
    assert source.status == "invalid" and not source.passages


async def test_decompressed_limit_rejects_compression_bomb():
    body = gzip.compress(HTML + b"a" * 2_000_000)
    transport = Transport(
        [Response(body=body, headers={"content-type": "text/html", "content-encoding": "gzip"})]
    )
    budget = Budget(Limits(html_bytes=1024))
    source = await PublicFetcher(budget, transport=transport, resolver=public_dns).fetch(
        Candidate(url="https://example.org/"), "s1"
    )
    assert source.status == "limit" and source.error == "html_bytes"
    assert source.bytes_received > 1024
    assert source.bytes_received <= 1024 + 64 * 1024
    assert not source.passages


async def test_gzip_valid_content_and_truncated_encoding():
    for payload, expected in [(gzip.compress(HTML), "ok"), (gzip.compress(HTML)[:-4], "invalid")]:
        transport = Transport(
            [Response(body=payload, headers={"content-type": "text/html", "content-encoding": "gzip"})]
        )
        source = await PublicFetcher(Budget(Limits()), transport=transport, resolver=public_dns).fetch(
            Candidate(url="https://example.org/"), "s1"
        )
        assert source.status == expected


async def test_cache_budget_and_request_budget_are_finite():
    budget = Budget(Limits(fetch_requests=1))
    transport = Transport([Response(302, headers={"location": "/next"})])
    source = await PublicFetcher(budget, transport=transport, resolver=public_dns).fetch(
        Candidate(url="https://example.org/"), "s1"
    )
    assert source.status == "limit" and source.error == "fetch_requests"
    assert len(transport.calls) == 1
    transport = Transport([Response(body=HTML * 20)])
    source = await PublicFetcher(
        Budget(Limits(cache_bytes=1024)), transport=transport, resolver=public_dns
    ).fetch(Candidate(url="https://example.org/"), "s1")
    assert source.status == "limit" and source.error == "cache_bytes"


async def test_redirect_limit_and_timeout():
    transport = Transport([Response(302, headers={"location": "/next"})])
    source = await PublicFetcher(
        Budget(Limits(max_redirects=0)), transport=transport, resolver=public_dns
    ).fetch(Candidate(url="https://example.org/"), "s1")
    assert source.status == "limit" and source.error == "max_redirects"
    transport = Transport([Response(delay=1)])
    source = await PublicFetcher(
        Budget(Limits(request_seconds=0.03)), transport=transport, resolver=public_dns
    ).fetch(Candidate(url="https://example.org/"), "s1")
    assert source.status == "timeout"


async def test_cancellation_propagates_and_transport_closes():
    transport = Transport([Response(delay=10)])
    fetcher = PublicFetcher(Budget(Limits()), transport=transport, resolver=public_dns)
    task = asyncio.create_task(fetcher.fetch(Candidate(url="https://example.org/"), "s1"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await fetcher.aclose()
    assert transport.closed


async def test_real_transport_keeps_tls_and_disables_proxy_cookie_redirect_retry(monkeypatch):
    from ephy_worker import fetch

    observed = {}

    class FakeConnector:
        def __init__(self, **kwargs):
            observed["connector"] = kwargs

    class FakeSession:
        def __init__(self, **kwargs):
            observed["session"] = kwargs
            self._retry_connection = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def get(self, url, **kwargs):
            observed["request"] = kwargs
            observed["retry"] = self._retry_connection
            yield Response(403)

    monkeypatch.setattr(fetch.aiohttp, "TCPConnector", FakeConnector)
    monkeypatch.setattr(fetch.aiohttp, "ClientSession", FakeSession)
    transport = fetch.AioHTTPTransport()
    async with transport.request("https://example.org/", ["93.184.216.34"], 5) as response:
        assert response.status == 403
    assert observed["connector"]["ssl"] is True
    assert isinstance(observed["connector"]["resolver"], _PinnedResolver)
    assert observed["session"]["trust_env"] is False
    assert observed["session"]["auto_decompress"] is False
    assert isinstance(observed["session"]["cookie_jar"], fetch.aiohttp.DummyCookieJar)
    assert observed["request"] == {"allow_redirects": False, "ssl": True}
    assert observed["retry"] is False


async def test_pdf_magic_is_used_with_generic_content_type():
    import io

    from reportlab.pdfgen.canvas import Canvas

    stream = io.BytesIO()
    canvas = Canvas(stream)
    canvas.drawString(50, 750, "A complete text PDF with explicit conditions.")
    canvas.showPage()
    canvas.save()
    transport = Transport(
        [Response(body=stream.getvalue(), headers={"content-type": "application/octet-stream"})]
    )
    source = await PublicFetcher(Budget(Limits()), transport=transport, resolver=public_dns).fetch(
        Candidate(url="https://example.org/download?id=42"), "s1"
    )
    assert source.status == "ok" and source.kind == "pdf"
    assert source.passages[0].page == 1
    assert any("Content-Type" in note for note in source.limitations)
