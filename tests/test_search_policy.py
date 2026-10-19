from __future__ import annotations

import asyncio
import gc
import time
import weakref
from email.utils import formatdate
from itertools import pairwise
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import SearchConfig, SearchRequestPolicy
from ephy_worker.schema import Limits
from ephy_worker.search import QueryPrivacyError, SearchError, create_search_provider, retry_after_seconds
from ephy_worker.search_policy import PolicySearchProvider


def response(content="evidence"):
    return httpx.Response(200, json={"results": [{"url": "https://example.com/doc", "title": "Reference",
                                                "content": content}]})


def provider(handler, *, limits=None, **config):
    policy = {"min_interval_seconds": 0, **config.pop("request_policy", {})}
    return create_search_provider(SearchConfig(provider="tavily_keyless", request_policy=policy, **config),
                                  Budget(limits or Limits()), transport=httpx.MockTransport(handler))


@pytest.fixture
def policy_clock(monkeypatch):
    import ephy_worker.search_policy as policy

    class Clock:
        now = 0.0

        def __init__(self):
            self.sleeps = []
            self.wake_fractions = []

        def monotonic(self):
            return self.now

        async def sleep(self, seconds):
            self.sleeps.append(seconds)
            fraction = self.wake_fractions.pop(0) if self.wake_fractions else 1
            self.now += seconds * fraction
            await asyncio.sleep(0)

    clock = Clock()
    # Only the policy's references change；Budget，HTTP and event-loop clocks stay real．
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(policy, "asyncio", SimpleNamespace(
        sleep=clock.sleep, timeout=asyncio.timeout, get_running_loop=asyncio.get_running_loop,
        Lock=asyncio.Lock,
    ))
    return clock


async def test_policy_config_bounds_and_opt_in():
    for field, value in [("cache_max_entries", 0), ("cache_max_bytes", 0), ("min_interval_seconds", -1),
                         ("cooldown_seconds", 0), ("cache_ttl_seconds", float("nan"))]:
        with pytest.raises(ValidationError):
            SearchRequestPolicy(**{field: value})
    plain = create_search_provider(SearchConfig(provider="tavily_keyless"), Budget(Limits()))
    assert not isinstance(plain, PolicySearchProvider)
    await plain.aclose()


async def test_cache_shared_across_jobs_copies_provenance_rebinds_id_and_validates_privacy():
    seen = []
    first = provider(lambda req: seen.append(req) or response())
    second = provider(lambda req: pytest.fail("cache hit must not use network"))
    try:
        original = await first.search("public query", "original")
        discovered = original[0].discovered_at
        original[0].title = "mutated"
        await first.aclose()
        cached = await second.search("public query", "new")
        assert cached[0].title == "Reference" and cached[0].query_id == "new"
        assert cached[0].discovered_at == discovered and cached[0] is not original[0]
        cached[0].snippet = "mutated"
        assert (await second.search("public query", "third"))[0].snippet == "evidence"
        with pytest.raises(QueryPrivacyError):
            await second.search("api_key=private", "new")
        assert len(seen) == 1 and second.budget.counts["search_requests"] == 0
        assert second.metadata["request_policy"]["cache_hits"] == 2
        assert second.last_diagnostic["code"] == "search_cache_hit"
        second.budget.counts["search_requests"] = second.budget.limits.search_requests
        assert (await second.search("public query", "budget-full-hit"))[0].query_id == "budget-full-hit"
        second.budget.started -= 1000
        with pytest.raises(BudgetExceeded):
            await second.search("public query", "dead")
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("expired_at", [0.02, 0.03])
async def test_ttl_success_only_empty_and_disable(policy_clock, expired_at):
    seen = []
    search = provider(lambda req: seen.append(req) or httpx.Response(200, json={"results": []}),
                      request_policy={"cache_ttl_seconds": 0.02})
    try:
        assert await search.search("public query", "q") == []
        assert await search.search("public query", "q") == []
        assert len(seen) == 1
        policy_clock.now = 0.019
        assert await search.search("public query", "before-expiry") == []
        assert len(seen) == 1
        policy_clock.now = expired_at
        await search.search("public query", "q")
        assert len(seen) == 2
    finally:
        await search.aclose()
    failed = provider(lambda req: seen.append(req) or httpx.Response(503),
                      request_policy={"cache_ttl_seconds": 0})
    try:
        for _ in range(2):
            with pytest.raises(SearchError):
                await failed.search("different query", "q")
        assert len(seen) == 4
    finally:
        await failed.aclose()


async def test_concurrent_duplicate_coalesces_and_distinct_queries_are_paced(policy_clock):
    started = []

    async def handler(req):
        started.append(policy_clock.monotonic())
        await asyncio.sleep(0)
        return response()

    first = provider(handler, request_policy={"min_interval_seconds": 0.04})
    second = provider(handler, request_policy={"min_interval_seconds": 0.04})
    try:
        a, b = await asyncio.gather(first.search("same query", "a"), second.search("same query", "b"))
        assert len(started) == 1 and a[0].query_id == "a" and b[0].query_id == "b"
        await asyncio.gather(first.search("next query", "c"), second.search("last query", "d"))
        assert len(started) == 3
        assert all(b - a >= first.policy.min_interval_seconds for a, b in pairwise(started))
    finally:
        await first.aclose()
        await second.aclose()


async def test_pacing_rechecks_after_early_wakeup_before_http(policy_clock):
    started = []
    search = provider(lambda req: started.append(policy_clock.monotonic()) or response(),
                      request_policy={"min_interval_seconds": 0.04, "cache_ttl_seconds": 0})
    try:
        await search.search("first query", "a")
        policy_clock.wake_fractions = [0, 0.5]
        await search.search("second query", "b")
        assert started == pytest.approx([0, 0.04])
        assert started[1] - started[0] >= search.policy.min_interval_seconds
        assert policy_clock.sleeps == pytest.approx([0.04, 0.04, 0.02])
        assert search.budget.counts["search_requests"] == 2
        assert not search._state.lock.locked()
    finally:
        await search.aclose()


async def test_semantic_settings_isolate_cache_but_not_cooldown_or_policy():
    seen = []
    first = provider(lambda req: seen.append(req) or response(), max_results=1)
    second = provider(lambda req: seen.append(req) or httpx.Response(429, headers={"retry-after": "30"}),
                      max_results=2, request_policy={"cooldown_seconds": 0.01})
    third = provider(lambda req: pytest.fail("cooldown"), max_results=3, language="en",
                     request_policy={"cooldown_seconds": 0.001, "cache_ttl_seconds": 0})
    try:
        await first.search("public query", "q")
        with pytest.raises(SearchError, match="rate_limited"):
            await second.search("public query", "q")
        await second.aclose()
        before = time.monotonic()
        with pytest.raises(SearchError, match="search_provider_cooldown") as error:
            await third.search("another query", "q")
        assert error.value.retry_after > 29 and time.monotonic() - before < 0.1
        assert third.budget.counts["search_requests"] == 0 and len(seen) == 2
        assert third.last_diagnostic["reasons"] == ["rate_limited"]
        assert third.metadata["request_policy"]["cooldown_remaining_seconds"] > 29
    finally:
        for search in (first, second, third):
            await search.aclose()


async def test_duckduckgo_html_challenge_cools_down_without_http():
    config = SearchConfig(provider="duckduckgo_html", request_policy={"min_interval_seconds": 0})
    seen = []
    search = create_search_provider(config, Budget(Limits()), transport=httpx.MockTransport(
        lambda req: seen.append(req) or httpx.Response(200, text='<form id="anomaly-form"></form>',
                                                      headers={"content-type": "text/html", "retry-after": "600"})))
    another = create_search_provider(config, Budget(Limits()), transport=httpx.MockTransport(
        lambda req: pytest.fail("cooled down")))
    try:
        with pytest.raises(SearchError, match="duckduckgo_challenge"):
            await search.search("public query", "q")
        with pytest.raises(SearchError, match="search_provider_cooldown") as error:
            await another.search("different query", "q")
        assert error.value.retry_after > 599
        assert len(seen) == 1 and another.budget.counts["search_requests"] == 0
    finally:
        await search.aclose()
        await another.aclose()


async def test_entry_byte_caps_ttl_and_lru_eviction():
    seen = []
    search = provider(lambda req: seen.append(req) or response(),
                      request_policy={"cache_max_entries": 1, "cache_max_bytes": 1024})
    try:
        for query in ["first query", "second query", "first query"]:
            await search.search(query, "q")
        assert len(seen) == 3 and len(search._state.cache) == 1
        assert search._state.cache_bytes <= 1024
        assert "first query" not in str(search._state.cache.keys())
    finally:
        await search.aclose()
    large = provider(lambda req: seen.append(req) or response("文" * 1000),
                     request_policy={"cache_max_entries": 128, "cache_max_bytes": 1024})
    try:
        await large.search("large query", "q")
        await large.search("large query", "q")
        assert len(seen) == 5 and large._state.cache_bytes <= 1024
    finally:
        await large.aclose()


async def test_byte_limit_evicts_small_entries_independently_of_entry_limit():
    seen = []
    search = provider(lambda req: seen.append(req) or response("evidence" * 20),
                      request_policy={"cache_max_entries": 128, "cache_max_bytes": 1024})
    try:
        for query in ["one query", "two query", "three query", "four query", "one query"]:
            await search.search(query, "q")
        assert len(seen) == 5 and len(search._state.cache) < 4
        assert search._state.cache_bytes == sum(len(entry.data) for entry in search._state.cache.values())
        assert search._state.cache_bytes <= 1024
    finally:
        await search.aclose()


async def test_cooldown_expires_and_rejects_without_sleep_or_count():
    seen = []
    search = provider(lambda req: seen.append(req) or (httpx.Response(429) if len(seen) == 1 else response()),
                      request_policy={"cooldown_seconds": 0.02})
    try:
        with pytest.raises(SearchError, match="rate_limited"):
            await search.search("public query", "q")
        with pytest.raises(SearchError, match="search_provider_cooldown"):
            await search.search("public query", "q")
        assert search.budget.counts["search_requests"] == 1
        await asyncio.sleep(0.03)
        assert await search.search("public query", "q")
        assert search.budget.counts["search_requests"] == 2
    finally:
        await search.aclose()


async def test_cancel_during_pacing_releases_lock_without_http():
    seen = []
    search = provider(lambda req: seen.append(req) or response(),
                      request_policy={"min_interval_seconds": 1})
    try:
        await search.search("first query", "q")
        task = asyncio.create_task(search.search("second query", "q"))
        await asyncio.sleep(0.01)
        assert search._state.lock.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not search._state.lock.locked() and len(seen) == 1
    finally:
        await search.aclose()


async def test_searxng_429_and_engine_captcha_do_not_retry_under_policy():
    for http_response in [httpx.Response(429, headers={"retry-after": "600"}),
                          httpx.Response(200, json={"unresponsive_engines": [["duckduckgo", "CAPTCHA"]]},
                                         headers={"retry-after": "600"})]:
        search = create_search_provider(SearchConfig(base_url="http://127.0.0.1:8888", request_policy={}),
                                        Budget(Limits()), transport=httpx.MockTransport(
                                            lambda req, response=http_response: response))
        try:
            # Each iteration isolates its backend while exercising the same wrapper contract．
            search._shared().cooldown_until = 0
            with pytest.raises(SearchError):
                await search.search("public query", "q")
            with pytest.raises(SearchError, match="search_provider_cooldown") as error:
                await search.search("different query", "q")
            assert error.value.retry_after > 599 and search.budget.counts["search_requests"] == 1
        finally:
            await search.aclose()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_waiting_lock_and_pacing_cancel_or_respect_deadline(mode):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(req):
        entered.set()
        await release.wait()
        return response()

    first = provider(handler)
    second = provider(lambda req: pytest.fail("deadline must prevent HTTP"),
                      limits=Limits(job_seconds=0.03) if mode == "deadline" else None)
    task = asyncio.create_task(first.search("first query", "q"))
    await entered.wait()
    waiter = asyncio.create_task(second.search("second query", "q"))
    try:
        if mode == "cancel":
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        else:
            with pytest.raises(BudgetExceeded, match="job_timeout"):
                await waiter
        assert second.budget.counts["search_requests"] == 0
    finally:
        release.set()
        await task
        await first.aclose()
        await second.aclose()
    paced = provider(lambda req: pytest.fail("pacing deadline"), limits=Limits(job_seconds=0.02),
                     request_policy={"min_interval_seconds": 1})
    try:
        with pytest.raises(BudgetExceeded):
            await paced.search("paced query", "q")
        assert paced.budget.counts["search_requests"] == 0 and not paced._state.lock.locked()
    finally:
        await paced.aclose()


async def test_endpoint_and_credentials_partition_backend_state(monkeypatch):
    searches = []
    for url in ["http://127.0.0.1:8888", "http://127.0.0.1:9999"]:
        searches.append(create_search_provider(SearchConfig(base_url=url, request_policy={}), Budget(Limits())))
    for key in ["tvly-credential-one", "tvly-credential-two"]:
        monkeypatch.setenv("TAVILY_API_KEY", key)
        searches.append(create_search_provider(SearchConfig(provider="tavily", request_policy={}), Budget(Limits())))
    try:
        assert len({id(search._shared()) for search in searches}) == 4
        assert all("credential" not in search._backend for search in searches)
    finally:
        for search in searches:
            await search.aclose()


def test_event_loops_have_separate_state():
    states = []

    async def run():
        search = provider(lambda req: response())
        try:
            await search.search("public query", "q")
            states.append(search._state)
            assert search.budget.counts["search_requests"] == 1
        finally:
            await search.aclose()

    asyncio.run(run())
    asyncio.run(run())
    assert states[0] is not states[1]


def test_contended_locks_do_not_retain_closed_event_loop():
    loop_ref = None

    async def run():
        nonlocal loop_ref
        loop_ref = weakref.ref(asyncio.get_running_loop())
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(req):
            entered.set()
            await release.wait()
            return response()

        first, second = provider(handler), provider(handler)
        task = asyncio.create_task(first.search("first query", "q"))
        await entered.wait()
        waiter = asyncio.create_task(second.search("second query", "q"))
        await asyncio.sleep(0)
        assert first._state.lock._loop is asyncio.get_running_loop()
        release.set()
        await asyncio.gather(task, waiter)
        await first.aclose()
        await second.aclose()

    asyncio.run(run())
    gc.collect()
    assert loop_ref() is None


async def test_registry_is_bounded_and_cannot_evict_active_cooldown(monkeypatch):
    import ephy_worker.search_policy as policy

    monkeypatch.setattr(policy, "_MAX_BACKENDS", 1)
    first = provider(lambda req: httpx.Response(429))
    second = create_search_provider(SearchConfig(base_url="http://127.0.0.1:8888", request_policy={}),
                                    Budget(Limits()))
    try:
        with pytest.raises(SearchError):
            await first.search("public query", "q")
        await first.aclose()
        with pytest.raises(SearchError, match="backend_limit"):
            await second.search("public query", "q")
    finally:
        await first.aclose()
        await second.aclose()


def test_retry_after_numeric_http_date_and_invalid():
    assert retry_after_seconds("15") == 15
    assert 20 < retry_after_seconds(formatdate(time.time() + 30, usegmt=True)) <= 30
    assert retry_after_seconds("-5") == 0
    for value in [None, "", "junk", "NaN", "inf"]:
        assert retry_after_seconds(value) is None
