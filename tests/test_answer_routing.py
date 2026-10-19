"""The model proposes flat text；code owns the evidence boundary．"""

import asyncio
import json
from types import SimpleNamespace

import httpx
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
from ephy_worker.models import ModelError, ModelRunner
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
    assert calls[0]["schema_retries"] == 0 and calls[0]["max_output_tokens"] == 256
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


@pytest.mark.parametrize(("profile_seconds", "policy_seconds", "job_seconds", "fixed_bound"), [
    (5, 12, 30, 5), (5, 12, 2, None), (5, 0.5, 30, 0.5)])
@pytest.mark.asyncio
async def test_real_preflight_tightens_profile_policy_and_original_budget(
    monkeypatch, profile_seconds, policy_seconds, job_seconds, fixed_bound,
):
    model_profile = profile().model_copy(update={"timeout_seconds": profile_seconds})
    budget = Budget(Limits(model_requests=2, job_seconds=job_seconds))
    budget.take("model_requests")
    started, remaining = budget.started, budget.remaining_seconds
    runners, physical_requests, bounds = [], [], []

    async def handler(request):
        physical_requests.append(request)
        bounds.append(runners[0].transport.call_timeout_seconds)
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["max_completion_tokens"] == 256
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "route": "knowledge", "kind": "stable_explanation",
                    "text": "DNSは名前をIPアドレスへ対応付ける仕組みです。",
                }, ensure_ascii=False),
            }}],
        })

    def make_runner(active_profile, original_budget):
        runner = ModelRunner(active_profile, original_budget, transport=httpx.MockTransport(handler))
        runners.append(runner)
        return runner

    monkeypatch.setattr(module, "ModelRunner", make_runner)
    text, reason = await preflight(
        "DNSとは", model_profile, AnswerPolicy(preflight_seconds=policy_seconds), budget, (),
    )
    assert text == "DNSは名前をIPアドレスへ対応付ける仕組みです．"
    assert reason == "stable_explanation"
    assert len(runners) == len(physical_requests) == len(bounds) == 1
    assert 0 < bounds[0] <= min(profile_seconds, policy_seconds, remaining)
    if fixed_bound is not None:
        assert bounds[0] == fixed_bound
    else:
        assert bounds[0] < profile_seconds
    assert runners[0].budget is budget and budget.started == started
    assert budget.counts["model_requests"] == 2
    assert runners[0].transport.call_timeout_seconds is None
    assert runners[0].client.is_closed()


@pytest.mark.parametrize("text", [
    "DNSはIPアドレスを検索する仕組みです。",
    "DNSは名前からIPアドレスを調べる仕組みです。",
    "辞書は語句を検索して意味を調べるための資料です。",
])
def test_stable_lookup_descriptions_are_safe_knowledge(text):
    result = safe_knowledge(RouteProposal(route="knowledge", kind="stable_explanation", text=text), ())
    assert result == text.replace("。", "．")


@pytest.mark.parametrize("text", [
    "Webで検索しました。DNSは名前を解決します。",
    "検索したところ，DNSは名前を解決します。",
    "調べたところ，DNSは名前を解決します。",
    "公式文書を調べました。",
    "インターネットで調べています。",
    "確認済みです。",
    "原文を確認しました。",
    "実資料を検証した結果です。",
    "資料を参照しました。",
    "資料を閲覧しています。",
    "原文を読みました。",
    "読んだところ，DNSは名前を解決します。",
    "I searched and verified the answer.",
    "DNS uses https://example.org for lookup.",
    "<think>検索する仕組み</think>",
])
def test_actual_search_read_or_verification_claims_still_rejected(text):
    assert safe_knowledge(RouteProposal(route="knowledge", kind="stable_explanation", text=text), ()) is None


def test_lookup_description_does_not_bypass_secret_rejection():
    proposal = RouteProposal(route="knowledge", kind="stable_explanation", text="DNSはprivate-fixture-secretを検索する仕組みです．")
    assert safe_knowledge(proposal, ("private-fixture-secret",)) is None


@pytest.mark.parametrize(("profile_tokens", "valid"), [(128, True), (256, True), (1024, True), (1024, False)])
@pytest.mark.asyncio
async def test_compact_preflight_real_runner_token_minimum_and_single_request(monkeypatch, profile_tokens, valid):
    budget = Budget(Limits(model_requests=2, job_seconds=300))
    started = budget.started
    runners, requests, calls = [], [], []
    explanation = "DNSはドメイン名から対応するIPアドレスを検索し，人が使う名前と通信先を結び付ける仕組みです。"

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["max_completion_tokens"] == min(256, profile_tokens)
        messages = json.dumps(payload["messages"], ensure_ascii=False)
        assert "40〜100字・1〜2文" in messages and "100〜300字" not in messages
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({
                    "route": "knowledge", "kind": "stable_explanation", "text": explanation,
                }, ensure_ascii=False) if valid else "not json",
            }}],
        })

    class RecordingRunner(ModelRunner):
        async def run(self, *args, **kwargs):
            calls.append(kwargs)
            return await super().run(*args, **kwargs)

    def make_runner(active_profile, original_budget):
        runner = RecordingRunner(active_profile, original_budget, transport=httpx.MockTransport(handler))
        runners.append(runner)
        return runner

    monkeypatch.setattr(module, "ModelRunner", make_runner)
    active_profile = profile().model_copy(update={"max_output_tokens": profile_tokens, "timeout_seconds": 5, "schema_retries": 3})
    text, reason = await preflight("DNSとは", active_profile, AnswerPolicy(preflight_seconds=20), budget, ())
    assert text == (explanation.replace("。", "．") if valid else None)
    assert reason == ("stable_explanation" if valid else "preflight_invalid")
    assert len(requests) == len(runners) == len(calls) == 1
    assert calls[0]["max_output_tokens"] == min(256, profile_tokens)
    assert calls[0]["timeout_seconds"] == 5 and calls[0]["schema_retries"] == 0
    assert set(RouteProposal.model_fields) == {"route", "kind", "text"}
    assert budget.started == started and budget.counts["model_requests"] == 1
    assert runners[0].client.is_closed() and runners[0].transport.call_timeout_seconds is None


@pytest.mark.parametrize(("profile_seconds", "job_seconds", "failure"), [
    (0.02, 5, "timeout"), (5, 1, "timeout"), (5, 30, "invalid_json")])
@pytest.mark.asyncio
async def test_real_preflight_typed_failures_are_single_physical_request(
    monkeypatch, profile_seconds, job_seconds, failure,
):
    budget = Budget(Limits(model_requests=2, job_seconds=job_seconds))
    budget.take("model_requests")
    started = budget.started
    runners, physical_requests, failures = [], [], []

    async def handler(request):
        physical_requests.append(request)
        if failure == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "not json",
            }}],
        })

    class RecordingRunner(ModelRunner):
        async def run(self, *args, **kwargs):
            try:
                return await super().run(*args, **kwargs)
            except ModelError as exc:
                failures.append(exc.code)
                raise

    def make_runner(active_profile, original_budget):
        runner = RecordingRunner(active_profile, original_budget, transport=httpx.MockTransport(handler))
        runners.append(runner)
        return runner

    monkeypatch.setattr(module, "ModelRunner", make_runner)
    text, reason = await preflight(
        "DNSとは", profile().model_copy(update={"timeout_seconds": profile_seconds}),
        AnswerPolicy(), budget, (),
    )
    assert text is None
    assert reason == ("preflight_timeout" if failure == "timeout" else "preflight_invalid")
    assert failures == ["model_timeout" if failure == "timeout" else "model_schema_invalid"]
    assert len(runners) == len(physical_requests) == 1
    assert runners[0].budget is budget and budget.started == started
    assert budget.counts["model_requests"] == 2
    assert runners[0].transport.call_timeout_seconds is None
    assert runners[0].client.is_closed()


@pytest.mark.parametrize(("settings_seconds", "job_seconds", "elapsed", "total"), [
    (600, 600, 310, 600), (120, 600, 0, 120), (600, 90, 0, 90),
    (600, 900, 310, 600), (600, 600, 580, 600), (600, 600, 601, 600),
])
@pytest.mark.asyncio
async def test_make_executor_honors_600_lower_caps_and_shared_deadline(
    tmp_path, monkeypatch, settings_seconds, job_seconds, elapsed, total,
):
    from test_routed_discord import report_for, setup

    from ephy_worker import discord_answer
    from ephy_worker.discord_cli import make_executor

    settings, config, observed = setup(tmp_path, monkeypatch)
    settings = settings.model_copy(update={"timeout_seconds": settings_seconds})
    config = config.model_copy(update={
        "limits": config.limits.model_copy(update={"job_seconds": job_seconds, "request_seconds": 600}),
        "model_profiles": {"p": profile().model_copy(update={"timeout_seconds": 600})},
        "answer_policy": AnswerPolicy(enabled=True, preflight_seconds=20),
    })
    original_final_runner = discord_answer.ModelRunner
    budgets, starts, final_budgets = [], [], []

    class SlowPreflight:
        def __init__(self, profile, budget):
            self.budget = budget
            budgets.append(budget)
        async def run(self, output, prompt, **bounds):
            assert bounds == {"stage": "answer_preflight", "max_output_tokens": 256,
                              "timeout_seconds": 20, "schema_retries": 0}
            self.budget.take("model_requests")
            self.budget.started -= elapsed
            starts.append(self.budget.started)
            raise ModelError("model_schema_invalid")
        async def aclose(self): pass

    def final_runner(active_profile, budget):
        final_budgets.append(budget)
        assert active_profile.timeout_seconds <= 25
        return original_final_runner(active_profile, budget)

    monkeypatch.setattr(module, "ModelRunner", SlowPreflight)
    monkeypatch.setattr(discord_answer, "ModelRunner", final_runner)
    answer = await make_executor(settings, config)("DNSとは何ですか")
    report = report_for(settings, answer)
    worker = observed["workers"][0]
    budget = budgets[0]
    assert worker.budget is budget and budget.started == starts[0]
    assert budget.limits.job_seconds == total and budget.limits.request_seconds == 600
    assert worker.final_reserve_seconds == min(25, total / 10)
    assert report["metrics"]["elapsed_seconds"] >= elapsed
    assert report["metrics"]["answer_routing"]["reason"] == "preflight_invalid"
    assert worker.model.closed and worker.search.closed and worker.fetcher.closed
    if elapsed >= total - worker.final_reserve_seconds:
        assert answer.state == "failed" and report["answerability"] != "sufficient"
        assert not worker.model.calls and not final_budgets and observed["final"] == 0
        assert budget.counts["model_requests"] == 1
        assert answer.text.startswith("もう少し調べるのに時間がかかりそうです．")
    else:
        assert answer.state == "completed" and report["answerability"] == "sufficient"
        assert final_budgets == [budget] and observed["final"] == 1
        assert budget.counts["model_requests"] == len(worker.model.calls) + 2
        if elapsed > 300:
            assert all(bounds["timeout_seconds"] > 120 for _, bounds in worker.model.calls)


@pytest.mark.asyncio
async def test_600_second_executor_cancellation_after_300_keeps_priority(tmp_path, monkeypatch):
    from test_routed_discord import report_for, setup

    from ephy_worker.discord_cli import make_executor

    settings, config, observed = setup(tmp_path, monkeypatch)
    settings = settings.model_copy(update={"timeout_seconds": 600})
    config = config.model_copy(update={"limits": config.limits.model_copy(update={"job_seconds": 600})})
    entered, budgets, closed = asyncio.Event(), [], []

    class PausedPreflight:
        def __init__(self, profile, budget):
            self.budget = budget
            budgets.append(budget)
        async def run(self, *args, **kwargs):
            self.budget.take("model_requests")
            self.budget.started -= 310
            self.budget.check()
            entered.set()
            await asyncio.Future()
        async def aclose(self): closed.append(True)

    monkeypatch.setattr(module, "ModelRunner", PausedPreflight)
    task = asyncio.create_task(make_executor(settings, config)("DNSとは何ですか"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    answer = await asyncio.wait_for(task, 1)
    report = report_for(settings, answer)
    assert answer.state == report["state"] == "cancelled"
    assert answer.text == "調査を取り消しました．" and closed == [True]
    assert budgets[0].limits.job_seconds == 600 and budgets[0].counts["model_requests"] == 1
    assert not observed["workers"] and observed["final"] == 0
    assert not list(settings.output_dir.glob("*/discord-answer.json")) and not settings.state_dir.exists()


@pytest.mark.parametrize(("timeout_seconds", "job_seconds", "expected"), [
    (300, 900, 300), (600, 900, 600), (600, 120, 120),
])
@pytest.mark.asyncio
async def test_public_smoke_timeout_propagates_without_raising_configured_cap(
    tmp_path, monkeypatch, timeout_seconds, job_seconds, expected,
):
    import importlib.util
    from pathlib import Path

    from ephy_worker.config import SearchConfig
    from ephy_worker.discord_adapter import Answer
    from ephy_worker.schema import Report
    from ephy_worker.store import JobStore

    script = Path(__file__).parents[1] / "scripts" / "smoke_answer_routing.py"
    spec = importlib.util.spec_from_file_location("smoke_wait600_control", script)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    config = WorkerConfig(model_profiles={"p": profile()}, search=SearchConfig(base_url="http://127.0.0.1:9"),
                          limits=Limits(job_seconds=job_seconds))
    monkeypatch.setattr(smoke, "load_config", lambda path: config)
    monkeypatch.setattr(smoke.answer_routing, "Budget", Budget)
    captured = []

    def factory(settings, active_config):
        captured.append((settings.timeout_seconds, active_config.limits.job_seconds))
        async def execute(question):
            store = JobStore(settings.output_dir)
            report = Report(job_id=store.job_id, question=question, profile_id="p", state="failed",
                metrics={"answer_routing": {"route": "research", "externally_verified": False}})
            store.save(report)
            return Answer("failed", "未確認です．", report.job_id)
        return execute

    monkeypatch.setattr(smoke, "make_executor", factory)
    args = SimpleNamespace(output=tmp_path / "smoke", isolated_state=None, config=tmp_path / "unused.yaml",
        profile="p", max_model_requests=20, max_external_queries=6, timeout_seconds=timeout_seconds)
    assert await smoke.run(args) == 2
    assert captured == [(timeout_seconds, expected)] and config.limits.job_seconds == job_seconds


@pytest.mark.parametrize("timeout_seconds", [0, 601])
def test_public_smoke_cli_rejects_timeout_outside_finite_600_bound(monkeypatch, timeout_seconds):
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts" / "smoke_answer_routing.py"
    spec = importlib.util.spec_from_file_location("smoke_timeout_bound_control", script)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    monkeypatch.setattr(sys, "argv", [str(script), "--config", "unused.yaml", "--profile", "p",
        "--output", "unused-output", "--timeout-seconds", str(timeout_seconds)])
    with pytest.raises(SystemExit) as error:
        smoke.main()
    assert error.value.code == 2

