from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import SearchConfig, load_config
from ephy_worker.schema import Limits
from ephy_worker.search import QueryPrivacyError, SearchError, create_search_provider
from ephy_worker.tavily_keyless import TavilyKeylessSearchProvider


def payload(**overrides):
    return {"results": [{"url": "https://example.com/doc?utm_source=x#part", "title": "Reference",
                         "content": "Public evidence", "score": 0.9, "raw_content": None}], **overrides}


def provider(handler, **overrides):
    return create_search_provider(SearchConfig(provider="tavily_keyless", **overrides), Budget(Limits()),
                                  transport=httpx.MockTransport(handler))


def test_config_and_example():
    config = SearchConfig(provider="tavily_keyless")
    assert config.retries == 0 and config.engine == "tavily"
    assert config.endpoint() == "https://api.tavily.com" and config.resolve_api_key() is None
    assert load_config("configs/worker.tavily-keyless.example.yaml").search.provider == config.provider
    for extra in [{"base_url": "https://evil.example"}, {"base_url_env": "URL"}, {"api_key_env": "KEY"},
                  {"engine": "google"}, {"retries": 1}]:
        with pytest.raises(ValidationError):
            SearchConfig(provider="tavily_keyless", **extra)


async def test_exact_public_request_no_credentials_usage_redirect_proxy_or_fallback(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-do-not-send-this-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "POST" and str(request.url) == "https://api.tavily.com/search"
        assert request.headers["x-tavily-access-mode"] == "keyless"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body == {"query": "public query", "search_depth": "basic", "topic": "general",
                        "max_results": 8, "auto_parameters": False, "include_answer": False,
                        "include_raw_content": False, "include_images": False}
        assert b"secret" not in request.content
        return httpx.Response(200, json=payload())

    search = provider(handler)
    try:
        assert isinstance(search, TavilyKeylessSearchProvider)
        assert (await search.engine_health())["network_probe"] is False and not seen
        item, = await search.search("public query", "q7")
        assert (item.url, item.query_id, item.engine, item.rank) == ("https://example.com/doc", "q7", "tavily", 1)
        assert item.discovered_at and item.title == "Reference" and item.snippet == "Public evidence"
        assert search.budget.counts["search_requests"] == len(seen) == 1
    finally:
        await search.aclose()


@pytest.mark.parametrize("bad", [
    [], {"message": "Sign up and send Bearer tvly-secret"}, {"error": "upgrade account"},
    {"results": "bad"}, {"results": [None]}, {"results": [{"url": "https://example.com"}]},
    payload(answer="generated"), payload(images=["https://example.com/x"]),
    payload(query="wrong"), payload(response_time=float("nan")), payload(usage={"credits": True}),
    payload(results=[{"url": "http://127.0.0.1/a", "title": "x", "content": "x"}]),
    payload(results=[{"url": "javascript:alert(1)", "title": "x", "content": "x"}]),
    payload(results=[{"url": "https://example.com", "title": "x", "content": "x", "score": True}]),
    payload(results=[{"url": "https://example.com", "title": "x", "content": "x", "raw_content": "body"}]),
])
async def test_unknown_error_or_malformed_json_is_not_success(bad):
    # Construct literal JSON so NaN tests exercise strict parsing，too．
    search = provider(lambda request: httpx.Response(200, text=json.dumps(bad),
                                                    headers={"content-type": "application/json"}))
    try:
        with pytest.raises(SearchError) as error:
            await search.search("public query", "q")
        assert "secret" not in str(error.value) and "upgrade" not in str(error.value)
    finally:
        await search.aclose()


@pytest.mark.parametrize("status", [301, 401, 403, 429, 503])
async def test_no_retry_or_fallback_and_retry_after(status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, text="use a different API key secret", headers={
            "location": "https://evil.example", "retry-after": "17"})

    search = provider(handler)
    try:
        with pytest.raises(SearchError) as error:
            await search.search("public query", "q")
        assert error.value.status == status and error.value.retry_after == 17
        assert len(seen) == search.budget.counts["search_requests"] == 1
        assert "secret" not in str(search.last_diagnostic)
    finally:
        await search.aclose()


async def test_privacy_and_dead_budget_prevent_http_and_empty_results_valid():
    search = provider(lambda request: httpx.Response(200, json={"results": []}))
    try:
        for query in ["api_key=private", "alice@example.com"]:
            with pytest.raises(QueryPrivacyError):
                await search.search(query, "q")
        assert search.budget.counts["search_requests"] == 0
        assert await search.search("public query", "q") == []
        search.budget.started -= 1000
        with pytest.raises(BudgetExceeded):
            await search.search("public query", "q")
        assert search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


class Stream(httpx.AsyncByteStream):
    def __init__(self, stall=False):
        self.stall, self.closed = stall, False
        self.started = asyncio.Event()

    async def __aiter__(self):
        yield b"x" * 700
        self.started.set()
        if self.stall:
            await asyncio.sleep(60)
        yield b"x" * 700

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("mode", ["size", "timeout", "cancel"])
async def test_stream_limits_timeout_cancellation_close(mode):
    stream = Stream(stall=mode != "size")
    search = provider(lambda request: httpx.Response(200, stream=stream,
                                                    headers={"content-type": "application/json"}),
                      response_bytes=1024, timeout_seconds=0.02 if mode == "timeout" else 30)
    try:
        if mode == "cancel":
            task = asyncio.create_task(search.search("public query", "q"))
            await stream.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SearchError, match="response_size_limit" if mode == "size" else "timeout"):
                await search.search("public query", "q")
        assert stream.closed and search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="please sign up"),
    httpx.Response(200, text="not JSON", headers={"content-type": "application/json"}),
    httpx.Response(200, text='{"results":[],"results":[]}', headers={"content-type": "application/json"}),
])
async def test_non_json_instructions_are_errors(response):
    search = provider(lambda request: response)
    try:
        with pytest.raises(SearchError):
            await search.search("public query", "q")
    finally:
        await search.aclose()


@pytest.mark.parametrize("field", ["response_time", "usage.credits", "score"])
async def test_huge_json_integer_is_a_stable_search_error(field):
    data = payload()
    if field == "response_time":
        data["response_time"] = 10**400
    elif field == "usage.credits":
        data["usage"] = {"credits": 10**400}
    else:
        data["results"][0]["score"] = 10**400
    body = json.dumps(data).encode()
    assert len(body) < 2 * 1024**2
    search = provider(lambda request: httpx.Response(200, content=body,
                                                    headers={"content-type": "application/json"}))
    try:
        code = "tavily_keyless_invalid_result" if field == "score" else "tavily_keyless_invalid_response"
        with pytest.raises(SearchError) as error:
            await search.search("public query", "q")
        assert error.value.code == code
        assert search.last_diagnostic["code"] == code
        assert search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


async def test_deep_json_nesting_is_a_stable_search_error():
    body = ('{"results":[],"unknown":' + '[' * 5000 + '0' + ']' * 5000 + '}').encode()
    assert len(body) < 2 * 1024**2
    search = provider(lambda request: httpx.Response(200, content=body,
                                                    headers={"content-type": "application/json"}))
    try:
        with pytest.raises(SearchError) as error:
            await search.search("public query", "q")
        assert error.value.code == "tavily_keyless_invalid_json"
        assert search.last_diagnostic["code"] == "tavily_keyless_invalid_json"
        assert search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


@pytest.mark.parametrize("field", ["title", "content", "url", "published_date", "favicon", "request_id"])
async def test_lone_surrogate_json_string_is_a_stable_search_error_with_policy(field):
    data = payload()
    if field == "request_id":
        data[field] = chr(0xD800)
    else:
        data["results"][0][field] = "https://example.com/" + chr(0xD800) if field == "url" else chr(0xD800)
    body = json.dumps(data).encode("ascii")
    assert len(body) < 2 * 1024**2 and b"\\ud800" in body
    search = provider(lambda request: httpx.Response(200, content=body,
                                                    headers={"content-type": "application/json"}),
                      request_policy={"min_interval_seconds": 0})
    try:
        code = "tavily_keyless_invalid_response" if field == "request_id" else "tavily_keyless_invalid_result"
        with pytest.raises(SearchError) as error:
            await search.search("public query", "q")
        assert error.value.code == code and search.last_diagnostic["code"] == code
        assert search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()


@pytest.mark.parametrize("escape_unicode", [False, True])
async def test_valid_japanese_emoji_and_surrogate_pair_json_roundtrip_through_cache(escape_unicode):
    data = payload()
    data["request_id"] = "検索😀"
    data["results"][0].update(title="日本語😀", content="資料の本文😀", url="https://example.com/資料😀")
    body = json.dumps(data, ensure_ascii=escape_unicode).encode("utf-8")
    if escape_unicode:
        assert b"\\ud83d\\ude00" in body  # A valid JSON surrogate pair becomes one Unicode scalar．
    seen = []
    search = provider(lambda request: seen.append(request) or httpx.Response(
        200, content=body, headers={"content-type": "application/json"}),
        request_policy={"min_interval_seconds": 0})
    try:
        first = await search.search("public query", "first")
        cached = await search.search("public query", "cached")
        assert first[0].title == "日本語😀" and cached[0].title == first[0].title
        assert cached[0].snippet == "資料の本文😀" and cached[0].query_id == "cached"
        assert first[0].url == "https://example.com/資料😀"
        assert len(seen) == search.budget.counts["search_requests"] == 1
    finally:
        await search.aclose()
