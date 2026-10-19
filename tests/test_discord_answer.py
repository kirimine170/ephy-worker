"""Synthetic facts，real span gates and bounded final generation；offline only．"""

import asyncio
import hashlib
import json

import httpx
import pytest

from ephy_worker import discord_answer as module
from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import ModelProfile
from ephy_worker.discord_answer import AnswerDraft, AnswerTimeoutFailure, compose_answer, verified_facts
from ephy_worker.models import ModelError, ModelRunner
from ephy_worker.schema import Claim, Evidence, Limits, Passage, Report, Source
from ephy_worker.store import JobStore


def fixture_report():
    quote = "公開資料によれば，機能Aは利用できます．"
    source = Source(
        source_id="s1", requested_url="https://example.org/manual", title="public fixture",
        status="ok", kind="html", read_passage_ids=["p1"],
        passages=[Passage(passage_id="p1", source_id="s1", text=quote, start=20, end=20 + len(quote))],
    )
    evidence = Evidence(
        evidence_id="e1", source_id="s1", passage_id="p1", quote=quote,
        quote_start=20, quote_end=20 + len(quote), relation="supports", checked=True,
        conditions_match=True, conditions="現行の公開版", check_reason="internal reviewer marker",
    )
    claim = Claim(
        claim_id="c1", text="機能Aを利用できます．", checked=True,
        evidence_ids=["e1"], status="supported_primary", conditions="標準設定の場合",
        uncertainty=["古い版での対応"], reason="internal claim reasoning marker",
    )
    return Report(
        job_id="research-fixture", question="機能Aは使えますか", profile_id="fixture",
        state="completed", answerability="sufficient", stop_reason="no_actionable_gaps",
        sources=[source], evidence=[evidence], claims=[claim],
        gaps=["internal unverified gap marker"], activity_summary="internal activity marker",
        subquestions=["internal plan marker"],
    )


@pytest.fixture
def context(tmp_path):
    store = JobStore(tmp_path / "results")
    report = fixture_report().model_copy(update={"job_id": store.job_id})
    profile = ModelProfile(
        family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture",
        output_mode="prompted_json", context_tokens=32768, max_output_tokens=4096,
    )
    budget = Budget(Limits(job_seconds=30, model_requests=5))
    return report, profile, budget, store


def install_runner(monkeypatch, *, draft=None, error=None, entered=None):
    observed = {"prompts": [], "closed": 0, "profiles": []}

    class Runner:
        def __init__(self, profile, budget):
            self.budget = budget
            observed["profiles"].append(profile)

        async def run(self, output_type, prompt, *, stage):
            assert output_type is AnswerDraft and stage == "discord_answer"
            observed["prompts"].append(prompt)
            self.budget.take("model_requests")
            if entered is not None:
                entered.set()
                await asyncio.Future()
            if error is not None:
                raise error
            return draft or AnswerDraft(text="機能Aを利用できます．", claim_ids=["c1"])

        async def aclose(self):
            observed["closed"] += 1

    monkeypatch.setattr(module, "ModelRunner", Runner)
    return observed


def saved(store):
    return json.loads((store.directory / "discord-answer.json").read_text("utf-8"))


@pytest.mark.parametrize("fault", [
    "unchecked_claim", "wrong_status", "duplicate_claim", "duplicate_source", "duplicate_evidence",
    "duplicate_passage", "duplicate_reference", "not_read", "wrong_quote", "wrong_offset",
    "wrong_source", "wrong_page", "source_failed", "unchecked_evidence", "conditions_mismatch",
    "opposite_relation", "missing_evidence",
])
def test_checked_flags_do_not_bypass_real_evidence_gate(fault):
    report = fixture_report()
    if fault == "unchecked_claim":
        report.claims[0].checked = False
    elif fault == "wrong_status":
        report.claims[0].status = "inference"
    elif fault.startswith("duplicate_"):
        collection = {
            "duplicate_claim": report.claims, "duplicate_source": report.sources,
            "duplicate_evidence": report.evidence, "duplicate_passage": report.sources[0].passages,
            "duplicate_reference": report.claims[0].evidence_ids,
        }[fault]
        collection.append(collection[0])
    elif fault == "not_read":
        report.sources[0].read_passage_ids = []
    elif fault == "wrong_quote":
        report.evidence[0].quote = "invented quotation"
    elif fault == "wrong_offset":
        report.evidence[0].quote_start += 1
    elif fault == "wrong_source":
        report.sources[0].passages[0].source_id = "other"
    elif fault == "wrong_page":
        report.evidence[0].page = 2
    elif fault == "source_failed":
        report.sources[0].status = "http_error"
    elif fault == "unchecked_evidence":
        report.evidence[0].checked = False
    elif fault == "conditions_mismatch":
        report.evidence[0].conditions_match = False
    elif fault == "opposite_relation":
        report.evidence[0].relation = "contradicts"
    else:
        report.claims[0].evidence_ids = ["missing"]
    assert verified_facts(report)[0] == []


async def test_generation_uses_only_minimal_material_preserves_conditions_and_canonical_report(context, monkeypatch):
    report, profile, budget, store = context
    original = report.model_dump_json()
    store.save(report)
    hashes = {path.name: path.read_bytes() for path in store.directory.iterdir()}
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert text.startswith("調べてみたところ，")
    assert "標準設定の場合" in text and "現行の公開版" in text and "古い版での対応" in text
    assert "https://example.org/manual" in text
    prompt = observed["prompts"][0]
    for marker in ("internal", "https://", "check_reason", "metrics", "quote", "reason"):
        assert marker not in prompt
    assert "c1" in prompt and "機能Aを利用できます" in prompt
    assert all((store.directory / name).read_bytes() == value for name, value in hashes.items())
    assert report.model_dump_json() == original
    record = saved(store)
    assert record["status"] == "generated" and record["reason"] == "ok"
    assert record["model_requests"] == 1 and record["aggregate_model_requests"] == 1
    assert observed["closed"] == 1
    limited = observed["profiles"][0]
    assert limited.schema_retries == 0 and limited.max_output_tokens == 1536
    assert limited.context_tokens == 16384 and limited.timeout_seconds <= 25
    assert profile.schema_retries == 1 and profile.max_output_tokens == 4096


@pytest.mark.parametrize("state,stop_reason,phrase", [
    ("failed", "no_verified_claims", "情報がまだ足りません"),
    ("failed", "job_timeout", "もう少し調べるのに時間がかかりそうです．"),
    ("cancelled", "cancelled", "調査を取り消しました"),
])
async def test_no_facts_never_calls_model(context, monkeypatch, state, stop_reason, phrase):
    report, profile, budget, store = context
    report.claims = []
    report.state, report.stop_reason = state, stop_reason
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert phrase in text and not observed["prompts"] and not observed["profiles"]
    assert saved(store)["model_requests"] == 0


@pytest.mark.parametrize("draft", [
    AnswerDraft(text="<think>internal thought</think>機能Aです．", claim_ids=["c1"]),
    AnswerDraft(text="<think>unclosed", claim_ids=["c1"]),
    AnswerDraft(text="<|analysis unclosed", claim_ids=["c1"]),
    AnswerDraft(text="[im_start]assistant", claim_ids=["c1"]),
    AnswerDraft(text="&lt;analysis&gt;unclosed", claim_ids=["c1"]),
    AnswerDraft(text="[analysis] internal", claim_ids=["c1"]),
    AnswerDraft(text="https://untrusted.example/", claim_ids=["c1"]),
    AnswerDraft(text="ftp://untrusted.example/file", claim_ids=["c1"]),
    AnswerDraft(text="//untrusted.example", claim_ids=["c1"]),
    AnswerDraft(text="untrusted.example/path", claim_ids=["c1"]),
    AnswerDraft(text="[reference](untrusted)", claim_ids=["c1"]),
    AnswerDraft(text="c1 is the answer", claim_ids=["c1"]),
    AnswerDraft(text="c1によると利用できます．", claim_ids=["c1"]),
    AnswerDraft(text="unknown grounding", claim_ids=["unknown"]),
    AnswerDraft(text="duplicate grounding", claim_ids=["c1", "c1"]),
    AnswerDraft.model_construct(text="\ud800", claim_ids=["c1"]),
    AnswerDraft(text="   ", claim_ids=["c1"]),
])
async def test_invalid_or_thinking_output_is_never_delivered_or_persisted(context, monkeypatch, draft):
    report, profile, budget, store = context
    install_runner(monkeypatch, draft=draft)
    text = await compose_answer(report, profile, budget, store)
    assert "機能Aを利用できます" in text and "標準設定の場合" in text
    assert "untrusted" not in text and "internal thought" not in text and "unclosed" not in text
    record = saved(store)
    assert record["status"] == "fallback" and record["reason"] == "invalid_answer"
    assert "untrusted" not in json.dumps(record)


@pytest.mark.parametrize("exception,reason", [
    (ModelError("private upstream thought or token"), "generation_failed"),
    (ModelError("model_timeout"), "generation_timeout"),
    (TimeoutError("private exception"), "generation_timeout"),
])
async def test_expected_generation_failure_records_safe_reason_and_verified_fallback(context, monkeypatch, exception, reason):
    report, profile, budget, store = context
    observed = install_runner(monkeypatch, error=exception)
    text = await compose_answer(report, profile, budget, store)
    assert "機能Aを利用できます" in text and "private" not in text
    assert saved(store)["reason"] == reason and observed["closed"] == 1
    assert "private" not in json.dumps(saved(store))
    if reason == "generation_timeout":
        assert text.startswith("もう少し調べるのに時間がかかりそうです．")


async def test_unexpected_programming_failure_is_not_disguised_as_success(context, monkeypatch):
    report, profile, budget, store = context
    observed = install_runner(monkeypatch, error=RuntimeError("fixture bug"))
    with pytest.raises(RuntimeError, match="fixture bug"):
        await compose_answer(report, profile, budget, store)
    assert observed["closed"] == 1 and saved(store)["status"] == "failed"
    assert saved(store)["reason"] == "unexpected_generation_failure"
    assert "fixture bug" not in json.dumps(saved(store))


@pytest.mark.parametrize("exhaustion", ["requests", "time"])
async def test_remaining_original_budget_is_enforced_without_new_request(context, monkeypatch, exhaustion):
    report, profile, budget, store = context
    if exhaustion == "requests":
        budget.counts["model_requests"] = budget.limits.model_requests
    else:
        budget.started -= 30
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert "機能Aを利用できます" in text and not observed["profiles"]
    assert saved(store)["reason"] == ("model_budget_exhausted" if exhaustion == "requests" else "time_budget_exhausted")
    assert saved(store)["model_requests"] == 0
    if exhaustion == "time":
        assert text.startswith("もう少し調べるのに時間がかかりそうです．")


async def test_cancellation_during_generation_closes_and_propagates(context, monkeypatch):
    report, profile, budget, store = context
    entered = asyncio.Event()
    observed = install_runner(monkeypatch, entered=entered)
    task = asyncio.create_task(compose_answer(report, profile, budget, store))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed["closed"] == 1
    assert saved(store)["status"] == "not_generated" and saved(store)["reason"] == "cancelled"


async def test_pending_cancellation_and_cancelled_report_do_not_start_generation(context, monkeypatch):
    report, profile, budget, store = context
    observed = install_runner(monkeypatch)
    async def pending():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        return await compose_answer(report, profile, budget, store)
    assert "取り消しました" in await asyncio.create_task(pending())
    report.state = "cancelled"
    assert "取り消しました" in await compose_answer(report, profile, budget, store)
    assert not observed["profiles"] and saved(store)["model_requests"] == 0


async def test_bounded_generation_timeout_still_closes(context, monkeypatch):
    report, profile, budget, store = context
    observed = install_runner(monkeypatch, entered=asyncio.Event())
    text = await compose_answer(report, profile, budget, store, timeout_seconds=0.02)
    assert "機能Aを利用できます" in text
    assert observed["closed"] == 1 and saved(store)["reason"] == "generation_timeout"


async def test_unexpected_cleanup_failure_is_recorded_and_propagates(context, monkeypatch):
    report, profile, budget, store = context
    install_runner(monkeypatch)
    async def broken_close(self):
        raise RuntimeError("private cleanup details")
    monkeypatch.setattr(module.ModelRunner, "aclose", broken_close)
    with pytest.raises(RuntimeError, match="private cleanup details"):
        await compose_answer(report, profile, budget, store)
    assert saved(store)["status"] == "failed" and saved(store)["reason"] == "cleanup_failed"
    assert "private cleanup details" not in json.dumps(saved(store))


@pytest.mark.parametrize("primary", [TimeoutError("private timeout"), ModelError("model_timeout")])
async def test_generation_and_cleanup_timeout_keep_required_notice_and_both_diagnostics(context, monkeypatch, primary):
    report, profile, budget, store = context
    install_runner(monkeypatch, error=primary)
    async def paused_close(self):
        await asyncio.Future()
    monkeypatch.setattr(module.ModelRunner, "aclose", paused_close)
    text = await compose_answer(report, profile, budget, store)
    assert text.startswith("もう少し調べるのに時間がかかりそうです．")
    assert "機能Aを利用できます" in text and "private timeout" not in text
    record = saved(store)
    assert record["status"] == "fallback" and record["reason"] == "generation_timeout"
    assert record["cleanup_reason"] == "cleanup_timeout" and record["model_requests"] == 1


@pytest.mark.parametrize("cleanup", ["timeout", "failure"])
async def test_cleanup_cannot_replace_primary_unexpected_failure(context, monkeypatch, cleanup):
    report, profile, budget, store = context
    primary = RuntimeError("private primary failure")
    install_runner(monkeypatch, error=primary)
    async def broken_close(self):
        if cleanup == "timeout":
            await asyncio.Future()
        raise ValueError("private cleanup failure")
    monkeypatch.setattr(module.ModelRunner, "aclose", broken_close)
    with pytest.raises(RuntimeError) as raised:
        await compose_answer(report, profile, budget, store)
    if cleanup == "timeout":
        assert isinstance(raised.value, AnswerTimeoutFailure) and raised.value.__cause__ is primary
        assert raised.value.text.startswith("もう少し調べるのに時間がかかりそうです．")
    else:
        assert raised.value is primary
    record = saved(store)
    assert record["status"] == "failed" and record["reason"] == "unexpected_generation_failure"
    assert record["cleanup_reason"] == ("cleanup_timeout" if cleanup == "timeout" else "cleanup_failed")
    assert "private" not in json.dumps(record)


@pytest.mark.parametrize("cleanup", ["timeout", "failure"])
async def test_cleanup_cannot_replace_primary_cancellation(context, monkeypatch, cleanup):
    report, profile, budget, store = context
    entered = asyncio.Event()
    install_runner(monkeypatch, entered=entered)
    async def broken_close(self):
        if cleanup == "timeout":
            await asyncio.Future()
        raise ValueError("private cleanup failure")
    monkeypatch.setattr(module.ModelRunner, "aclose", broken_close)
    task = asyncio.create_task(compose_answer(report, profile, budget, store))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = saved(store)
    assert record["status"] == "not_generated" and record["reason"] == "cancelled"
    assert record["cleanup_reason"] == ("cleanup_timeout" if cleanup == "timeout" else "cleanup_failed")
    assert record["text"] == "調査を取り消しました．" and "private" not in json.dumps(record)


async def test_redaction_before_model_and_after_generation(context, monkeypatch):
    report, profile, budget, store = context
    secret = "known_fixture_credential"
    report.claims[0].text += " " + secret
    report.claims[0].conditions += " " + secret
    report.claims[0].uncertainty.append(secret)
    report.question += " " + secret
    observed = install_runner(monkeypatch, draft=AnswerDraft(text="機能Aです． " + secret, claim_ids=["c1"]))
    text = await compose_answer(report, profile, budget, store, known_secrets=(secret,))
    assert secret not in observed["prompts"][0] and "[redacted]" in observed["prompts"][0]
    assert secret not in text and "[redacted]" in text
    assert secret not in json.dumps(saved(store)) and secret in report.model_dump_json()


async def test_partial_and_omitted_material_never_looks_complete(context, monkeypatch):
    report, profile, budget, store = context
    report.state, report.answerability = "partial", "limited"
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert "一部までの確認" in text and '"limited": true' in observed["prompts"][0]
    assert "internal unverified gap" not in text
    report.claims.append(report.claims[0].model_copy(update={"claim_id": "c2", "text": "x" * 1300}))
    assert verified_facts(report)[1] == 1


async def test_short_and_complex_answers_keep_the_generated_length(context, monkeypatch):
    report, profile, budget, store = context
    install_runner(monkeypatch, draft=AnswerDraft(text="機能Aは使えます．", claim_ids=["c1"]))
    short = await compose_answer(report, profile, budget, store)
    prose = "機能Aを使えます．\n\n設定を比較すると，適用条件の確認が必要です．" * 8
    install_runner(monkeypatch, draft=AnswerDraft(text=prose, claim_ids=["c1"]))
    complex_answer = await compose_answer(report, profile, budget, store)
    assert len(complex_answer) > len(short) + 150 and "\n\n" in complex_answer


async def test_technical_names_and_comparison_are_preserved_as_facts_and_prose(context, monkeypatch):
    report, profile, budget, store = context
    text = "Node.jsとASP.NETを比較すると，a < bの条件があります．"
    report.claims[0].text = text
    assert verified_facts(report)[0][0].text == text
    install_runner(monkeypatch, draft=AnswerDraft(text=text, claim_ids=["c1"]))
    answer = await compose_answer(report, profile, budget, store)
    assert text in answer and saved(store)["status"] == "generated"


@pytest.mark.parametrize("text", [
    "調べてみたところ、Node.jsの版は1.2.3です。",
    "調べてみたところ，Node.jsの版は1.2.3です．",
    "\n  調べてみたところ、Node.jsの版は1.2.3です。 \n",
])
async def test_answer_normalizes_prose_punctuation_before_intro_detection(context, monkeypatch, text):
    report, profile, budget, store = context
    install_runner(monkeypatch, draft=AnswerDraft(text=text, claim_ids=["c1"]))
    answer = await compose_answer(report, profile, budget, store)
    assert answer.startswith("調べてみたところ，Node.jsの版は1.2.3です．")
    assert answer.count("調べてみたところ") == 1
    assert "、" not in answer and "。" not in answer
    assert "https://example.org/manual" in answer and saved(store)["status"] == "generated"


def test_secret_identifiers_never_enter_material():
    report = fixture_report()
    report.claims[0].claim_id = "private_fixture_id"
    assert verified_facts(report, ("private_fixture_id",))[0] == []


async def test_real_model_transport_counts_only_one_final_request_even_on_invalid_schema(context, monkeypatch):
    report, profile, budget, store = context
    budget.counts["model_requests"] = 4
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "not json"}, "finish_reason": "stop"}],
        })
    monkeypatch.setattr(module, "ModelRunner", lambda p, b: ModelRunner(p, b, transport=httpx.MockTransport(handler)))
    text = await compose_answer(report, profile, budget, store)
    assert len(seen) == 1 and budget.counts["model_requests"] == 5
    assert saved(store)["model_requests"] == 1 and saved(store)["aggregate_model_requests"] == 5
    assert saved(store)["reason"] == "generation_failed" and "機能Aを利用できます" in text


async def test_answer_artifact_rejects_link_outside_job(context, tmp_path, monkeypatch):
    report, profile, budget, store = context
    external = tmp_path / "external.json"
    external.write_text("unchanged", encoding="utf-8")
    import os
    os.link(external, store.directory / "discord-answer.json")
    report.claims = []
    with pytest.raises(ValueError, match="unsafe answer artifact"):
        await compose_answer(report, profile, budget, store)
    assert external.read_text("utf-8") == "unchanged"


async def test_executor_reserves_only_normal_job_budget_and_combines_request_counts(tmp_path, monkeypatch):
    from ephy_worker.config import WorkerConfig
    from ephy_worker.discord_adapter import DiscordSettings
    from ephy_worker.discord_cli import make_executor
    from ephy_worker.research import ResearchExecutor

    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture", output_mode="prompted_json")
    from ephy_worker.config import SearchConfig
    config = WorkerConfig(model_profiles={"fixture": profile}, search=SearchConfig(base_url="http://127.0.0.1:9"))
    settings = DiscordSettings(
        application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "out", state_dir=tmp_path / "state",
    )
    limits_seen = []
    async def execute(self, urls):
        limits_seen.append(self.config.limits)
        self.budget.counts["model_requests"] = 3
        fixture = fixture_report()
        self.report.sources, self.report.evidence, self.report.claims = fixture.sources, fixture.evidence, fixture.claims
    monkeypatch.setattr(ResearchExecutor, "_execute", execute)
    install_runner(monkeypatch)
    answer = await make_executor(settings, config)("public fixture")
    artifact = json.loads((settings.output_dir / answer.job_id / "discord-answer.json").read_text("utf-8"))
    report = json.loads((settings.output_dir / answer.job_id / "report.json").read_text("utf-8"))
    assert limits_seen[0].job_seconds == 275 and limits_seen[0].model_requests == 39
    assert artifact["aggregate_model_requests"] == 4 and artifact["model_request_limit"] == 40
    assert report["metrics"]["requests"]["model_requests"] == 3
    await make_executor(settings.model_copy(update={"timeout_seconds": 1}), config)("public fixture")
    assert limits_seen[-1] == config.limits


@pytest.mark.parametrize("stop", [
    "cancel", "deadline", "shutdown", "model_timeout_cleanup_failure", "timeout_cleanup_failure",
])
async def test_sdk_final_generation_stop_preserves_saved_job_in_ledger(tmp_path, monkeypatch, stop):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ephy_worker.config import SearchConfig, WorkerConfig
    from ephy_worker.discord_adapter import DiscordAdapter, DiscordSettings, Ledger
    from ephy_worker.discord_cli import build_client, make_executor
    from ephy_worker.research import ResearchExecutor

    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture", output_mode="prompted_json")
    config = WorkerConfig(model_profiles={"fixture": profile}, search=SearchConfig(base_url="http://127.0.0.1:9"))
    settings = DiscordSettings(
        entrypoint="slash", application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "out", state_dir=tmp_path / "state",
    )
    workers = []
    async def execute(self, urls):
        workers.append(self)
        fixture = fixture_report()
        self.report.sources, self.report.evidence, self.report.claims = fixture.sources, fixture.evidence, fixture.claims
    monkeypatch.setattr(ResearchExecutor, "_execute", execute)
    entered = asyncio.Event()
    combined_failure = stop.endswith("cleanup_failure")
    if combined_failure:
        primary = ModelError("model_timeout") if stop.startswith("model_") else TimeoutError("private timeout")
        observed = install_runner(monkeypatch, error=primary)
        original_run = module.ModelRunner.run
        async def run(self, *args, **kwargs):
            entered.set()
            return await original_run(self, *args, **kwargs)
        async def failed_close(self):
            observed["closed"] += 1
            raise RuntimeError("private cleanup failure")
        monkeypatch.setattr(module.ModelRunner, "run", run)
        monkeypatch.setattr(module.ModelRunner, "aclose", failed_close)
    else:
        observed = install_runner(monkeypatch, entered=entered)
    adapter = DiscordAdapter(settings, Ledger(settings), make_executor(settings, config))
    client, tree, guild = build_client(settings, adapter, "fixture-bot-token")
    def interaction(number):
        return SimpleNamespace(
            id=number, application_id=100, guild_id=200, channel_id=300,
            user=SimpleNamespace(id=400, bot=False), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
    original = interaction(500)
    task = asyncio.create_task(tree.get_command("ephy-research", guild=guild).callback(original, "public fixture"))
    closed = False
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if combined_failure:
            pass  # The synthetic model and cleanup have already failed，without an external stop request．
        elif stop == "deadline":
            adapter._deadline.reschedule(asyncio.get_running_loop().time())
        elif stop == "shutdown":
            await adapter.close()
            closed = True
        else:
            await tree.get_command("ephy-cancel", guild=guild).callback(interaction(501), "500")
        await asyncio.wait_for(task, 5)
        directory = workers[0].store.directory
        assert json.loads((directory / "report.json").read_text("utf-8"))["state"] == "completed"
        artifact = saved(workers[0].store)
        assert artifact["status"] == ("failed" if combined_failure else "not_generated")
        if combined_failure:
            assert artifact["reason"] == "generation_timeout" and artifact["cleanup_reason"] == "cleanup_failed"
        ledger = Ledger(settings) if closed else adapter.ledger
        try:
            expected = "failed" if combined_failure else "timed_out" if stop == "deadline" else "cancelled"
            assert ledger.db.execute("SELECT status,job FROM requests WHERE id=500").fetchone() == (
                expected, directory.name,
            )
        finally:
            if closed:
                ledger.close()
        messages = [call.args[0] for call in original.followup.send.await_args_list]
        assert len(messages) == (1 if stop == "shutdown" else 2)
        if stop == "deadline":
            assert messages[-1] == "もう少し調べるのに時間がかかりそうです．"
        elif combined_failure:
            assert messages[-1].startswith("もう少し調べるのに時間がかかりそうです．")
            assert "機能Aを利用できます" in messages[-1] and "private" not in messages[-1]
        assert observed["closed"] == 1
    finally:
        await client.close()
        if not closed:
            await adapter.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("generation", ["ok", "timeout", "model_timeout", "model_failure", "unexpected", "cancelled"])
@pytest.mark.parametrize("cleanup", ["ok", "timeout", "unexpected"])
async def test_generation_cleanup_outcome_matrix_keeps_primary_semantics(context, monkeypatch, generation, cleanup):
    report, profile, budget, store = context
    errors = {
        "ok": None, "timeout": TimeoutError("private timeout"),
        "model_timeout": ModelError("model_timeout"), "model_failure": ModelError("private model error"),
        "unexpected": RuntimeError("private primary error"), "cancelled": asyncio.CancelledError(),
    }
    primary = errors[generation]
    observed = install_runner(monkeypatch, error=primary)
    cleanup_error = RuntimeError("private cleanup error")
    async def close(self):
        observed["closed"] += 1
        if cleanup == "timeout":
            await asyncio.Future()
        if cleanup == "unexpected":
            raise cleanup_error
    monkeypatch.setattr(module.ModelRunner, "aclose", close)
    raised = None
    try:
        await compose_answer(report, profile, budget, store)
    except (RuntimeError, asyncio.CancelledError) as exc:
        raised = exc
    record = saved(store)
    assert observed["closed"] == 1 and "private" not in json.dumps(record)
    assert record["cleanup_reason"] == {"ok": None, "timeout": "cleanup_timeout", "unexpected": "cleanup_failed"}[cleanup]
    if generation == "cancelled":
        assert raised is primary and record["status"] == "not_generated" and record["reason"] == "cancelled"
        assert record["text"] == "調査を取り消しました．"
    elif generation == "unexpected":
        assert record["status"] == "failed" and record["reason"] == "unexpected_generation_failure"
        if cleanup == "timeout":
            assert isinstance(raised, AnswerTimeoutFailure) and raised.__cause__ is primary
            assert raised.text == record["text"] and record["text"].startswith("もう少し調べるのに時間がかかりそうです．")
        else:
            assert raised is primary
    elif generation in {"timeout", "model_timeout"}:
        assert record["reason"] == "generation_timeout"
        assert record["text"].startswith("もう少し調べるのに時間がかかりそうです．")
        if cleanup == "unexpected":
            assert isinstance(raised, AnswerTimeoutFailure) and raised.text == record["text"]
            assert record["status"] == "failed" and raised.__cause__ is cleanup_error
        else:
            assert raised is None and record["status"] == "fallback"
    elif cleanup == "unexpected":
        assert raised is cleanup_error and record["status"] == "failed"
        assert record["reason"] == ("cleanup_failed" if generation == "ok" else "generation_failed")
    elif generation == "model_failure":
        assert raised is None and record["status"] == "fallback" and record["reason"] == "generation_failed"
    else:
        assert raised is None and record["status"] == ("generated" if cleanup == "ok" else "fallback")
        assert record["reason"] == ("ok" if cleanup == "ok" else "cleanup_timeout")


async def test_physical_request_deadline_exhaustion_keeps_timeout_context(context, monkeypatch):
    report, profile, budget, store = context
    install_runner(monkeypatch, error=BudgetExceeded("job_timeout"))
    text = await compose_answer(report, profile, budget, store)
    assert text.startswith("もう少し調べるのに時間がかかりそうです．")
    assert saved(store)["reason"] == "time_budget_exhausted" and saved(store)["status"] == "fallback"


@pytest.mark.parametrize("storage", ["unsafe_link", "write_failure"])
@pytest.mark.parametrize("generation", ["ok", "unexpected"])
async def test_research_timeout_storage_failure_is_failed_and_keeps_original_cause(context, monkeypatch, tmp_path, storage, generation):
    report, profile, budget, store = context
    report.stop_reason, report.state = "job_timeout", "partial"
    primary = RuntimeError("private generation failure") if generation == "unexpected" else None
    install_runner(monkeypatch, error=primary)
    original = None
    external = tmp_path / "external-answer"
    if storage == "unsafe_link":
        import os
        external.write_text("unchanged", encoding="utf-8")
        os.link(external, store.directory / "discord-answer.json")
    else:
        original = OSError("private write failure")
        def fail_write(path, text):
            raise original
        monkeypatch.setattr(module, "atomic_write", fail_write)
    with pytest.raises(AnswerTimeoutFailure) as raised:
        await compose_answer(report, profile, budget, store)
    assert raised.value.text.startswith("もう少し調べるのに時間がかかりそうです．")
    if storage == "unsafe_link":
        assert isinstance(raised.value.__cause__, ValueError)
        assert str(raised.value.__cause__) == "unsafe answer artifact"
        assert external.read_text("utf-8") == "unchanged"
    else:
        assert raised.value.__cause__ is original and not (store.directory / "discord-answer.json").exists()
    if primary is not None:
        assert raised.value.__cause__.__cause__ is primary
    assert "private" not in raised.value.text


async def test_cancellation_stays_primary_when_timeout_context_and_storage_failure_coincide(context, monkeypatch):
    report, profile, budget, store = context
    report.stop_reason, report.state = "job_timeout", "partial"
    cancellation, storage = asyncio.CancelledError(), OSError("private storage failure")
    install_runner(monkeypatch, error=cancellation)
    def fail_write(path, text):
        raise storage
    monkeypatch.setattr(module, "atomic_write", fail_write)
    with pytest.raises(asyncio.CancelledError) as raised:
        await compose_answer(report, profile, budget, store)
    assert raised.value is cancellation and raised.value.__cause__ is storage
    assert not (store.directory / "discord-answer.json").exists()


@pytest.mark.parametrize("entrypoint", ["slash", "mention"])
@pytest.mark.parametrize("generation", ["ok", "model_failure", "unexpected"])
@pytest.mark.parametrize("cleanup", ["ok", "timeout", "unexpected"])
async def test_research_timeout_context_survives_full_sdk_failure_matrix(
    tmp_path, monkeypatch, entrypoint, generation, cleanup
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord

    from ephy_worker.config import SearchConfig, WorkerConfig
    from ephy_worker.discord_adapter import DiscordAdapter, DiscordSettings, Ledger
    from ephy_worker.discord_cli import build_client, make_executor
    from ephy_worker.research import ResearchExecutor

    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture", output_mode="prompted_json")
    config = WorkerConfig(model_profiles={"fixture": profile}, search=SearchConfig(base_url="http://127.0.0.1:9"))
    settings = DiscordSettings(
        entrypoint=entrypoint, application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "out", state_dir=tmp_path / "state",
    )
    workers = []
    async def research(self, urls):
        workers.append(self)
        fixture = fixture_report()
        self.report.sources, self.report.evidence, self.report.claims = fixture.sources, fixture.evidence, fixture.claims
        raise TimeoutError("private research timeout")
    monkeypatch.setattr(ResearchExecutor, "_execute", research)
    errors = {"ok": None, "model_failure": ModelError("private model error"), "unexpected": RuntimeError("private program error")}
    observed = install_runner(monkeypatch, error=errors[generation])
    async def close(self):
        observed["closed"] += 1
        if cleanup == "timeout":
            await asyncio.Future()
        if cleanup == "unexpected":
            raise RuntimeError("private cleanup error")
    monkeypatch.setattr(module.ModelRunner, "aclose", close)
    adapter = DiscordAdapter(settings, Ledger(settings), make_executor(settings, config))
    client, tree, guild = build_client(settings, adapter, "fixture-bot-token")
    try:
        if entrypoint == "slash":
            item = SimpleNamespace(
                id=500, application_id=100, guild_id=200, channel_id=300, user=SimpleNamespace(id=400, bot=False),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            command = tree.get_command("ephy-research", guild=guild)
            await command.callback(item, "public fixture")
            sends = item.followup.send.await_args_list
            item.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        else:
            client._connection.user = SimpleNamespace(id=900, bot=True)
            item = SimpleNamespace(
                id=500, content="<@900> public fixture", guild=SimpleNamespace(id=200),
                channel=SimpleNamespace(id=300, send=AsyncMock()), author=SimpleNamespace(id=400, bot=False),
                webhook_id=None, type=discord.MessageType.default, reference=None, mention_everyone=False,
                mentions=[SimpleNamespace(id=900)],
            )
            await client.on_message(item)
            sends = item.channel.send.await_args_list
        messages = [call.args[0] for call in sends]
        directory = workers[0].store.directory
        report = json.loads((directory / "report.json").read_text("utf-8"))
        artifact = saved(workers[0].store)
        failed = generation == "unexpected" or cleanup == "unexpected"
        expected_state = "failed" if failed else "partial"
        assert adapter.ledger.db.execute("SELECT status,job FROM requests WHERE id=500").fetchone() == (expected_state, directory.name)
        assert report["state"] == "partial" and report["stop_reason"] == "job_timeout" and report["finished_at"]
        assert report["metrics"]["requests"]["model_requests"] == 0 and artifact["model_requests"] == 1
        assert artifact["status"] == ("failed" if failed else "generated" if generation == "ok" and cleanup == "ok" else "fallback")
        expected_reason = (
            "unexpected_generation_failure" if generation == "unexpected" else "generation_failed"
            if generation == "model_failure" else "ok" if cleanup == "ok" else "cleanup_" + ("timeout" if cleanup == "timeout" else "failed")
        )
        assert artifact["reason"] == expected_reason
        assert artifact["cleanup_reason"] == {"ok": None, "timeout": "cleanup_timeout", "unexpected": "cleanup_failed"}[cleanup]
        assert len(messages) == 2 and messages[-1] == artifact["text"]
        assert messages[-1].startswith("もう少し調べるのに時間がかかりそうです．")
        assert "機能Aを利用できます" in messages[-1] and "標準設定の場合" in messages[-1]
        assert "private" not in json.dumps(artifact) + "".join(messages)
        assert observed["closed"] == 1 and len(observed["prompts"]) == 1
        if entrypoint == "mention":
            assert messages[0] == "分かりました，調べてみますね．"
        for call in sends:
            assert call.kwargs["allowed_mentions"].to_dict()["parse"] == [] and call.kwargs["suppress_embeds"]
    finally:
        await client.close()
        await adapter.close()


def acquired_progress_report(count=1, *, body="資料に記載された機能の紹介です。", kind="html"):
    report = fixture_report()
    report.state, report.stop_reason, report.answerability = "failed", "job_timeout", "unresolved"
    report.claims, report.evidence, report.sources = [], [], []
    for index in range(1, count + 1):
        identity = f"s{index}"
        report.sources.append(Source(
            source_id=identity, requested_url=f"https://example.org/doc/{index}",
            status="ok", kind=kind, http_status=200, bytes_received=len(body.encode("utf-8")) + 100,
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(), extractor="synthetic/1",
            page_count=3 if kind == "pdf" else None, read_passage_ids=[],
            passages=[Passage(passage_id=f"{identity}-body", source_id=identity, text=body,
                              start=0, end=len(body), page=2 if kind == "pdf" else None)],
        ))
    return report


async def test_timeout_zero_claims_and_zero_read_ids_returns_acquired_body_without_model(context, monkeypatch):
    _, profile, budget, store = context
    report = acquired_progress_report(5).model_copy(update={"job_id": store.job_id})
    original = report.model_dump_json()
    store.save(report)
    canonical = {name: (store.directory / name).read_bytes() for name in ("report.json", "report.md", "metrics.json", "artifacts.json")}
    budget.started -= 60  # Existing body remains available after the request/time budget ends．
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert text.startswith("もう少し調べるのに時間がかかりそうです．")
    assert "取得した資料の途中情報" in text and "内容の照合は未完了" in text and "結論はまだ確認できていません" in text
    assert text.count("資料に記載された機能の紹介です。") == 5
    assert text.count("https://example.org/doc/") == 5
    assert report.model_dump_json() == original and all(not source.read_passage_ids for source in report.sources)
    assert all((store.directory / name).read_bytes() == value for name, value in canonical.items())
    record = saved(store)
    assert record["status"] == "fallback" and record["reason"] == "no_verified_facts" and record["verified_facts"] == 0
    assert record["partial_progress"] == {"stage": "acquired_document_unchecked", "excerpt_count": 5, "available": True}
    assert record["model_requests"] == 0 and not observed["profiles"]


@pytest.mark.parametrize("fault", [
    "missing_hash", "malformed_hash", "no_bytes", "no_extractor", "failed_fetch", "wrong_kind", "http_error",
    "duplicate_source", "duplicate_passage", "wrong_owner", "bad_span", "negative_start",
    "private_url", "credential_url", "non_http_url", "encoded_secret_url", "thinking", "channel",
    "html", "code", "path", "unix_path", "unc_path", "secret_body", "unknown_body", "html_page",
    "pdf_missing_page", "pdf_wrong_page",
])
async def test_unreviewed_progress_excludes_unsafe_or_unproven_material(context, monkeypatch, fault):
    _, profile, budget, store = context
    report = acquired_progress_report()
    source = report.sources[0]
    passage = source.passages[0]
    secret = "known_fixture_partial_secret"
    if fault == "missing_hash":
        source.body_hash = None
    elif fault == "malformed_hash":
        source.body_hash = "unproven"
    elif fault == "no_bytes":
        source.bytes_received = 0
    elif fault == "no_extractor":
        source.extractor = None
    elif fault == "failed_fetch":
        source.status = "extraction_failed"
    elif fault == "wrong_kind":
        source.kind = "unknown"
    elif fault == "http_error":
        source.http_status = 403
    elif fault == "duplicate_source":
        report.sources.append(source.model_copy(deep=True))
    elif fault == "duplicate_passage":
        source.passages.append(passage.model_copy())
    elif fault == "wrong_owner":
        passage.source_id = "other"
    elif fault == "bad_span":
        passage.end += 1
    elif fault == "negative_start":
        passage.start = -1
        passage.end -= 1
    elif fault == "private_url":
        source.requested_url = "http://127.0.0.1/private"
    elif fault == "credential_url":
        source.requested_url = "https://name:password@example.org/private"
    elif fault == "non_http_url":
        source.requested_url = "file:///private"
    elif fault == "encoded_secret_url":
        source.requested_url += "/" + "".join(f"%{ord(char):02x}" for char in secret)
    elif fault == "html_page":
        passage.page = 1
    elif fault.startswith("pdf_"):
        source.kind, source.page_count = "pdf", 3
        passage.page = None if fault == "pdf_missing_page" else 4
    else:
        passage.text = {
            "thinking": "<think>private internal thought", "channel": "<|analysis internal thought",
            "html": "<script>untrusted</script>", "code": "```powershell untrusted```",
            "path": "C:\\private\\document", "unix_path": "/home/private/document",
            "unc_path": "\\\\private\\document", "secret_body": "x" * 170 + secret,
            "unknown_body": "   ",
        }[fault]
        passage.end = passage.start + len(passage.text)
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store, known_secrets=(secret,))
    assert text == "もう少し調べるのに時間がかかりそうです．"
    assert saved(store)["partial_progress"]["excerpt_count"] == 0 and not observed["profiles"]
    assert "private" not in text and secret not in json.dumps(saved(store))


async def test_partial_progress_bounds_deduplicates_and_uses_physical_pdf_page(context, monkeypatch):
    _, profile, budget, store = context
    report = acquired_progress_report(7, body="😀" * 300)
    report.sources[1].requested_url = report.sources[0].requested_url
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert saved(store)["partial_progress"]["excerpt_count"] == 5
    assert text.count("https://example.org/doc/1") == 1 and "doc/7" not in text
    excerpts = [line[2:] for line in text.splitlines() if line.startswith("> ")]
    assert len(excerpts) == 5 and all(len(excerpt) == 180 and excerpt.endswith("…") for excerpt in excerpts)
    assert len(text.encode("utf-16-le")) // 2 <= 7500 and not observed["profiles"]
    report = acquired_progress_report(kind="pdf")
    report.sources[0].status, report.sources[0].http_status = "partial", 206
    text = await compose_answer(report, profile, budget, store)
    assert "物理p.2" in text and "#page=2" in text and saved(store)["partial_progress"]["excerpt_count"] == 1


async def test_unreviewed_progress_keeps_complete_reply_within_utf16_cap(context, monkeypatch):
    _, profile, budget, store = context
    report = acquired_progress_report(5, body="&" * 300)
    for source in report.sources:
        source.requested_url += "?q=" + "x" * 420
    install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    from ephy_worker.discord_adapter import split_reply
    assert saved(store)["partial_progress"]["excerpt_count"] == 5
    assert len(text.encode("utf-16-le")) // 2 <= 7500
    assert "".join(split_reply(text)) == text and "出力上限" not in text


@pytest.mark.parametrize("state", ["no_timeout", "cancelled", "no_body"])
async def test_partial_progress_does_not_change_non_timeout_cancel_or_no_body(context, monkeypatch, state):
    _, profile, budget, store = context
    report = acquired_progress_report()
    if state == "no_timeout":
        report.stop_reason = "no_verified_claims"
    elif state == "cancelled":
        report.state = "cancelled"
    else:
        report.sources[0].passages = []
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert "資料に記載された" not in text and "https://example.org" not in text
    assert saved(store)["partial_progress"]["excerpt_count"] == 0 and not observed["profiles"]
    if state == "cancelled":
        assert text == "調査を取り消しました．"


async def test_verified_answer_path_does_not_include_unreviewed_source_excerpts(context, monkeypatch):
    report, profile, budget, store = context
    report.stop_reason = "job_timeout"
    report.sources.append(acquired_progress_report(2).sources[1])
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert "機能Aを利用できます" in text and "内容の照合は未完了" not in text
    assert saved(store)["partial_progress"]["excerpt_count"] == 0 and len(observed["profiles"]) == 1


async def test_progress_uses_next_safe_passage_and_suppresses_pending_cancel(context, monkeypatch):
    _, profile, budget, store = context
    report = acquired_progress_report(body="<think>untrusted reasoning")
    source = report.sources[0]
    safe = "本文の別の安全な記載です。"
    source.passages.append(Passage(passage_id="s1-next", source_id="s1", text=safe, start=30, end=30 + len(safe)))
    source.body_hash = hashlib.sha256((source.passages[0].text.ljust(30) + safe).encode("utf-8")).hexdigest()
    observed = install_runner(monkeypatch)
    text = await compose_answer(report, profile, budget, store)
    assert safe in text and "untrusted" not in text and saved(store)["partial_progress"]["excerpt_count"] == 1
    async def pending():
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        return await compose_answer(report, profile, budget, store)
    assert await asyncio.create_task(pending()) == "調査を取り消しました．"
    assert saved(store)["partial_progress"]["excerpt_count"] == 0 and not observed["profiles"]


@pytest.mark.parametrize("entrypoint", ["slash", "mention"])
async def test_sdk_timeout_with_acquired_body_sends_unchecked_progress_with_zero_model_calls(tmp_path, monkeypatch, entrypoint):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord

    from ephy_worker.config import SearchConfig, WorkerConfig
    from ephy_worker.discord_adapter import DiscordAdapter, DiscordSettings, Ledger
    from ephy_worker.discord_cli import build_client, make_executor
    from ephy_worker.research import ResearchExecutor

    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture", output_mode="prompted_json")
    config = WorkerConfig(model_profiles={"fixture": profile}, search=SearchConfig(base_url="http://127.0.0.1:9"))
    settings = DiscordSettings(
        entrypoint=entrypoint, application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "out", state_dir=tmp_path / "state",
    )
    workers = []
    async def research(self, urls):
        workers.append(self)
        self.report.sources = acquired_progress_report(5).sources
        raise TimeoutError("private research timeout")
    monkeypatch.setattr(ResearchExecutor, "_execute", research)
    observed = install_runner(monkeypatch)
    adapter = DiscordAdapter(settings, Ledger(settings), make_executor(settings, config))
    client, tree, guild = build_client(settings, adapter, "fixture-bot-token")
    try:
        if entrypoint == "slash":
            item = SimpleNamespace(
                id=500, application_id=100, guild_id=200, channel_id=300, user=SimpleNamespace(id=400, bot=False),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
            )
            await tree.get_command("ephy-research", guild=guild).callback(item, "public fixture")
            sends = item.followup.send.await_args_list
        else:
            client._connection.user = SimpleNamespace(id=900, bot=True)
            item = SimpleNamespace(
                id=500, content="<@900> public fixture", guild=SimpleNamespace(id=200),
                channel=SimpleNamespace(id=300, send=AsyncMock()), author=SimpleNamespace(id=400, bot=False),
                webhook_id=None, type=discord.MessageType.default, reference=None, mention_everyone=False,
                mentions=[SimpleNamespace(id=900)],
            )
            await client.on_message(item)
            sends = item.channel.send.await_args_list
        messages = [call.args[0] for call in sends]
        directory = workers[0].store.directory
        report = json.loads((directory / "report.json").read_text("utf-8"))
        record = saved(workers[0].store)
        assert len(messages) == 2 and messages[-1] == record["text"]
        assert messages[-1].startswith("もう少し調べるのに時間がかかりそうです．")
        assert "内容の照合は未完了" in messages[-1] and "資料に記載された" in messages[-1]
        assert report["claims"] == [] and all(not source["read_passage_ids"] for source in report["sources"])
        assert report["stop_reason"] == "job_timeout" and report["state"] == "failed"
        assert report["metrics"]["requests"]["model_requests"] == record["model_requests"] == 0
        assert record["partial_progress"]["excerpt_count"] == 5 and not observed["profiles"]
        assert adapter.ledger.db.execute("SELECT status,job FROM requests WHERE id=500").fetchone() == ("failed", directory.name)
        for call in sends:
            assert call.kwargs["allowed_mentions"].to_dict()["parse"] == [] and call.kwargs["suppress_embeds"]
    finally:
        await client.close()
        await adapter.close()
