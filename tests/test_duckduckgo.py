from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import SearchConfig, WorkerConfig, load_config
from ephy_worker.duckduckgo import DuckDuckGoHTMLProvider
from ephy_worker.schema import Limits
from ephy_worker.search import QueryPrivacyError, SearchError, create_search_provider


def result(url="https://example.com/doc?v=2&utm_source=test#part"):
    return (
        '<div class="result results_links web-result"><h2><a class="result__a" '
        f'href="{url}">Public <b>documentation</b></a></h2>'
        '<a class="result__snippet">A &amp; B <strong>reference</strong></a></div>'
    )


def html_response(body, status=200):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def provider(handler, *, limits=None, **config):
    return create_search_provider(
        SearchConfig(provider="duckduckgo_html", **config),
        Budget(limits or Limits()), transport=httpx.MockTransport(handler),
    )


def test_configuration_and_example():
    config = SearchConfig(provider="duckduckgo_html")
    assert config.endpoint() == "https://html.duckduckgo.com/html/"
    assert config.resolve_api_key() is None
    for overrides in [
        {"base_url": "https://evil.example"}, {"base_url_env": "EVIL"},
        {"api_key_env": "KEY"}, {"engine": "google"}, {"language": "en"},
    ]:
        with pytest.raises(ValidationError):
            SearchConfig(provider="duckduckgo_html", **overrides)
    assert load_config("configs/worker.duckduckgo.example.yaml").search == config


async def test_direct_get_results_provenance_limit_and_local_health(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        assert str(request.url).startswith("https://html.duckduckgo.com/html/?q=")
        assert request.url.params["q"] == "Python documentation"
        assert request.headers["user-agent"] == "ephy-worker/0.1"
        assert "authorization" not in request.headers
        return html_response(result() + result("https://second.example/doc"))

    search = provider(handler, max_results=1)
    try:
        assert isinstance(search, DuckDuckGoHTMLProvider)
        assert (await search.engine_health())["network_probe"] is False
        assert not seen and search.budget.counts["search_requests"] == 0
        candidates = await search.search("Python documentation", "q1")
        assert len(candidates) == 1
        item = candidates[0]
        assert item.url == "https://example.com/doc?v=2"
        assert item.title == "Public documentation" and item.snippet == "A & B reference"
        assert item.query_id == "q1" and item.engine == "duckduckgo" and item.rank == 1
        assert item.discovered_at
        assert search.budget.counts["search_requests"] == 1
        assert search.metadata["provider"] == "duckduckgo_html"
        assert search.last_diagnostic["result_count"] == 1
    finally:
        await search.aclose()


@pytest.mark.parametrize("body,error", [
    ("<html>new upstream layout</html>", "unrecognized_html"),
    ('<div class="result"><a class="result__a" href="https://example.com">x', "invalid_html"),
    ('<div class="result">missing anchor</div>', "invalid_result"),
    ('<form id="challenge-form" action="//duckduckgo.com/anomaly.js"></form>', "challenge"),
    ('<div class="anomaly-modal">solve</div>' + result(), "challenge"),
])
async def test_unrecognized_malformed_and_challenge_are_errors(body, error):
    search = provider(lambda req: html_response(body))
    try:
        with pytest.raises(SearchError, match=error):
            await search.search("public query", "q1")
    finally:
        await search.aclose()


async def test_explicit_empty():
    search = provider(lambda req: html_response(
        '<div class="result result--no-result"><div class="no-results">No results found</div></div>'
    ))
    try:
        assert await search.search("public query", "q1") == []
    finally:
        await search.aclose()


@pytest.mark.parametrize("status,error", [(202, "challenge"), (429, "rate_limited"), (302, "redirect_refused")])
async def test_refusals_do_not_retry_or_follow(status, error):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(status, headers={"location": "https://evil.example"})

    search = provider(handler)
    try:
        with pytest.raises(SearchError, match=error):
            await search.search("public query", "q1")
        assert len(seen) == 1
    finally:
        await search.aclose()


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "http://user:secret@example.com",
    "http://127.0.0.1/a", "http://[::1]/a", "http://10.0.0.1", "http://localhost/",
    "https://service.internal", "http://router", "http://2130706433/", "http://127.1/",
    "https://evil.example/l/?uddg=https%3A%2F%2Fexample.com",
])
async def test_unsafe_urls_fail_and_foreign_wrapper_remains_foreign(url):
    search = provider(lambda req: html_response(result(url)))
    try:
        if "evil.example" in url:
            assert (await search.search("public query", "q1"))[0].url.startswith("https://evil.example/")
        else:
            with pytest.raises(SearchError, match="invalid_result"):
                await search.search("public query", "q1")
    finally:
        await search.aclose()


async def test_only_trusted_ddg_wrapper_unwrapped():
    search = provider(lambda req: html_response(result(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc%3Fx%3D1&rut=ignored"
    )))
    try:
        assert (await search.search("public query", "q1"))[0].url == "https://example.com/doc?x=1"
    finally:
        await search.aclose()


async def test_privacy_and_budget_prevent_network():
    search = provider(lambda req: pytest.fail("no network"), limits=Limits(search_requests=1))
    try:
        for query in ["api_key=private", "alice@example.com", "C:\\Users\\private.txt"]:
            with pytest.raises(QueryPrivacyError):
                await search.search(query, "q1")
        assert search.budget.counts["search_requests"] == 0
        search.budget.take("search_requests")
        with pytest.raises(BudgetExceeded, match="search_requests"):
            await search.search("public query", "q1")
    finally:
        await search.aclose()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *, stall=False):
        self.closed = False
        self.stall = stall

    async def __aiter__(self):
        yield b"x" * 700
        if self.stall:
            await asyncio.sleep(60)
        yield b"x" * 700

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("streaming", [False, True])
async def test_response_byte_limit(streaming):
    stream = Chunks()
    search = provider(lambda req: (
        httpx.Response(200, stream=stream, headers={"content-type": "text/html"})
        if streaming else html_response("x" * 1400)
    ), response_bytes=1024)
    try:
        with pytest.raises(SearchError, match="response_size_limit"):
            await search.search("public query", "q1")
        assert search.budget.counts["search_requests"] == 1
        if streaming:
            assert stream.closed
    finally:
        await search.aclose()


async def test_timeout_includes_stream_body_and_request_budget():
    stream = Chunks(stall=True)
    search = provider(lambda req: httpx.Response(200, stream=stream, headers={"content-type": "text/html"}),
                      timeout_seconds=0.02, retries=0)
    try:
        with pytest.raises(SearchError, match="timeout"):
            await search.search("public query", "q1")
        assert stream.closed and search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


async def test_network_timeout_retries_and_invalid_content():
    async def stalled(req):
        await asyncio.sleep(60)

    search = provider(stalled, timeout_seconds=0.01)
    try:
        with pytest.raises(SearchError, match="timeout"):
            await search.search("public query", "q1")
        assert search.budget.counts["search_requests"] == 2
    finally:
        await search.aclose()
    search = provider(lambda req: httpx.Response(200, json={"results": []}))
    try:
        with pytest.raises(SearchError, match="invalid_content_type"):
            await search.search("public query", "q1")
    finally:
        await search.aclose()


async def test_retry_counts_and_budget():
    seen = []

    def handler(req):
        seen.append(req)
        return html_response(result()) if len(seen) == 2 else httpx.Response(503)

    search = provider(handler)
    try:
        assert await search.search("public query", "q1")
        assert len(seen) == search.budget.counts["search_requests"] == 2
    finally:
        await search.aclose()
    search = provider(lambda req: httpx.Response(503), limits=Limits(search_requests=1))
    try:
        with pytest.raises(BudgetExceeded):
            await search.search("public query", "q1")
        assert search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


async def test_cancellation_propagates_and_closes_stream():
    stream = Chunks(stall=True)
    search = provider(lambda req: httpx.Response(200, stream=stream, headers={"content-type": "text/html"}))
    task = asyncio.create_task(search.search("public query", "q1"))
    try:
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and stream.closed
        assert search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


async def test_doctor_probes_network_and_reports_html(monkeypatch):
    from ephy_worker import diagnostics

    monkeypatch.setattr(diagnostics, "create_search_provider", lambda config, budget:
                        DuckDuckGoHTMLProvider(config, budget, transport=httpx.MockTransport(
                            lambda req: html_response(result()))))
    report = await diagnostics.doctor(WorkerConfig(search=SearchConfig(provider="duckduckgo_html")))
    assert report["ok"] and report["search"]["ok"]
    assert report["search"]["json_api"] is False
    assert report["search"]["network_probe"] is False  # health capability itself is local
    assert report["search"]["probe_result_count"] == 1
    assert report["metrics"]["requests"]["search_requests"] == 1
