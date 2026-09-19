from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import BaseModel

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import ConfigurationError, ModelProfile, SearchConfig, WorkerConfig, load_config
from ephy_worker.models import ModelError, ModelRunner
from ephy_worker.schema import Limits
from ephy_worker.search import (
    QueryPrivacyError,
    SearchError,
    SearchProvider,
    normalize_url,
    validate_public_text,
    validate_query,
)


def profile(**overrides):
    return ModelProfile(
        family="qwen",
        base_url="http://127.0.0.1:8083/v1",
        model_id="qwen-fixture",
        output_mode=overrides.pop("output_mode", "prompted_json"),
        **overrides,
    )


class Answer(BaseModel):
    answer: int


def completion(content, *, usage=True):
    result = {
        "id": "fixture",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen-actual",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
    }
    if usage:
        result["usage"] = {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}
    return httpx.Response(200, json=result)


@pytest.mark.parametrize(
    "query",
    [
        "API key formats documentation",
        "Python 3.12 site:docs.python.org",
        "分散処理 公式文書 制限",
        "RFC 192.0.2.1 example",
        "https://example.org/Users/public/docs%20guide.pdf",
        "see https://example.org/srv/public.txt details",
    ],
)
def test_public_queries(query):
    if "192.0.2.1" in query:
        with pytest.raises(QueryPrivacyError):
            validate_query(query)
    else:
        assert validate_query(query) == query


@pytest.mark.parametrize(
    "query",
    [
        "Bearer secret12345678",
        "api_key=secret-value",
        "/Users/alice/private.txt",
        "C:\\Users\\alice\\file.txt",
        "alice@example.com",
        "service.internal api",
        "http://127.0.0.1:8080",
        "http://[::1]/",
        "!google query",
        "!! query",
        "社外秘 文書",
        "sk-" + "abcdefghijklmnopqrst",
        "```` private code",
        "http://name:secret@example.com",
        "/Volumes/PrivateDrive/customer.csv",
        "/srv/customer/private.log",
        "~/Documents/private.txt",
        "/mnt/archive/account.csv",
        "/Library/Documents/record.txt",
        "see '/arbitrary-root/sensitive/data.log'",
        "api%5Fkey%3Dhiddenvalue",
        "api%255Fkey%253Dhiddenvalue",
        "%2Fsrv%2Fcustomer%2Fprivate.log",
    ],
)
def test_private_queries_never_sent(query):
    with pytest.raises(QueryPrivacyError):
        validate_query(query)


def test_url_normalization_preserves_semantics():
    assert normalize_url("https://example.com/doc?v=2&utm_source=x#section") == "https://example.com/doc?v=2"
    assert normalize_url("https://alice:secret@example.com") is None


def test_whole_question_validation_does_not_split_sensitive_boundary():
    text = "public background " * 20 + "api_key=not-a-real-key"
    with pytest.raises(QueryPrivacyError, match="credential_assignment"):
        validate_public_text(text)
    assert validate_public_text("public question " * 100)


def test_lazy_profiles_and_secret_redaction(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_MODEL_KEY", "never-print-this-secret")
    resolved = profile(api_key_env="TEST_MODEL_KEY").resolve()
    assert "never-print" not in repr(resolved)
    assert "never-print" not in json.dumps(resolved.metadata())
    monkeypatch.delenv("MISSING_MODEL_URL", raising=False)
    bad = ModelProfile(
        family="deepseek", base_url_env="MISSING_MODEL_URL", model_id="deepseek", output_mode="prompted_json"
    )
    with pytest.raises(ConfigurationError, match="MISSING_MODEL_URL"):
        bad.resolve()
    path = tmp_path / "config.yaml"
    path.write_text(
        "model_profiles:\n  qwen:\n    api_key: never-print-this-secret\nsearch: {}", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError) as caught:
        load_config(path)
    assert "never-print" not in str(caught.value)


def test_unconfigured_public_model_is_not_implicit_fallback():
    public = ModelProfile(
        family="qwen", base_url="https://example.com/v1", model_id="qwen", output_mode="prompted_json"
    )
    with pytest.raises(ConfigurationError, match="allow_remote_api"):
        public.resolve()


async def test_search_posts_explicit_engine_and_provenance():
    def handler(request):
        assert request.method == "POST"
        data = parse_qs(request.content.decode())
        assert data["engines"] == ["duckduckgo"]
        assert data["format"] == ["json"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/?version=2&utm_source=x",
                        "title": "Source",
                        "content": "selection only",
                        "engines": ["duckduckgo"],
                    }
                ]
            },
        )

    budget = Budget(Limits())
    provider = SearchProvider(
        SearchConfig(base_url="http://127.0.0.1:8888"), budget, transport=httpx.MockTransport(handler)
    )
    try:
        candidates = await provider.search("public documentation", "q1")
        assert candidates[0].url == "https://example.com/?version=2"
        assert candidates[0].query_id == "q1"
        assert candidates[0].snippet == "selection only"
        assert budget.counts["search_requests"] == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    ("payload", "status", "error", "requests"),
    [
        ({"results": []}, 403, "forbidden", 1),
        ({"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}, 200, "unresponsive", 1),
        ({"results": []}, 429, "http_error", 2),
        ({"unexpected": []}, 200, "missing_results", 1),
        ({"results": [{"url": "https://example.com", "engine": "google"}]}, 200, "unexpected_engine", 1),
    ],
)
async def test_search_errors_are_not_zero_results(payload, status, error, requests):
    budget = Budget(Limits())
    provider = SearchProvider(
        SearchConfig(base_url="http://127.0.0.1:8888"),
        budget,
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload)),
    )
    try:
        with pytest.raises(SearchError, match=error):
            await provider.search("public query", "q1")
        assert budget.counts["search_requests"] == requests
    finally:
        await provider.aclose()


async def test_search_real_empty_and_redirect_refusal():
    budget = Budget(Limits())
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/search"})

    provider = SearchProvider(
        SearchConfig(base_url="http://127.0.0.1:8888"), budget, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SearchError):
        await provider.search("public query", "q1")
    assert len(seen) == 1
    await provider.aclose()
    provider = SearchProvider(
        SearchConfig(base_url="http://127.0.0.1:8888"),
        budget,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"results": []})),
    )
    assert await provider.search("public query", "q2") == []
    await provider.aclose()


async def test_typed_model_retry_counts_raw_requests_and_usage():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == "qwen-fixture"
        assert "tools" not in payload and "response_format" not in payload
        seen.append(payload)
        return completion("not json" if len(seen) == 1 else '{"answer": 4}')

    budget = Budget(Limits())
    runner = ModelRunner(profile(), budget, transport=httpx.MockTransport(handler))
    try:
        assert (await runner.run(Answer, "Answer 2+2", stage="fixture")).answer == 4
        assert budget.counts["model_requests"] == 2
        assert budget.input_tokens == 60 and budget.output_tokens == 20
        assert runner.metadata["actual_model_ids"] == ["qwen-actual"]
    finally:
        await runner.aclose()


async def test_model_bad_json_and_http_failures_are_finite():
    for response, expected in [(lambda: completion("broken"), 2), (lambda: httpx.Response(429), 1)]:
        budget = Budget(Limits())
        runner = ModelRunner(profile(), budget, transport=httpx.MockTransport(lambda req, fn=response: fn()))
        try:
            with pytest.raises(ModelError):
                await runner.run(Answer, "Answer 2+2", stage="fixture")
            assert budget.counts["model_requests"] == expected
        finally:
            await runner.aclose()


async def test_unknown_usage_and_shared_model_budget():
    budget = Budget(Limits(model_requests=1))
    runner = ModelRunner(
        profile(), budget, transport=httpx.MockTransport(lambda req: completion("broken", usage=False))
    )
    try:
        with pytest.raises(BudgetExceeded, match="model_requests"):
            await runner.run(Answer, "Answer 2+2", stage="fixture")
        assert budget.counts["model_requests"] == 1
        assert budget.input_tokens is None and budget.output_tokens is None
        assert runner.metadata["usage_unknown_requests"] == 1
    finally:
        await runner.aclose()


async def test_model_timeout_and_cancel_propagate():
    async def stalled(request):
        await asyncio.sleep(30)
        return completion('{"answer":4}')

    budget = Budget(Limits())
    runner = ModelRunner(profile(timeout_seconds=0.02), budget, transport=httpx.MockTransport(stalled))
    try:
        with pytest.raises(ModelError):
            await runner.run(Answer, "Answer 2+2", stage="fixture")
        assert budget.counts["model_requests"] == 1
    finally:
        await runner.aclose()
    runner = ModelRunner(profile(), Budget(Limits()), transport=httpx.MockTransport(stalled))
    task = asyncio.create_task(runner.run(Answer, "Answer 2+2", stage="fixture"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runner.aclose()


async def test_context_limit_prevents_network():
    runner = ModelRunner(
        profile(context_tokens=2048, max_output_tokens=128),
        Budget(Limits()),
        transport=httpx.MockTransport(lambda req: pytest.fail("must not send")),
    )
    try:
        with pytest.raises(ModelError, match="model_context_limit"):
            await runner.run(Answer, "large " * 3000, stage="fixture")
    finally:
        await runner.aclose()


async def test_schema_retry_context_is_rechecked_before_counting_or_sending():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        # Invalid but long model output is carried into the next repair request．
        return completion("x" * 3000)

    budget = Budget(Limits())
    runner = ModelRunner(
        profile(context_tokens=4096, max_output_tokens=1024), budget, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ModelError, match="model_context_limit") as caught:
            await runner.run(Answer, "public " * 650, stage="fixture")
        assert caught.value.code.startswith("model_context_limit:")
        assert len(seen) == 1
        assert budget.counts["model_requests"] == 1
    finally:
        await runner.aclose()


@pytest.mark.parametrize("mode", ["tool", "native_json"])
async def test_explicit_output_modes_never_automatically_fallback(mode):
    def handler(request):
        payload = json.loads(request.content)
        if mode == "native_json":
            assert payload["response_format"]["type"] == "json_schema"
            assert "tools" not in payload
            return completion('{"answer":4}')
        tool = payload["tools"][0]["function"]
        assert "response_format" not in payload
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen-actual",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-fixture",
                                    "type": "function",
                                    "function": {"name": tool["name"], "arguments": '{"answer":4}'},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    runner = ModelRunner(profile(output_mode=mode), Budget(Limits()), transport=httpx.MockTransport(handler))
    try:
        assert (await runner.run(Answer, "Answer 2+2", stage="mode_probe")).answer == 4
    finally:
        await runner.aclose()


async def test_search_timeout_retry_budget_and_cancel():
    async def stalled(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={"results": []})

    budget = Budget(Limits(search_requests=1))
    provider = SearchProvider(
        SearchConfig(base_url="http://127.0.0.1:8888", timeout_seconds=0.01),
        budget,
        transport=httpx.MockTransport(stalled),
    )
    try:
        with pytest.raises(BudgetExceeded, match="search_requests"):
            await provider.search("public query", "q1")
        assert budget.counts["search_requests"] == 1
    finally:
        await provider.aclose()
    provider = SearchProvider(
        SearchConfig(base_url="http://127.0.0.1:8888"),
        Budget(Limits()),
        transport=httpx.MockTransport(stalled),
    )
    task = asyncio.create_task(provider.search("public query", "q1"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.aclose()


@pytest.mark.parametrize("nested_error", [False, True])
async def test_doctor_checks_real_nested_workflow_schema(monkeypatch, nested_error):
    from ephy_worker import diagnostics

    seen_schemas = []

    def model_handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "qwen-fixture", "object": "model", "created": 1, "owned_by": "fixture"}],
                },
            )
        payload = json.loads(request.content)
        tool = payload["tools"][0]["function"]
        schema = tool["parameters"]
        is_workflow = "claims" in schema["properties"]
        seen_schemas.append("Extraction" if is_workflow else "basic")
        if is_workflow and nested_error:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "type": "invalid_request_error",
                        "message": "Failed to initialize samplers: failed to parse grammar sensitive-upstream-detail",
                    }
                },
            )
        arguments = '{"claims":[],"gaps":["No sources"]}' if is_workflow else '{"answer":"ok","value":4}'
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen-actual",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-fixture",
                                    "type": "function",
                                    "function": {"name": tool["name"], "arguments": arguments},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    def search_handler(request):
        if request.url.path == "/config":
            return httpx.Response(200, json={"engines": [{"name": "duckduckgo", "enabled": True}]})
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(
        diagnostics,
        "ModelRunner",
        lambda profile, budget: ModelRunner(profile, budget, transport=httpx.MockTransport(model_handler)),
    )
    monkeypatch.setattr(
        diagnostics,
        "create_search_provider",
        lambda config, budget: SearchProvider(config, budget, transport=httpx.MockTransport(search_handler)),
    )
    config = WorkerConfig(
        model_profiles={"qwen": profile(output_mode="tool")},
        search=SearchConfig(base_url="http://127.0.0.1:8888"),
    )
    result = await diagnostics.doctor(config, "qwen")
    probed = result["profiles"]["qwen"]
    assert probed["basic_output_probe"] is True
    assert probed["workflow_schema_probe"] is not nested_error
    assert probed["typed_output_probe"] is not nested_error
    assert probed["ok"] is not nested_error
    assert result["ok"] is not nested_error
    assert seen_schemas == ["basic", "Extraction"]
    assert result["metrics"]["requests"]["model_requests"] == 3
    if nested_error:
        assert probed["error"] == "model_output_grammar_unsupported"
        assert "sensitive-upstream-detail" not in json.dumps(result)
