"""Offline response ordering and grounded opinions，without live model or Discord calls．"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from ephy_worker import discord_adapter as adapter_module
from ephy_worker import discord_answer as answer_module
from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import ModelProfile, SearchConfig, WorkerConfig
from ephy_worker.discord_adapter import Answer, DiscordAdapter, DiscordSettings, Ledger, Request
from ephy_worker.discord_cli import build_mention_client, make_executor
from ephy_worker.schema import Candidate, Claim, Evidence, Limits, Passage, Report, Source
from ephy_worker.store import JobStore


@pytest.fixture
def settings(tmp_path):
    return DiscordSettings(
        application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "unused.yaml", profile="fixture", output_dir=tmp_path / "results",
        state_dir=tmp_path / "state", timeout_seconds=1,
    )


@pytest.fixture
def short_progress(monkeypatch):
    assert adapter_module._PROGRESS_NOTICE_SECONDS == 120.0
    monkeypatch.setattr(adapter_module, "_PROGRESS_NOTICE_SECONDS", 0.02)


def request(number=500, user=400):
    return Request(number, 100, 200, 300, user)


class Reply:
    def __init__(self):
        self.messages = []
        self.rejected = []
        self.progress = asyncio.Event()

    async def defer(self):
        pass

    async def send(self, text):
        self.messages.append(text)
        if "調査を続けています" in text:
            self.progress.set()

    async def reject(self, text):
        self.rejected.append(text)


def pending_progress():
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("ephy-discord-progress-")]


async def test_fast_completion_and_later_request_have_no_progress(settings, short_progress):
    calls = []

    async def execute(question):
        calls.append(question)
        return Answer("completed", "最終結果です．")

    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    try:
        for number in (500, 501):
            reply = Reply()
            await adapter.ask(request(number), reply, "公開情報")
            assert reply.messages == ["分かりました，調べてみますね．", "最終結果です．"]
        await asyncio.sleep(0.05)
        assert calls == ["公開情報", "公開情報"] and not pending_progress()
    finally:
        await adapter.close()


async def test_notice_starts_after_successful_receipt_and_stops_before_final_delivery(settings, short_progress):
    receipt_entered, receipt_release = asyncio.Event(), asyncio.Event()
    final_entered, final_release = asyncio.Event(), asyncio.Event()
    calls = 0

    class SlowDelivery(Reply):
        async def send(self, text):
            if text == "分かりました，調べてみますね．":
                receipt_entered.set()
                await receipt_release.wait()
            elif text == "最終結果":
                final_entered.set()
                await final_release.wait()
            await super().send(text)

    async def execute(question):
        nonlocal calls
        calls += 1
        return Answer("completed", "最終結果")

    reply = SlowDelivery()
    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    work = asyncio.create_task(adapter.ask(request(), reply, "公開情報"))
    try:
        await receipt_entered.wait()
        await asyncio.sleep(0.05)
        assert not reply.messages and calls == 0 and not pending_progress()
        receipt_release.set()
        await final_entered.wait()
        await asyncio.sleep(0.05)
        assert reply.messages == ["分かりました，調べてみますね．"]
        assert calls == 1 and not pending_progress()
        final_release.set()
        await work
        assert reply.messages[-1] == "最終結果"
    finally:
        receipt_release.set()
        final_release.set()
        await work
        await adapter.close()


@pytest.mark.parametrize("state", ["partial", "timed_out", "failed", "completed"])
async def test_notice_once_then_terminal_findings_preserving_exact_body(settings, short_progress, state):
    reply, release = Reply(), asyncio.Event()
    legacy = "もう少し調べるのに時間がかかりそうです．"
    finding = "断定できませんが，資料には条件Aで使えるという説明があります．\n\n" + legacy
    original = Answer(state, legacy + "\n\n" + finding, "research-fixture")
    calls = 0

    async def execute(question):
        nonlocal calls
        calls += 1
        await release.wait()
        return original

    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    work = asyncio.create_task(adapter.ask(request(), reply, "公開情報"))
    try:
        await asyncio.wait_for(reply.progress.wait(), 1)
        await asyncio.sleep(0.05)
        assert not work.done() and calls == 1 and len(reply.messages) == 2
        release.set()
        await work
        assert reply.messages[-1] == "今わかった範囲でお伝えしますね．\n\n" + finding
        assert original.text == legacy + "\n\n" + finding
        assert ledger.db.execute("SELECT status, job FROM requests").fetchone() == (state, "research-fixture")
        assert not pending_progress()
    finally:
        release.set()
        await work
        await adapter.close()


async def test_real_executor_no_checked_claim_timeout_delivers_terminal_preview(settings, short_progress, monkeypatch):
    from test_research import FakeFetcher, FakeModel, FakeSearch

    from ephy_worker.research import ResearchExecutor

    reply = Reply()
    snippet = "機能Aには公開版の標準設定で利用できるという説明があります．"

    async def timed_out_research(self, urls):
        await reply.progress.wait()
        self.report.queries.append({"query_id": "q1", "status": "ok"})
        self.report.candidates.append(Candidate(
            url="https://example.org/manual", snippet=snippet, query_id="q1", engine="fixture", rank=1,
        ))
        raise BudgetExceeded("job_timeout")

    monkeypatch.setattr(ResearchExecutor, "_execute", timed_out_research)
    monkeypatch.setattr("ephy_worker.models.ModelRunner", lambda *args: FakeModel())
    monkeypatch.setattr("ephy_worker.search.create_search_provider", lambda *args: FakeSearch())
    monkeypatch.setattr("ephy_worker.fetch.PublicFetcher", lambda *args: FakeFetcher())
    no_generation = AsyncMock(side_effect=AssertionError("Unverified preview must not generate opinions"))
    monkeypatch.setattr(answer_module, "ModelRunner", no_generation)
    config = WorkerConfig(
        model_profiles={"fixture": ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1",
                                                model_id="fixture", output_mode="prompted_json")},
        search=SearchConfig(base_url="http://127.0.0.1:9"), limits=Limits(job_seconds=30),
    )
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, make_executor(settings, config))
    try:
        await adapter.ask(request(), reply, "公開情報")
        assert len(reply.messages) == 3 and reply.messages[-1].startswith("今わかった範囲でお伝えしますね．")
        assert snippet in reply.messages[-1] and "リンク先の本文はまだ確認していません" in reply.messages[-1]
        assert "ephyとしては，" not in reply.messages[-1]
        state, job = ledger.db.execute("SELECT status, job FROM requests").fetchone()
        assert state == "failed"
        canonical = json.loads((settings.output_dir / job / "report.json").read_text(encoding="utf-8"))
        assert canonical["stop_reason"] == "job_timeout" and canonical["claims"] == []
        artifact = json.loads((settings.output_dir / job / "discord-answer.json").read_text(encoding="utf-8"))
        assert artifact["text"].startswith("もう少し調べるのに時間がかかりそうです．")
        assert snippet in artifact["text"] and artifact["partial_progress"]["stage"] == "search_preview_unchecked"
        no_generation.assert_not_called()
        assert not pending_progress()
    finally:
        await adapter.close()


async def test_progress_does_not_extend_original_deadline_and_empty_material_is_honest(settings, short_progress):
    reply = Reply()

    async def execute(question):
        await asyncio.Future()

    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    started = time.monotonic()
    try:
        await adapter.ask(request(), reply, "公開情報")
        assert time.monotonic() - started < 1.7
        assert len(reply.messages) == 3
        assert reply.messages[-1] == (
            "今わかった範囲でお伝えしますね．\n\n今回の調査では，紹介できる情報をまだ取得できませんでした．"
        )
        assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("timed_out",)
        assert not pending_progress()
    finally:
        await adapter.close()


@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
async def test_cancel_and_shutdown_stop_notice_and_preserve_cancel_priority(settings, short_progress, stop):
    reply, started, cleanup = Reply(), asyncio.Event(), asyncio.Event()

    async def execute(question):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup.set()

    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    work = asyncio.create_task(adapter.ask(request(), reply, "公開情報"))
    await started.wait()
    if stop == "cancel":
        await adapter.cancel(request(501), Reply(), "500")
    else:
        await adapter.close()
    await work
    await asyncio.sleep(0.05)
    assert cleanup.is_set() and not reply.progress.is_set() and not pending_progress()
    if stop == "cancel":
        assert reply.messages == ["分かりました，調べてみますね．", "調査を取り消しました．"]
        assert ledger.db.execute("SELECT status FROM requests WHERE id=500").fetchone() == ("cancelled",)
    else:
        assert reply.messages == ["分かりました，調べてみますね．"]
    await adapter.close()


async def test_cancel_after_notice_never_sends_partial_finding(settings, short_progress):
    reply = Reply()

    async def execute(question):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return Answer("partial", "この結果は取消後に出さない．")

    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    work = asyncio.create_task(adapter.ask(request(), reply, "公開情報"))
    try:
        await asyncio.wait_for(reply.progress.wait(), 1)
        await adapter.cancel(request(501), Reply(), "500")
        await work
        assert len(reply.messages) == 3 and reply.messages[-1] == "調査を取り消しました．"
        assert not pending_progress()
    finally:
        await adapter.close()


async def test_rejected_busy_duplicate_and_failed_receipt_never_start_notice(settings, short_progress):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def execute(question):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return Answer("completed", "最終結果")

    class FailedReceipt(Reply):
        async def send(self, text):
            raise RuntimeError("private-failure-marker")

    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    reply = Reply()
    try:
        denied, invalid, failed = Reply(), Reply(), FailedReceipt()
        await adapter.ask(request(490, user=401), denied, "公開情報")
        await adapter.ask(request(491), invalid, "")
        await adapter.ask(request(492), failed, "公開情報")
        assert not pending_progress() and calls == 0
        work = asyncio.create_task(adapter.ask(request(), reply, "公開情報"))
        await entered.wait()
        busy, duplicate = Reply(), Reply()
        await adapter.ask(request(501), busy, "別の情報")
        await adapter.ask(request(), duplicate, "同じ情報")
        assert busy.rejected and not busy.messages and not duplicate.messages
        release.set()
        await work
        await asyncio.sleep(0.05)
        assert len(reply.messages) == 2 and calls == 1 and not pending_progress()
    finally:
        release.set()
        await adapter.close()


async def test_progress_send_failure_is_sanitized_not_retried_and_final_survives(settings, short_progress, caplog):
    attempted = asyncio.Event()

    class FailedProgress(Reply):
        attempts = 0

        async def send(self, text):
            if "調査を続けています" in text:
                self.attempts += 1
                attempted.set()
                raise RuntimeError("private-failure-marker")
            await super().send(text)

    async def execute(question):
        await attempted.wait()
        await asyncio.sleep(0.05)
        return Answer("completed", "最終結果です．")

    reply = FailedProgress()
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    try:
        await adapter.ask(request(), reply, "公開情報")
        assert reply.attempts == 1 and reply.messages[-1] == "最終結果です．"
        assert "discord_progress_delivery_failed" in caplog.text and "private-failure-marker" not in caplog.text
        assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("completed",)
        assert not pending_progress()
    finally:
        await adapter.close()


async def test_inflight_notice_is_joined_before_final_send(settings, short_progress):
    progress_started, progress_stopped = asyncio.Event(), asyncio.Event()

    class SlowProgress(Reply):
        async def send(self, text):
            if "調査を続けています" in text:
                progress_started.set()
                try:
                    await asyncio.Future()
                finally:
                    progress_stopped.set()
            else:
                if text == "最終結果":
                    assert progress_stopped.is_set()
                await super().send(text)

    async def execute(question):
        await progress_started.wait()
        return Answer("completed", "最終結果")

    reply = SlowProgress()
    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    try:
        await adapter.ask(request(), reply, "公開情報")
        assert reply.messages == ["分かりました，調べてみますね．", "最終結果"]
        assert not pending_progress()
    finally:
        await adapter.close()


async def test_executor_error_after_notice_does_not_leave_timer(settings, short_progress):
    reply = Reply()

    async def execute(question):
        await reply.progress.wait()
        raise RuntimeError("private-failure-marker")

    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    try:
        await adapter.ask(request(), reply, "公開情報")
        assert len(reply.messages) == 3 and "問題が起きた" in reply.messages[-1]
        assert "private-failure-marker" not in repr(reply.messages) and not pending_progress()
    finally:
        await adapter.close()


async def test_real_sdk_mention_uses_three_responses(settings, short_progress):
    sent, progress = [], asyncio.Event()

    async def send(text, **kwargs):
        assert kwargs["allowed_mentions"].everyone is False
        sent.append(text)
        if "調査を続けています" in text:
            progress.set()

    async def execute(question):
        assert question == "公開情報"
        await progress.wait()
        return Answer("completed", "調べてみたところ，公開資料に説明があります．")

    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    client, _, _ = build_mention_client(settings, adapter, "fixture-token")
    client._connection.user = SimpleNamespace(id=900, bot=True)
    message = SimpleNamespace(
        id=500, guild=SimpleNamespace(id=200), channel=SimpleNamespace(id=300, send=send),
        author=SimpleNamespace(id=400, bot=False), webhook_id=None, type=discord.MessageType.default,
        reference=None, mention_everyone=False, mentions=[SimpleNamespace(id=900)], content="<@900> 公開情報",
    )
    try:
        await client.on_message(message)
        assert len(sent) == 3 and sent[0] == "分かりました，調べてみますね．"
        assert sent[-1].startswith("調べてみたところ，") and not pending_progress()
    finally:
        await client.close()
        await adapter.close()


@pytest.fixture
def answer_context(tmp_path):
    quote = "機能Aは公開版の標準設定で利用できます．"
    store = JobStore(tmp_path / "answers")
    report = Report(
        job_id=store.job_id, question="機能Aについてどう思う？", profile_id="fixture",
        state="completed", answerability="sufficient",
        sources=[Source(source_id="s1", requested_url="https://example.org/manual", kind="html", status="ok",
                        read_passage_ids=["p1"], passages=[Passage(passage_id="p1", source_id="s1", text=quote,
                                                               start=0, end=len(quote))])],
        evidence=[Evidence(evidence_id="e1", source_id="s1", passage_id="p1", quote=quote,
                           quote_start=0, quote_end=len(quote), checked=True, relation="supports", conditions_match=True)],
        claims=[Claim(claim_id="c1", text="機能Aを利用できます．", checked=True, status="supported_primary",
                      evidence_ids=["e1"], conditions="公開版の標準設定", uncertainty=["旧版の対応"])],
    )
    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture",
                           output_mode="prompted_json", max_output_tokens=4096)
    return report, profile, Budget(Limits(job_seconds=30, model_requests=3)), store


def install_answer_runner(monkeypatch, payload):
    observed = []

    class Runner:
        def __init__(self, profile, budget):
            self.budget = budget
            assert profile.max_output_tokens <= 1536

        async def run(self, output_type, prompt, *, stage):
            self.budget.take("model_requests")
            observed.append((stage, prompt))
            return output_type.model_validate(payload)

        async def aclose(self):
            pass

    monkeypatch.setattr(answer_module, "ModelRunner", Runner)
    return observed


def answer_payload():
    return {
        "text": "機能Aを利用できます．", "claim_ids": ["c1"],
        "opinion": "まずは小さく試して，目的に合うか確かめるのがよいと思います．",
        "opinion_claim_ids": ["c1"],
    }


async def test_grounded_opinion_same_call_label_conditions_partial_and_untrusted_context(answer_context, monkeypatch):
    report, profile, budget, store = answer_context
    report.state, report.answerability = "partial", "limited"
    payload = answer_payload()
    observed = install_answer_runner(monkeypatch, payload)
    previous = "過去の回答では別の案を紹介しました．"
    before = report.model_dump_json()
    text = await answer_module.compose_answer(report, profile, budget, store, previous_answer=previous)
    assert "\n\nephyとしては，" + payload["opinion"] in text
    assert "公開版の標準設定" in text and "旧版の対応" in text and "一部まで" in text
    assert "https://example.org/manual" in text and "c1" not in text
    assert budget.counts["model_requests"] == 1 and len(observed) == 1
    prompt = observed[0][1]
    assert "新しい事実を足さず" in prompt and "個人的な体験" in prompt and "今回のfacts" in prompt
    material = json.loads(prompt[prompt.index('{"question"'):])
    assert material["previous_answer"] == {"text": previous, "status": "untrusted_conversation_context"}
    assert previous not in json.dumps(material["facts"], ensure_ascii=False)
    assert report.model_dump_json() == before
    saved = json.loads((store.directory / "discord-answer.json").read_text(encoding="utf-8"))
    assert saved["opinion"]["status"] == "generated" and saved["reason"] == "ok"


@pytest.mark.parametrize("opinion,basis", [
    ("根拠なしの提案です．", []), ("未知の根拠です．", ["unknown"]),
    ("重複した根拠です．", ["c1", "c1"]), ("", ["c1"]), ("   ", ["c1"]),
    ("<think>private reasoning</think>", ["c1"]), ("https://example.org/new", ["c1"]),
    ("analysis: private reasoning", ["c1"]), ("c1に基づく見方です．", ["c1"]),
    ("e1に基づく見方です．", ["c1"]), ("危険\x00な文字", ["c1"]),
    ("fixture-secret", ["c1"]), ("fixture&#45;secret", ["c1"]),
    ("tvly-" + "A" * 20, ["c1"]),
])
async def test_unsafe_or_ungrounded_opinion_falls_back_with_safe_reason(answer_context, monkeypatch, opinion, basis):
    report, profile, budget, store = answer_context
    payload = answer_payload()
    payload.update(opinion=opinion, opinion_claim_ids=basis)
    observed = install_answer_runner(monkeypatch, payload)
    text = await answer_module.compose_answer(report, profile, budget, store, known_secrets=("fixture-secret",))
    assert "機能Aを利用できます" in text and "ephyとしては，" not in text
    assert "公開版の標準設定" in text and "旧版の対応" in text
    assert budget.counts["model_requests"] == 1 and len(observed) == 1
    saved = json.loads((store.directory / "discord-answer.json").read_text(encoding="utf-8"))
    assert saved["status"] == "fallback" and saved["reason"] == "invalid_opinion"
    assert saved["opinion"]["status"] == "rejected"
    assert "fixture-secret" not in json.dumps(saved, ensure_ascii=False)


async def test_opinion_basis_must_be_selected_fact_not_other_verified_fact(answer_context, monkeypatch):
    report, profile, budget, store = answer_context
    other = report.claims[0].model_copy(update={"claim_id": "c2", "text": "別の機能説明です．"})
    report.claims.append(other)
    payload = answer_payload()
    payload["opinion_claim_ids"] = ["c2"]
    install_answer_runner(monkeypatch, payload)
    text = await answer_module.compose_answer(report, profile, budget, store)
    assert "ephyとしては，" not in text and "別の機能説明" in text
    saved = json.loads((store.directory / "discord-answer.json").read_text(encoding="utf-8"))
    assert saved["reason"] == "invalid_opinion"


@pytest.mark.parametrize("restriction", ["no_facts", "no_budget", "no_time"])
async def test_missing_evidence_or_budget_never_fabricates_opinion(answer_context, monkeypatch, restriction):
    report, profile, budget, store = answer_context
    if restriction == "no_facts":
        report.claims[0].checked = False
    elif restriction == "no_budget":
        budget.counts["model_requests"] = budget.limits.model_requests
    else:
        budget.started -= budget.limits.job_seconds
    runner = AsyncMock(side_effect=AssertionError("No generation budget or verified facts"))
    monkeypatch.setattr(answer_module, "ModelRunner", runner)
    text = await answer_module.compose_answer(report, profile, budget, store)
    assert "ephyとしては，" not in text
    runner.assert_not_called()


async def test_default_empty_opinion_remains_backward_compatible(answer_context, monkeypatch):
    report, profile, budget, store = answer_context
    payload = {"text": "機能Aを利用できます．", "claim_ids": ["c1"]}
    install_answer_runner(monkeypatch, payload)
    draft = answer_module.AnswerDraft(**payload)
    assert draft.opinion == "" and draft.opinion_claim_ids == []
    assert answer_module.AnswerDraft(**payload).opinion_claim_ids is not draft.opinion_claim_ids
    text = await answer_module.compose_answer(report, profile, budget, store)
    assert "ephyとしては，" not in text
    saved = json.loads((store.directory / "discord-answer.json").read_text(encoding="utf-8"))
    assert saved["status"] == "generated" and saved["reason"] == "ok"
