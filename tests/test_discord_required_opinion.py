"""Required opinions stay tentative，bounded and distinct from verified evidence．"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_discord_answer import acquired_progress_report, fixture_report, search_preview_report

from ephy_worker import discord_answer as module
from ephy_worker.budget import Budget
from ephy_worker.config import ModelProfile, SearchConfig, WorkerConfig
from ephy_worker.discord_adapter import DiscordSettings
from ephy_worker.discord_cli import make_executor
from ephy_worker.models import ModelError
from ephy_worker.schema import Limits
from ephy_worker.store import JobStore


@pytest.fixture
def context(tmp_path):
    store = JobStore(tmp_path / "answers")
    report = fixture_report().model_copy(update={"job_id": store.job_id})
    profile = ModelProfile(
        family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture",
        output_mode="prompted_json", timeout_seconds=600, max_output_tokens=4096,
    )
    return report, profile, Budget(Limits(job_seconds=600, model_requests=5)), store


def record(store):
    return json.loads((store.directory / "discord-answer.json").read_text(encoding="utf-8"))


def runner(monkeypatch, *, opinion="条件に合うか小さく試し，比較して選ぶのがよいと思います．",
           claim_ids=None, error=None, cleanup=None, entered=None):
    observed = {"calls": [], "profiles": [], "closed": 0}

    class Runner:
        def __init__(self, profile, budget):
            observed["profiles"].append(profile)
            self.budget = budget

        async def run(self, output_type, prompt, *, stage):
            self.budget.take("model_requests")
            observed["calls"].append((output_type, prompt, stage))
            if entered is not None:
                entered.set()
                await asyncio.Future()
            if error is not None:
                raise error
            if output_type is module.OpinionDraft:
                return output_type(opinion=opinion)
            return output_type(
                text="機能Aを利用できます．", claim_ids=["c1"], opinion=opinion,
                opinion_claim_ids=["c1"] if claim_ids is None else claim_ids,
            )

        async def aclose(self):
            observed["closed"] += 1
            if cleanup == "timeout":
                await asyncio.Future()
            if cleanup == "error":
                raise RuntimeError("private cleanup failure")

    monkeypatch.setattr(module, "ModelRunner", Runner)
    return observed


def settings(tmp_path, **kwargs):
    return DiscordSettings(
        application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "results",
        state_dir=tmp_path / "state", **kwargs,
    )


def test_setting_defaults_and_strict_opt_in(tmp_path):
    default = settings(tmp_path)
    assert default.require_opinion is False and default.answer_timeout_seconds == 25
    assert settings(tmp_path, require_opinion=True, answer_timeout_seconds=300).answer_timeout_seconds == 300
    for value in (0, 1, "true", "false", None):
        with pytest.raises(ValidationError):
            settings(tmp_path, require_opinion=value)
    for value in (0, 601, "300", True, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            settings(tmp_path, answer_timeout_seconds=value)


async def test_grounded_required_opinion_one_call_longer_budget_and_canonical_report(context, monkeypatch):
    report, profile, budget, store = context
    report.state, report.answerability = "partial", "limited"
    before = report.model_dump_json()
    store.save(report)
    canonical = {p.name: p.read_bytes() for p in store.directory.iterdir()}
    observed = runner(monkeypatch)
    text = await module.compose_answer(report, profile, budget, store, require_opinion=True, timeout_seconds=300)
    assert text.count("\n\nわたしとしては，") == 1 and "条件に合うか小さく試し" in text
    assert "標準設定の場合" in text and "古い版での対応" in text and "一部まで" in text
    assert "https://example.org/manual" in text
    assert len(observed["calls"]) == budget.counts["model_requests"] == observed["closed"] == 1
    assert observed["calls"][0][0] is module.AnswerDraft and "必ずopinion" in observed["calls"][0][1]
    bounded = observed["profiles"][0]
    assert 25 < bounded.timeout_seconds <= 300 and bounded.schema_retries == 0
    assert report.model_dump_json() == before
    assert all((store.directory / name).read_bytes() == raw for name, raw in canonical.items())
    assert record(store)["opinion"]["status"] == "generated" and record(store)["reason"] == "ok"


@pytest.mark.parametrize("material", ["none", "body", "preview"])
async def test_no_facts_single_opinion_call_with_only_bounded_unchecked_context(context, monkeypatch, material):
    report, profile, budget, store = context
    if material == "body":
        report = acquired_progress_report(8)
    elif material == "preview":
        report = search_preview_report(8)
    else:
        report.claims.clear()
    report.job_id = store.job_id
    original = report.model_dump_json()
    previous = "過去のbot回答で別の候補を紹介しました．"
    observed = runner(monkeypatch)
    text = await module.compose_answer(
        report, profile, budget, store, require_opinion=True, timeout_seconds=300, previous_answer=previous,
    )
    assert "わたしとしては，暫定的な見方ですが，" in text
    assert len(observed["calls"]) == 1 and observed["calls"][0][0] is module.OpinionDraft
    prompt = observed["calls"][0][1]
    data = json.loads(prompt[prompt.index('{"question"'):])
    assert "facts" not in data and data["previous_answer"] == {
        "text": previous, "status": "untrusted_conversation_context",
    }
    unchecked = data["unchecked_material"]
    assert len(unchecked["excerpts"]) <= 5 and all(len(s) <= 180 for s in unchecked["excerpts"])
    assert previous not in json.dumps(unchecked, ensure_ascii=False) and "https://" not in prompt
    assert "新しい事実・証拠・個人的な体験を作らず" in prompt
    if material == "none":
        assert unchecked == {"status": "none", "excerpts": []}
    else:
        assert unchecked["status"].endswith("_unchecked") and unchecked["excerpts"]
        assert "https://example.org" in text
        assert ("内容の照合は未完了" if material == "body" else "リンク先の本文はまだ確認していません") in text
    saved = record(store)
    assert saved["verified_facts"] == 0 and saved["opinion"]["status"] == "generated"
    assert report.model_dump_json() == original


@pytest.mark.parametrize("no_facts", [False, True])
@pytest.mark.parametrize("restriction", ["budget", "time", "model_error", "model_timeout", "invalid"])
async def test_required_opinion_fallback_is_honest_and_does_not_hide_generation_reason(context, monkeypatch, no_facts, restriction):
    report, profile, budget, store = context
    if no_facts:
        report.claims.clear()
    error = None
    if restriction == "budget":
        budget.counts["model_requests"] = budget.limits.model_requests
    elif restriction == "time":
        budget.started -= 600
    elif restriction.startswith("model_"):
        error = ModelError("model_timeout" if restriction == "model_timeout" else "model_invalid_output")
    observed = runner(monkeypatch, opinion="<think>private thought</think>" if restriction == "invalid" else "意見です．", error=error)
    text = await module.compose_answer(report, profile, budget, store, require_opinion=True, timeout_seconds=300)
    saved = record(store)
    assert text.count("\n\nわたしとしては，") == 1
    assert saved["opinion"]["status"] == "fallback" and saved["status"] == "fallback"
    assert saved["reason"] == {
        "budget": "model_budget_exhausted", "time": "time_budget_exhausted", "model_error": "generation_failed",
        "model_timeout": "generation_timeout", "invalid": "invalid_opinion",
    }[restriction]
    assert len(observed["calls"]) == (0 if restriction in {"budget", "time"} else 1)
    assert "private" not in text and "<think>" not in text
    assert "暫定" in text if no_facts else "https://example.org/manual" in text


@pytest.mark.parametrize("no_facts", [False, True])
@pytest.mark.parametrize("opinion", [
    "", "   ", "根拠が不足しているため判断を保留します．", "わたしとしては，分かりません．",
    "https://example.org/invented", "&amp;lt;think&amp;gt;thought&amp;lt;/think&amp;gt;",
    "fixture-secret", "fixture&amp;#45;secret", "e1の見方です．", "research-fixtureの意見です．",
    "わたしは以前使ったことがあります．", "私の経験から勧めます．", "僕は小さく試すのがよいと思います．",
])
async def test_required_invalid_opinion_is_not_published_or_marked_generated(context, monkeypatch, no_facts, opinion):
    report, profile, budget, store = context
    report.job_id = "research-fixture"
    if no_facts:
        report.claims.clear()
    runner(monkeypatch, opinion=opinion)
    text = await module.compose_answer(report, profile, budget, store, require_opinion=True, known_secrets=("fixture-secret",))
    saved = record(store)
    assert "わたしとしては，" in text and saved["opinion"]["status"] == "fallback"
    assert saved["reason"] in {"invalid_opinion", "generation_failed"}
    assert "fixture-secret" not in json.dumps(saved) and "thought" not in text
    assert "research-fixture" not in text and "e1" not in text
    assert "以前使った" not in text and "私の経験" not in text and "僕は" not in text


@pytest.mark.parametrize("basis", [[], ["unknown"], ["c1", "c1"], ["c2"]])
async def test_required_grounded_opinion_still_requires_selected_verified_claims(context, monkeypatch, basis):
    report, profile, budget, store = context
    report.claims.append(report.claims[0].model_copy(update={"claim_id": "c2", "text": "別の確認済み説明です．"}))
    observed = runner(monkeypatch, claim_ids=basis)
    text = await module.compose_answer(report, profile, budget, store, require_opinion=True)
    assert len(observed["calls"]) == 1 and "わたしとしては，" in text
    assert record(store)["opinion"]["status"] == "fallback" and record(store)["reason"] == "invalid_opinion"


async def test_encoded_secret_unchecked_excerpt_is_not_sent_to_opinion_model(context, monkeypatch):
    _, profile, budget, store = context
    report = acquired_progress_report(body="fixture&#45;secret の説明です．")
    report.question = "fixture&amp;#45;secret について"
    observed = runner(monkeypatch)
    await module.compose_answer(report, profile, budget, store, require_opinion=True, known_secrets=("fixture-secret",))
    prompt = observed["calls"][0][1]
    data = json.loads(prompt[prompt.index('{"question"'):])
    assert data["unchecked_material"]["excerpts"] == [] and "fixture-secret" not in prompt


@pytest.mark.parametrize("no_facts", [False, True])
async def test_required_cleanup_timeout_replaces_generated_opinion_with_explicit_fallback(context, monkeypatch, no_facts):
    report, profile, budget, store = context
    if no_facts:
        report.claims.clear()
    observed = runner(monkeypatch, opinion="generated opinion marker", cleanup="timeout")
    text = await module.compose_answer(report, profile, budget, store, require_opinion=True)
    saved = record(store)
    assert text.count("\n\nわたしとしては，") == 1 and "generated opinion marker" not in text
    assert saved["status"] == saved["opinion"]["status"] == "fallback"
    assert saved["reason"] == saved["cleanup_reason"] == "cleanup_timeout" and observed["closed"] == 1


@pytest.mark.parametrize("no_facts", [False, True])
async def test_required_cancel_before_or_during_generation_never_adds_opinion(context, monkeypatch, no_facts):
    report, profile, budget, store = context
    if no_facts:
        report.claims.clear()
    entered = asyncio.Event()
    observed = runner(monkeypatch, entered=entered)
    report.state = "cancelled"
    text = await module.compose_answer(report, profile, budget, store, require_opinion=True)
    assert text == "調査を取り消しました．" and not observed["calls"]
    report.state = "partial"
    task = asyncio.create_task(module.compose_answer(report, profile, budget, store, require_opinion=True))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record(store)["reason"] == "cancelled" and "わたしとしては，" not in record(store)["text"]
    assert record(store)["opinion"]["status"] == "omitted" and observed["closed"] == 1


@pytest.mark.parametrize("failure", ["generation", "cleanup", "save"])
async def test_required_unexpected_failures_stay_failed_and_propagate(context, monkeypatch, failure):
    report, profile, budget, store = context
    report.claims.clear()
    runner(monkeypatch, error=RuntimeError("private generation") if failure == "generation" else None,
           cleanup="error" if failure == "cleanup" else None)
    if failure == "save":
        monkeypatch.setattr(module, "_save", lambda *args: (_ for _ in ()).throw(OSError("private save")))
    with pytest.raises((RuntimeError, OSError)):
        await module.compose_answer(report, profile, budget, store, require_opinion=True)
    if failure != "save":
        saved = record(store)
        assert saved["status"] == "failed" and "private" not in saved["text"]


@pytest.mark.parametrize("total,answer_timeout", [(600, 300), (100, 300), (10, 5)])
async def test_executor_required_reserves_within_original_aggregate_budget(tmp_path, monkeypatch, total, answer_timeout):
    from ephy_worker import research

    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture", output_mode="prompted_json")
    config = WorkerConfig(model_profiles={"fixture": profile}, search=SearchConfig(base_url="http://127.0.0.1:9"),
                          limits=Limits(job_seconds=total, model_requests=5))
    configured = settings(tmp_path, require_opinion=True, answer_timeout_seconds=answer_timeout, timeout_seconds=total)
    observed = {}

    class Research:
        def __init__(self, config, profile, store):
            observed["limits"] = config.limits
            self.budget = Budget(config.limits)
            self.budget.counts["model_requests"] = 3
            self.store = store

        async def run(self, question):
            return fixture_report().model_copy(update={"job_id": self.store.job_id})

    async def compose(report, profile, budget, store, **kwargs):
        observed.update(kwargs)
        observed["aggregate"] = budget
        return "わたしとしては，条件を比べるのがよいと思います．"

    monkeypatch.setattr(research, "ResearchExecutor", Research)
    monkeypatch.setattr(module, "compose_answer", compose)
    result = await make_executor(configured, config)("公開情報の比較")
    assert "わたし" in result.text
    assert observed["limits"].job_seconds == total - min(answer_timeout, total / 2)
    assert observed["limits"].model_requests == 4
    assert observed["aggregate"].limits.job_seconds == total and observed["aggregate"].counts["model_requests"] == 3
    assert observed["require_opinion"] is True and observed["timeout_seconds"] == answer_timeout


async def test_legacy_no_facts_default_does_not_call_model(context, monkeypatch):
    report, profile, budget, store = context
    report.claims.clear()
    mock = AsyncMock(side_effect=AssertionError("Legacy no-fact answer must not generate"))
    monkeypatch.setattr(module, "ModelRunner", mock)
    text = await module.compose_answer(report, profile, budget, store)
    assert "わたしとしては，" not in text and record(store)["opinion"]["status"] == "omitted"
    mock.assert_not_called()
