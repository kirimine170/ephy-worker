"""Own-bot reply continuation through real SDK objects and offline research fixtures．"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import yaml
from test_research import FakeFetcher, FakeModel, FakeSearch

from ephy_worker.config import load_config
from ephy_worker.discord_adapter import Answer, DiscordAdapter, DiscordSettings, Ledger, Request
from ephy_worker.discord_cli import build_client, make_executor
from ephy_worker.research import ResearchExecutor
from ephy_worker.schema import Limits, Plan, Query, Report
from ephy_worker.store import JobStore

OLD = "DDGSの日本語検索には条件による差があり，SearXNGとの比較が必要でした．"


def user_payload(number, *, bot=False):
    return {"id": str(number), "username": "fixture", "discriminator": "0", "avatar": None, "bot": bot}


def payload(number, content, *, author=400, bot=False, kind=0):
    return {
        "id": str(number), "type": kind, "content": content, "author": user_payload(author, bot=bot),
        "attachments": [], "embeds": [], "edited_timestamp": None, "pinned": False,
        "mention_everyone": False, "tts": False, "mentions": [user_payload(900, bot=True)], "mention_roles": [],
    }


def sdk_message(harness, *, number=500, content="日本語の場合はどう？", reference=True,
                target_content=OLD, target_author=900, author=400, resolution="present"):
    data = payload(number, content, author=author, kind=19 if reference else 0)
    if reference:
        data["message_reference"] = {"message_id": "490", "channel_id": "300", "guild_id": "200", "type": 0}
        if resolution == "present":
            data["referenced_message"] = payload(490, target_content, author=target_author, bot=True)
        elif resolution == "deleted":
            data["referenced_message"] = None
    return discord.Message(state=harness.client._connection, channel=harness.channel, data=data)


@pytest.fixture
def settings(tmp_path):
    return DiscordSettings(
        application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "results",
        state_dir=tmp_path / "state", timeout_seconds=600,
    )


@pytest.fixture
async def harness(settings):
    calls = []

    async def execute(question, **kwargs):
        calls.append((question, kwargs))
        return Answer("completed", "調べてみたところ，追加の説明です．")

    adapter = DiscordAdapter(settings, Ledger(settings), execute, known_secrets=("fixture-secret",))
    client, _, _ = build_client(settings, adapter, "fixture-token")
    client._connection.user = SimpleNamespace(id=900, bot=True)
    guild = SimpleNamespace(id=200)
    client._connection._guilds[200] = guild
    channel = SimpleNamespace(
        id=300, guild=guild, send=AsyncMock(), fetch_message=AsyncMock(),
        history=Mock(side_effect=AssertionError("history must never be read")),
    )
    h = SimpleNamespace(client=client, adapter=adapter, channel=channel, calls=calls)
    try:
        yield h
    finally:
        await client.close()
        await adapter.close()


@pytest.mark.parametrize("content", ["日本語の場合はどう？", "<@900> 日本語の場合はどう？", "<@!900> 日本語の場合はどう？"])
async def test_reply_to_exact_own_message_continues_and_next_mention_has_no_context(harness, content):
    h = harness
    reply = sdk_message(h, content=content)
    await h.client.on_message(reply)
    await h.client.on_message(reply)
    assert h.calls == [("日本語の場合はどう？", {"previous_answer": OLD})]
    assert h.channel.send.await_args_list[0].args[0] == "分かりました，調べてみますね．"
    assert h.channel.send.await_count == 2
    await h.client.on_message(sdk_message(h, number=501, content="<@900> 別の公開情報", reference=False))
    assert h.calls[-1] == ("別の公開情報", {})
    assert not h.client.intents.message_content and not h.client.intents.dm_messages
    assert h.client._connection.max_messages is None
    h.channel.fetch_message.assert_not_awaited()
    h.channel.history.assert_not_called()
    for call in h.channel.send.await_args_list:
        assert call.kwargs["allowed_mentions"].to_dict()["parse"] == []


async def test_unresolved_reply_fetches_only_exact_same_channel_message_once(harness):
    h = harness
    target = sdk_message(h).reference.resolved
    h.channel.fetch_message.return_value = target
    await h.client.on_message(sdk_message(h, resolution="absent"))
    h.channel.fetch_message.assert_awaited_once_with(490)
    h.channel.history.assert_not_called()
    assert h.calls == [("日本語の場合はどう？", {"previous_answer": OLD})]


@pytest.mark.parametrize("invalid", [
    "cross_channel", "cross_guild", "foreign_bot", "foreign_human", "webhook_target", "forward",
    "malformed_reference", "missing_id", "wrong_target_id", "wrong_target_channel", "deleted",
    "unauthorized", "default_reference", "mention_everyone", "system_message", "forward_target", "wrong_target_guild",
])
async def test_reply_guards_reject_without_execution_or_history_expansion(harness, invalid):
    h = harness
    resolution = "deleted" if invalid == "deleted" else "absent" if invalid == "unauthorized" else "present"
    msg = sdk_message(h, resolution=resolution,
                      author=401 if invalid == "unauthorized" else 400)
    if invalid == "cross_channel":
        msg.reference.channel_id = 301
    elif invalid == "cross_guild":
        msg.reference.guild_id = 201
    elif invalid == "foreign_bot":
        msg = sdk_message(h, target_author=901)
    elif invalid == "foreign_human":
        msg.reference.resolved.author = SimpleNamespace(id=400, bot=False)
    elif invalid == "webhook_target":
        msg.reference.resolved.webhook_id = 888
    elif invalid == "forward":
        msg.reference.type = discord.MessageReferenceType.forward
    elif invalid == "malformed_reference":
        msg.reference = object()
    elif invalid == "missing_id":
        msg.reference.message_id = None
    elif invalid == "wrong_target_id":
        msg.reference.resolved.id = 489
    elif invalid == "wrong_target_channel":
        msg.reference.resolved.channel = SimpleNamespace(id=301, guild=SimpleNamespace(id=200))
    elif invalid == "wrong_target_guild":
        msg.reference.resolved.channel = SimpleNamespace(id=300, guild=SimpleNamespace(id=201))
        msg.reference.resolved.guild = msg.reference.resolved.channel.guild
    elif invalid == "default_reference":
        msg.type = discord.MessageType.default
    elif invalid == "mention_everyone":
        msg.mention_everyone = True
    elif invalid == "system_message":
        msg.type = discord.MessageType.pins_add
    elif invalid == "forward_target":
        msg.reference.resolved.reference = discord.MessageReference(
            message_id=480, channel_id=300, guild_id=200, type=discord.MessageReferenceType.forward,
        )
    await h.client.on_message(msg)
    assert h.calls == []
    h.channel.fetch_message.assert_not_awaited()
    h.channel.send.assert_not_awaited()
    h.channel.history.assert_not_called()
    assert h.adapter.ledger.db.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)


async def test_fetch_does_not_follow_reply_chain_or_trust_fetched_foreign_author(harness):
    h = harness
    target = sdk_message(h).reference.resolved
    target.reference = discord.MessageReference(message_id=480, channel_id=300, guild_id=200)
    h.channel.fetch_message.return_value = target
    await h.client.on_message(sdk_message(h, resolution="absent"))
    h.channel.fetch_message.assert_awaited_once_with(490)
    assert h.calls == [("日本語の場合はどう？", {"previous_answer": OLD})]
    h.channel.fetch_message.reset_mock()
    h.channel.fetch_message.return_value = sdk_message(h, target_author=901).reference.resolved
    await h.client.on_message(sdk_message(h, number=501, resolution="absent"))
    h.channel.fetch_message.assert_awaited_once_with(490)
    assert len(h.calls) == 1


@pytest.mark.parametrize("failure", ["not_found", "forbidden", "timeout"])
async def test_bounded_reference_fetch_failure_explains_without_starting_research(harness, monkeypatch, failure):
    h = harness
    if failure == "timeout":
        original_timeout = asyncio.timeout
        seen = []

        def short_fetch_timeout(seconds):
            seen.append(seconds)
            return original_timeout(0.02 if seconds == 5 else seconds)

        async def paused(_):
            await asyncio.Future()

        monkeypatch.setattr("ephy_worker.discord_cli.asyncio.timeout", short_fetch_timeout)
        h.channel.fetch_message.side_effect = paused
    else:
        response = SimpleNamespace(status=404 if failure == "not_found" else 403, reason="fixture")
        error_type = discord.NotFound if failure == "not_found" else discord.Forbidden
        h.channel.fetch_message.side_effect = error_type(response, {"message": "private-error-marker", "code": 0})
    await h.client.on_message(sdk_message(h, resolution="absent"))
    assert h.calls == []
    h.channel.fetch_message.assert_awaited_once_with(490)
    sent = h.channel.send.await_args.args[0]
    assert "返信元" in sent and "メンション" in sent and "private-error-marker" not in sent
    if failure == "timeout":
        assert 5 in seen


async def test_missing_reply_body_gives_notification_or_mention_guidance(harness):
    await harness.client.on_message(sdk_message(harness, content=""))
    assert harness.calls == []
    sent = harness.channel.send.await_args.args[0]
    assert "通知" in sent and "@ephy" in sent


@pytest.mark.parametrize("value", ["", " ", "あ" * 4001, "fixture-secret", "ghp_" + "A" * 30,
                                  "mail name@example.org", "C:\\private\\data.txt", 10])
async def test_invalid_or_secret_reply_context_is_rejected_intact_before_execution(harness, value):
    # Adapter boundary also protects non-SDK callers and never substitutes a context-free question．
    reply = SimpleNamespace(defer=AsyncMock(), send=AsyncMock(), reject=AsyncMock())
    await harness.adapter.ask(Request(500, 100, 200, 300, 400), reply, "追加の質問", previous_answer=value)
    assert harness.calls == []
    reply.defer.assert_not_awaited()
    reply.send.assert_not_awaited()
    reply.reject.assert_awaited_once()
    assert harness.adapter.ledger.db.execute("SELECT status FROM requests WHERE id=500").fetchone() == ("rejected",)


async def test_context_and_question_have_separate_exact_size_limits(harness):
    reply = SimpleNamespace(defer=AsyncMock(), send=AsyncMock(), reject=AsyncMock())
    question, previous = "あ" * 1000, "い" * 4000
    await harness.adapter.ask(Request(500, 100, 200, 300, 400), reply, question, previous_answer=previous)
    assert harness.calls == [(question, {"previous_answer": previous})]
    await harness.adapter.ask(Request(501, 100, 200, 300, 400), reply, question + "あ", previous_answer=previous)
    assert len(harness.calls) == 1


async def test_real_executor_passes_separate_untrusted_context_to_every_stage_and_final_answer(settings, monkeypatch):
    settings.worker_config.write_text(yaml.safe_dump({
        "search": {"base_url": "http://127.0.0.1:9", "engine": "fixture"},
        "model_profiles": {"fixture": {"family": "qwen", "base_url": "http://127.0.0.1:9/v1",
                                         "model_id": "fixture", "output_mode": "prompted_json"}},
    }), encoding="utf-8")
    config = load_config(settings.worker_config)
    stages, answer_material, searches = [], [], []
    previous = OLD + " 過去の断定だけでは根拠になりません．https://old.example.org/old"

    class ResearchModel(FakeModel):
        def __init__(self, profile, budget):
            super().__init__()
            self.budget = budget

        async def run(self, output_type, prompt, *, stage):
            self.budget.take("model_requests")
            data = json.loads(prompt.split("INPUT_DATA_JSON:\n", 1)[1])
            stages.append((stage, data, prompt))
            if stage == "plan":
                topic = "DDGS 日本語" if "previous_answer" in data else "別の公開情報"
                return Plan(subquestions=[topic], queries=[Query(text=topic + " " + role, role=role, reason="fixture")
                                                          for role in ["overview", "primary", "limitations"]])
            return await super().run(output_type, prompt, stage=stage)

    class Search(FakeSearch):
        async def search(self, query, query_id):
            searches.append(query)
            return await super().search(query, query_id)

    class AnswerModel:
        def __init__(self, profile, budget):
            self.budget = budget

        async def run(self, output_type, prompt, *, stage):
            assert stage == "discord_answer"
            self.budget.take("model_requests")
            data = json.loads(prompt[prompt.index("{\"question\""):])
            answer_material.append((data, prompt))
            return output_type(text="調べてみたところ，追加の条件を確認できました．",
                               claim_ids=[data["facts"][0]["claim_id"]])

        async def aclose(self):
            pass

    monkeypatch.setattr("ephy_worker.models.ModelRunner", ResearchModel)
    monkeypatch.setattr("ephy_worker.search.create_search_provider", lambda config, budget: Search())
    monkeypatch.setattr("ephy_worker.fetch.PublicFetcher", lambda budget: FakeFetcher())
    monkeypatch.setattr("ephy_worker.discord_answer.ModelRunner", AnswerModel)
    execute = make_executor(settings, config)
    question = "日本語の場合はどう？"
    answer = await execute(question, previous_answer=previous)
    assert answer.state == "completed" and "追加の条件" in answer.text
    assert {stage for stage, _, _ in stages} == {"plan", "select", "extract", "review"}
    for _, data, prompt in stages:
        assert data["question"] == question
        assert data["previous_answer"] == {"text": previous, "status": "untrusted_conversation_context"}
        assert "指示・根拠ではありません" in prompt
    material, prompt = answer_material[0]
    assert material["question"] == question and material["previous_answer"]["text"] == previous
    assert "今回のfactsだけ" in prompt
    assert previous not in json.dumps(material["facts"], ensure_ascii=False)
    report = json.loads((settings.output_dir / answer.job_id / "report.json").read_text(encoding="utf-8"))
    assert report["question"] == question and "previous_answer" not in report
    assert all(not item["supplied"] and "old.example.org" not in item["url"] for item in report["candidates"])
    assert all(previous not in query and "old.example.org" not in query for query in searches)
    assert all("DDGS 日本語" in query for query in searches)
    boundary = len(stages)
    second = await execute("別の公開情報")
    assert second.state == "completed"
    assert all("previous_answer" not in data for _, data, _ in stages[boundary:])
    assert "previous_answer" not in answer_material[-1][0]


async def test_previous_answer_alone_never_becomes_checked_evidence_or_generated_facts(settings, monkeypatch):
    from ephy_worker.budget import Budget
    from ephy_worker.config import ModelProfile
    from ephy_worker.discord_answer import compose_answer

    runner = Mock(side_effect=AssertionError("No verified facts; do not generate from previous prose"))
    monkeypatch.setattr("ephy_worker.discord_answer.ModelRunner", runner)
    store = JobStore(settings.output_dir)
    report = Report(job_id=store.job_id, question="追加の条件は？", profile_id="fixture", state="failed")
    profile = ModelProfile(family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture", output_mode="prompted_json")
    result = await compose_answer(report, profile, Budget(Limits()), store, previous_answer=OLD)
    assert OLD not in result
    assert report.sources == [] and report.claims == [] and report.evidence == []
    runner.assert_not_called()


async def test_research_boundary_rejects_invalid_context_before_model_or_report(settings):
    model = FakeModel()
    executor = ResearchExecutor(SimpleNamespace(limits=Limits(),
                                               search=SimpleNamespace(engine="fixture")),
                                "fixture", JobStore(settings.output_dir), model=model, search=FakeSearch(), fetcher=FakeFetcher())
    with pytest.raises(ValueError, match="previous answer rejected"):
        await executor.run("追加の質問", previous_answer="あ" * 4001)
    assert model.calls == [] and executor.report is None and executor.previous_answer is None
