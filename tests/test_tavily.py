"""Offline Tavily contract，free allowance，request accounting and workflow checks．"""

import asyncio
import json
import subprocess
import sys

import httpx
import pytest
import yaml
from pydantic import ValidationError

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import ConfigurationError, SearchConfig, WorkerConfig
from ephy_worker.schema import Limits
from ephy_worker.search import QueryPrivacyError, SearchError, create_search_provider


def allowance(**overrides):
    return {
        "account": {
            "current_plan": "Researcher",
            "plan_usage": 0,
            "plan_limit": 1000,
            "paygo_usage": 0,
            "paygo_limit": 0,
            **overrides,
        }
    }


def result(**overrides):
    return {
        "results": [
            {
                "url": "https://example.org/spec?utm_source=test&v=2",
                "title": "Spec",
                "content": "snippet only",
            }
        ],
        "usage": {"credits": 1},
        **overrides,
    }


@pytest.fixture
def key(monkeypatch):
    value = "tvly-fixture-not-a-real-key"
    monkeypatch.setenv("TAVILY_API_KEY", value)
    return value


def provider(handler, **overrides):
    budget = Budget(Limits())
    return create_search_provider(
        SearchConfig(provider="tavily", **overrides), budget, transport=httpx.MockTransport(handler)
    )


async def test_tavily_request_contract_and_provenance(key):
    seen = []

    def handle(req):
        seen.append(req)
        assert str(req.url).startswith("https://api.tavily.com/")
        assert req.headers["Authorization"] == "Bearer " + key
        if req.url.path == "/usage":
            assert req.method == "GET"
            return httpx.Response(200, json=allowance())
        assert req.url.path == "/search" and req.method == "POST"
        body = json.loads(req.content)
        assert body == {
            "query": "公開資料 仕様",
            "search_depth": "basic",
            "topic": "general",
            "max_results": 8,
            "auto_parameters": False,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
        }
        assert key not in req.content.decode()
        return httpx.Response(200, json=result(raw_content="Not evidence"))

    p = provider(handle)
    try:
        found = await p.search("公開資料 仕様", "Q1")
        assert found[0].url == "https://example.org/spec?v=2"
        assert found[0].engine == "tavily" and found[0].query_id == "Q1"
        assert not found[0].supplied and found[0].snippet == "snippet only"
        assert p.budget.counts["search_requests"] == 2
        assert p.metadata["credits_reported"] == 1
        assert p.metadata["remaining_credits_conservative"] == 999
        assert key not in json.dumps([p.metadata, p.last_diagnostic])
    finally:
        await p.aclose()


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"current_plan": "Bootstrap"}, "free_plan_required"),
        ({"current_plan": None}, "free_plan_required"),
        ({"paygo_limit": 1}, "paygo_must_be_disabled"),
        ({"paygo_usage": 1}, "paygo_must_be_disabled"),
        ({"plan_usage": 1000}, "free_credits_exhausted"),
        ({"plan_limit": 1500}, "free_allowance_unverified"),
        ({"plan_usage": None}, "free_allowance_unverified"),
        ({"plan_usage": "0"}, "free_allowance_unverified"),
        ({"paygo_limit": False}, "free_allowance_unverified"),
        ({"plan_limit": -1}, "free_allowance_unverified"),
    ],
)
async def test_unknown_paid_or_exhausted_allowance_never_searches(key, changes, error):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=allowance(**changes))

    p = provider(handle)
    try:
        for query in ["public source", "another query"]:
            with pytest.raises(SearchError, match=error):
                await p.search(query, "Q1")
        assert seen == ["/usage"]
        assert p.metadata["search_attempts"] == 0
    finally:
        await p.aclose()


async def test_stale_usage_does_not_restore_reserved_last_credit(key):
    paths = []

    def handle(req):
        paths.append(req.url.path)
        return httpx.Response(200, json=allowance(plan_usage=999) if req.url.path == "/usage" else result())

    p = provider(handle)
    try:
        await p.search("public source", "Q1")
        with pytest.raises(SearchError, match="free_credits_exhausted"):
            await p.search("another source", "Q2")
        assert paths == ["/usage", "/search", "/usage"]
        assert p.metadata["credits_reserved"] == 1
    finally:
        await p.aclose()


async def test_local_credit_cap_and_global_request_cap(key):
    def handle(req):
        return httpx.Response(200, json=allowance() if req.url.path == "/usage" else result())

    p = provider(handle, max_credits_per_job=1)
    try:
        await p.search("public source", "Q1")
        with pytest.raises(BudgetExceeded, match="tavily_credits_per_job"):
            await p.search("another source", "Q2")
        assert p.budget.counts["search_requests"] == 2
    finally:
        await p.aclose()
    p = provider(handle)
    p.budget.limits.search_requests = 1
    try:
        with pytest.raises(BudgetExceeded, match="search_requests"):
            await p.search("public source", "Q1")
        assert p.metadata["credits_reserved"] == 0
    finally:
        await p.aclose()


@pytest.mark.parametrize(
    "status,error",
    [
        (401, "authentication_failed"),
        (403, "access_denied"),
        (429, "rate_limited"),
        (432, "quota_exhausted"),
        (433, "paygo_limit"),
        (503, "http_error"),
        (302, "http_error"),
    ],
)
async def test_errors_are_redacted_not_retried_or_followed(key, status, error):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        if req.url.path == "/usage":
            return httpx.Response(200, json=allowance())
        return httpx.Response(status, text=key, headers={"location": "https://elsewhere.example/secret"})

    p = provider(handle)
    try:
        for _ in range(2):
            with pytest.raises(SearchError, match=error) as caught:
                await p.search("public source", "Q1")
            assert key not in str(caught.value)
        assert seen == ["/usage", "/search"]
        assert p.metadata["usage_unknown_requests"] == 1
        assert p.metadata["credits_reported"] is None
    finally:
        await p.aclose()


@pytest.mark.parametrize(
    "payload,error",
    [
        ({}, "missing_results"),
        ({"results": None}, "missing_results"),
        ({"results": ["wrong"]}, "invalid_result"),
        ({"results": [{"url": 1}]}, "invalid_result"),
        ({"results": [{"url": "https://example.org", "title": None}]}, "invalid_result"),
        ({"detail": "upstream secret"}, "invalid_response"),
    ],
)
async def test_malformed_success_is_not_empty_search(key, payload, error):
    p = provider(lambda req: httpx.Response(200, json=allowance() if req.url.path == "/usage" else payload))
    try:
        with pytest.raises(SearchError, match=error):
            await p.search("public source", "Q1")
    finally:
        await p.aclose()


async def test_empty_results_and_missing_usage_remain_distinct(key):
    p = provider(
        lambda req: httpx.Response(200, json=allowance() if req.url.path == "/usage" else {"results": []})
    )
    try:
        assert await p.search("public source", "Q1") == []
        assert p.metadata["credits_reported"] is None
        assert p.last_diagnostic["credits_reported"] is None
        assert p.metadata["usage_unknown_requests"] == 1
    finally:
        await p.aclose()


async def test_timeout_cancel_and_size_bound(key):
    started = asyncio.Event()

    async def handle(req):
        if req.url.path == "/usage":
            return httpx.Response(200, json=allowance())
        started.set()
        await asyncio.Future()

    p = provider(handle, timeout_seconds=0.01)
    try:
        with pytest.raises(SearchError, match="timeout"):
            await p.search("public source", "Q1")
        assert p.metadata["credits_reserved"] == 1
    finally:
        await p.aclose()
    started.clear()
    p = provider(handle)
    try:
        task = asyncio.create_task(p.search("public source", "Q1"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert p.metadata["usage_unknown_requests"] == 1
    finally:
        await p.aclose()
    p = provider(lambda req: httpx.Response(200, content=b"x" * 1025), response_bytes=1024)
    try:
        with pytest.raises(SearchError, match="response_size_limit"):
            await p.search("public source", "Q1")
    finally:
        await p.aclose()


def test_missing_key_and_host_restrictions(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="TAVILY_API_KEY"):
        provider(lambda req: pytest.fail("must not connect"))
    for url in [
        "http://api.tavily.com",
        "https://api.tavily.com.evil.example",
        "http://127.0.0.1:8888",
        "https://api.tavily.com/path",
    ]:
        with pytest.raises(ConfigurationError, match="credentials may only"):
            SearchConfig(provider="tavily", base_url=url).endpoint()
    for overrides in [
        {"retries": 1},
        {"max_credits_per_job": 11},
        {"api_key_env": "bad name"},
        {"engine": "duckduckgo"},
    ]:
        with pytest.raises(ValidationError):
            SearchConfig(provider="tavily", **overrides)


async def test_key_in_query_is_rejected_before_usage_request(key):
    p = provider(lambda req: pytest.fail("private query must not connect"))
    try:
        with pytest.raises(QueryPrivacyError):
            await p.search(key, "Q1")
        assert p.budget.counts["search_requests"] == 0
    finally:
        await p.aclose()


def test_real_cli_missing_key_creates_no_job(monkeypatch, tmp_path):
    from test_cli import fixture_config

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    path = fixture_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["search"] = {"provider": "tavily"}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ephy_worker",
            "research",
            "--config",
            str(path),
            "--profile",
            "test",
            "--question",
            "public source",
            "--output-dir",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 1
    assert "TAVILY_API_KEY" in proc.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "credit_cap,additional,expected_state", [(10, False, "completed"), (3, True, "partial")]
)
async def test_full_workflow_uses_tavily_candidates_not_snippet_evidence(
    key, monkeypatch, tmp_path, credit_cap, additional, expected_state
):
    from test_research import FakeFetcher, FakeModel

    from ephy_worker.research import ResearchExecutor
    from ephy_worker.store import JobStore

    def handle(req):
        if req.url.path == "/usage":
            return httpx.Response(200, json=allowance())
        return httpx.Response(
            200,
            json=result(
                results=[
                    {
                        "url": f"https://example.org/{i}",
                        "title": "Version 2 spec",
                        "content": "INCORRECT snippet",
                    }
                    for i in range(5)
                ]
            ),
        )

    monkeypatch.setattr(
        "ephy_worker.search.create_search_provider",
        lambda c, b: create_search_provider(c, b, transport=httpx.MockTransport(handle)),
    )
    config = WorkerConfig(
        search=SearchConfig(provider="tavily", max_credits_per_job=credit_cap),
        model_profiles={
            "test": {
                "family": "qwen",
                "base_url": "http://127.0.0.1:9/v1",
                "model_id": "fixture",
                "output_mode": "prompted_json",
            }
        },
    )
    store = JobStore(tmp_path)
    worker = ResearchExecutor(
        config, "test", store, model=FakeModel(additional=additional), fetcher=FakeFetcher()
    )
    report = await worker.run("version 2 の制約")
    assert report.state == expected_state
    if additional:
        assert report.stop_reason == "tavily_credits_per_job"
    assert len(report.sources) == 5 and report.evidence[0].page == 8
    assert all(c.engine == "tavily" and not c.supplied for c in report.candidates)
    assert "INCORRECT" not in report.evidence[0].quote
    assert report.metrics["search"]["credits_reported"] == 3
    assert report.metrics["requests"]["search_requests"] == 6
    assert key not in (store.directory / "report.json").read_text()


async def test_unexpected_usage_halts_and_unknown_usage_is_not_echoed(key):
    payloads = iter(
        [result(), result(usage={"credits": "untrusted upstream string"}), result(usage={"credits": 2})]
    )
    p = provider(
        lambda req: httpx.Response(200, json=allowance() if req.url.path == "/usage" else next(payloads))
    )
    try:
        await p.search("first public source", "Q1")
        await p.search("second public source", "Q2")
        assert p.last_diagnostic["credits_reported"] is None
        assert p.metadata["usage_unknown_requests"] == 1
        assert "untrusted" not in json.dumps(p.metadata)
        with pytest.raises(SearchError, match="unexpected_credit_usage"):
            await p.search("third public source", "Q3")
        with pytest.raises(SearchError, match="unexpected_credit_usage"):
            await p.search("fourth public source", "Q4")
        assert p.metadata["credits_reported"] == 3
        assert p.budget.counts["search_requests"] == 6
    finally:
        await p.aclose()


async def test_concurrent_requests_cannot_pass_single_credit_limit(key):
    async def handle(req):
        await asyncio.sleep(0)
        return httpx.Response(200, json=allowance() if req.url.path == "/usage" else result())

    p = provider(handle, max_credits_per_job=1)
    try:
        outcomes = await asyncio.gather(
            p.search("first public source", "Q1"),
            p.search("second public source", "Q2"),
            return_exceptions=True,
        )
        assert len([r for r in outcomes if isinstance(r, list)]) == 1
        assert len([r for r in outcomes if isinstance(r, BudgetExceeded)]) == 1
        assert p.budget.counts["search_requests"] == 2
    finally:
        await p.aclose()


async def test_doctor_selects_tavily_and_reports_usage(key, monkeypatch, tmp_path):
    from ephy_worker import diagnostics

    def handle(req):
        return httpx.Response(200, json=allowance() if req.url.path == "/usage" else result())

    class ProbeModel:
        def __init__(self, *args):
            self.metadata = {"model_id": "fixture"}

        async def available(self):
            return True

        async def run(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(
        diagnostics,
        "create_search_provider",
        lambda c, b: create_search_provider(c, b, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(diagnostics, "ModelRunner", ProbeModel)
    config = WorkerConfig(
        search=SearchConfig(provider="tavily"),
        model_profiles={
            "test": {
                "family": "qwen",
                "base_url": "http://127.0.0.1:9/v1",
                "model_id": "fixture",
                "output_mode": "prompted_json",
            }
        },
    )
    outcome = await diagnostics.doctor(config, profile_id="test", output_dir=tmp_path)
    assert outcome["ok"]
    assert outcome["search"]["free_plan_verified"]
    assert outcome["metrics"]["search"]["credits_reported"] == 1
    assert outcome["metrics"]["requests"]["search_requests"] == 3
    assert key not in json.dumps(outcome)
