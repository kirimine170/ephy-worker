"""The model proposes flat text；code owns the evidence boundary．"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ephy_worker import answer_routing as module
from ephy_worker.answer_routing import (
    RouteProposal,
    cache_namespace,
    evidence_guard,
    preflight,
    safe_knowledge,
)
from ephy_worker.budget import Budget
from ephy_worker.config import AnswerPolicy, ModelProfile, WorkerConfig
from ephy_worker.discord_adapter import Request
from ephy_worker.schema import Limits


def profile():
    return ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture",
                        output_mode="prompted_json", schema_retries=1)


@pytest.mark.parametrize(("question", "reason"), [
    ("DNSの最新の仕組みとは", "freshness_required"), ("DNSとは 出典付き", "sources_required"),
    ("薬の一般的な仕組みとは", "high_stakes"), ("製品の仕組みとは", "specific_unverified"),
    ("AとBの違い", "uncertain_specific"), ("DNSとは何ですか", None)])
def test_guard_overrides_model(question, reason):
    assert evidence_guard(question) == reason


@pytest.mark.parametrize("text", ["<think>secret</think>", "https://example.org", "検索で確認済みです．",
    "analysis: hidden", "api_key=abcdefghi", "../private/file"])
def test_unsafe_knowledge_rejected(text):
    assert safe_knowledge(RouteProposal(route="knowledge", kind="stable_explanation", text=text), ()) is None


def test_flat_schema_no_model_verification_fields():
    for extra in ({"draft": {"text": "nested"}}, {"externally_verified": True}, {"origin": "source_verified"}, {"confidence": "high"}):
        with pytest.raises(ValidationError):
            RouteProposal.model_validate({"route": "knowledge", "kind": "stable_explanation", "text": "説明", **extra})
    assert WorkerConfig().answer_policy is None
    assert not AnswerPolicy().enabled


@pytest.mark.asyncio
async def test_one_bounded_preflight_and_no_retry(monkeypatch):
    calls = []
    class Runner:
        def __init__(self, profile, budget): self.budget = budget
        async def run(self, output, prompt, **kwargs):
            self.budget.take("model_requests")
            calls.append(kwargs)
            assert output is RouteProposal and '"route"' in prompt
            return RouteProposal(route="knowledge", kind="stable_explanation", text="DNSは名前をIPアドレスへ対応付ける仕組みです。")
        async def aclose(self): calls.append("closed")
    monkeypatch.setattr(module, "ModelRunner", Runner)
    budget = Budget(Limits(model_requests=2))
    started = budget.started
    text, reason = await preflight("DNSとは", profile(), AnswerPolicy(), budget, ())
    assert text.endswith("．") and reason == "stable_explanation"
    assert calls[0]["schema_retries"] == 0 and calls[0]["max_output_tokens"] == 512
    assert budget.counts["model_requests"] == 1 and budget.started == started and calls[1] == "closed"


def test_namespace_all_authenticated_dimensions_and_policy():
    settings = SimpleNamespace(application_id=1, guild_id=2, allowed_channel_ids={3, 4}, allowed_user_ids={5, 6}, profile="p")
    config = WorkerConfig(model_profiles={"p": profile()}, answer_policy=AnswerPolicy(enabled=True))
    request = Request(7, 1, 2, 3, 5)
    first = cache_namespace(settings, config, request)
    assert first and cache_namespace(settings, config, None) is None
    assert first != cache_namespace(settings, config, Request(8, 1, 2, 3, 6))
    assert first != cache_namespace(settings, config, Request(8, 1, 2, 4, 5))
    changed = config.model_copy(update={"answer_policy": AnswerPolicy(enabled=True, max_rounds=3)})
    assert first != cache_namespace(settings, changed, request)
    assert cache_namespace(settings, config, Request(8, 99, 2, 3, 5)) is None


@pytest.mark.asyncio
async def test_real_model_runner_overrides_only_tighten():
    from ephy_worker.models import ModelRunner
    runner = ModelRunner(profile(), Budget(Limits()))
    try:
        for kwargs in ({"max_output_tokens": 9999}, {"timeout_seconds": 999}, {"schema_retries": 2}):
            with pytest.raises(ValueError, match="tighten"):
                await runner.run(RouteProposal, json.dumps({}), stage="fixture", **kwargs)
    finally:
        await runner.aclose()
    await asyncio.sleep(0)
