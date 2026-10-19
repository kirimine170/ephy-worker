"""Production make_executor，task-local scope，safe artifacts and SDK delivery．"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_adaptive_research import Fetcher, ResearchModel, Search
from test_answer_routing import profile

from ephy_worker import answer_routing as routing
from ephy_worker import discord_answer, research
from ephy_worker.answer_routing import RouteProposal, request_context
from ephy_worker.config import AnswerPolicy, SearchConfig, WorkerConfig
from ephy_worker.discord_adapter import DiscordAdapter, DiscordSettings, Ledger, Request
from ephy_worker.discord_cli import build_client, make_executor
from ephy_worker.models import ModelError
from ephy_worker.schema import Limits


def setup(tmp_path, monkeypatch):
    settings = DiscordSettings(application_id=100, guild_id=200, allowed_channel_ids=[300],
        allowed_user_ids=[400, 401], worker_config=tmp_path / "worker.yaml", profile="p",
        output_dir=tmp_path / "jobs", state_dir=tmp_path / "state")
    config = WorkerConfig(model_profiles={"p": profile()}, search=SearchConfig(base_url="http://127.0.0.1:9"),
        answer_policy=AnswerPolicy(enabled=True), limits=Limits(job_seconds=300))
    observed = {"preflight": 0, "final": 0, "workers": []}
    class Router:
        def __init__(self, profile, budget): self.budget = budget
        async def run(self, output, prompt, **bounds):
            assert request_context.get() is None or request_context.get().user_id in {400, 401}
            observed["preflight"] += 1
            self.budget.take("model_requests")
            return RouteProposal(route="knowledge", kind="stable_explanation", text="DNSは名前をIPアドレスへ対応付ける仕組みです．")
        async def aclose(self): pass
    class Composer:
        def __init__(self, profile, budget): self.budget = budget
        async def run(self, output, prompt, **bounds):
            self.budget.take("model_requests")
            observed["final"] += 1
            return discord_answer.AnswerDraft(text="資料に示された項目を利用できます．", claim_ids=["R0-c001"])
        async def aclose(self): pass
    original = research.ResearchExecutor
    def factory(config, profile_id, store, **kwargs):
        budget = kwargs["budget"]
        worker = original(config, profile_id, store, model=ResearchModel(budget),
            search=Search(budget), fetcher=Fetcher(budget), **kwargs)
        observed["workers"].append(worker)
        return worker
    monkeypatch.setattr(routing, "ModelRunner", Router)
    monkeypatch.setattr(discord_answer, "ModelRunner", Composer)
    monkeypatch.setattr(research, "ResearchExecutor", factory)
    return settings, config, observed


async def scoped(execute, question, user=400):
    token = request_context.set(Request(500, 100, 200, 300, user))
    try:
        return await execute(question)
    finally:
        request_context.reset(token)


def report_for(settings, answer):
    return json.loads((settings.output_dir / answer.job_id / "report.json").read_text("utf-8"))


@pytest.mark.asyncio
async def test_actual_make_executor_knowledge_research_restart_cache_and_scope(tmp_path, monkeypatch):
    settings, config, observed = setup(tmp_path, monkeypatch)
    execute = make_executor(settings, config)
    knowledge = await scoped(execute, "DNSとは何ですか")
    report = report_for(settings, knowledge)
    assert report["claims"] == report["evidence"] == report["sources"] == []
    assert report["answerability"] == "unresolved" and report["stop_reason"] == "model_knowledge_unverified"
    assert report["metrics"]["answer_routing"]["externally_verified"] is False
    assert "外部資料は未確認" in knowledge.text
    question = "公開資料の出典付き比較"
    result = await scoped(execute, question)
    assert result.state == "completed" and observed["preflight"] == 1
    original = report_for(settings, result)
    assert original["metrics"]["answer_routing"]["cache_store"] == "stored"
    repeat = await scoped(make_executor(settings, config), question)
    cached = report_for(settings, repeat)
    assert repeat.job_id != result.job_id and cached["metrics"]["answer_routing"]["route"] == "cache"
    assert cached["metrics"]["requests"]["model_requests"] == 0 and cached["metrics"]["requests"]["search_requests"] == 0
    assert cached["metrics"]["answer_routing"]["original_created_at"] == original["created_at"]
    assert cached["sources"][0]["fetched_at"] == original["sources"][-1]["fetched_at"] or cached["sources"][0]["fetched_at"] in [s["fetched_at"] for s in original["sources"]]
    assert "参考：" in repeat.text and "未確認" not in repeat.text
    assert repeat.text.startswith("保存済みの確認結果") and original["finished_at"] in repeat.text
    cached_artifact = json.loads((settings.output_dir / repeat.job_id / "discord-answer.json").read_text("utf-8"))
    assert cached_artifact["text"] == repeat.text
    assert observed["final"] == 1 and len(observed["workers"]) == 1
    isolated = await scoped(execute, question, user=401)
    assert report_for(settings, isolated)["metrics"]["answer_routing"]["route"] == "research"
    assert request_context.get() is None


@pytest.mark.asyncio
async def test_no_context_has_no_persistent_cache(tmp_path, monkeypatch):
    settings, config, _ = setup(tmp_path, monkeypatch)
    result = await make_executor(settings, config)("公開資料の出典付き比較")
    assert report_for(settings, result)["metrics"]["answer_routing"]["cache_code"] == "no_request_context"
    assert not settings.state_dir.exists()


@pytest.mark.asyncio
async def test_known_schema_failure_uses_remaining_original_budget(tmp_path, monkeypatch):
    settings, config, observed = setup(tmp_path, monkeypatch)
    class Invalid:
        def __init__(self, profile, budget): self.budget = budget
        async def run(self, *args, **kwargs):
            self.budget.take("model_requests")
            raise ModelError("model_schema_invalid")
        async def aclose(self): pass
    monkeypatch.setattr(routing, "ModelRunner", Invalid)
    result = await scoped(make_executor(settings, config), "DNSとは何ですか")
    report = report_for(settings, result)
    assert report["metrics"]["answer_routing"]["reason"] == "preflight_invalid"
    assert report["metrics"]["requests"]["model_requests"] == len(observed["workers"][0].model.calls) + 2


@pytest.mark.asyncio
async def test_cancel_preflight_cleanup_failure_takes_priority(tmp_path, monkeypatch):
    settings, config, _ = setup(tmp_path, monkeypatch)
    entered = asyncio.Event()
    class Paused:
        def __init__(self, profile, budget): pass
        async def run(self, *args, **kwargs):
            entered.set()
            await asyncio.Future()
        async def aclose(self): raise OSError("cleanup fixture")
    monkeypatch.setattr(routing, "ModelRunner", Paused)
    task = asyncio.create_task(scoped(make_executor(settings, config), "DNSとは何ですか"))
    await entered.wait()
    task.cancel()
    result = await task
    assert result.state == "cancelled" and "取り消し" in result.text
    assert report_for(settings, result)["state"] == "cancelled"
    assert not list(settings.output_dir.glob("*/discord-answer.json"))


@pytest.mark.asyncio
async def test_save_failure_is_real_and_keeps_job_association(tmp_path, monkeypatch):
    settings, config, _ = setup(tmp_path, monkeypatch)
    def fail(*args): raise OSError("synthetic safe failure")
    monkeypatch.setattr(routing, "_save", fail)
    with pytest.raises(OSError) as error:
        await scoped(make_executor(settings, config), "DNSとは何ですか")
    assert error.value.job_id.startswith("research-")
    assert (settings.output_dir / error.value.job_id / "report.json").exists()
    assert json.loads((settings.output_dir / error.value.job_id / "report.json").read_text("utf-8"))["state"] == "failed"


@pytest.mark.asyncio
async def test_post_cache_save_failure_discards_entry_and_failed_artifact(tmp_path, monkeypatch):
    from ephy_worker.answer_cache import AnswerCache
    from ephy_worker.answer_routing import cache_namespace
    from ephy_worker.store import JobStore

    settings, config, _ = setup(tmp_path, monkeypatch)
    save = JobStore.save
    def fail_after_cache(self, report):
        if report.state == "completed" and report.metrics.get("answer_routing", {}).get("cache_store") == "stored":
            raise OSError("synthetic post-cache write failure")
        return save(self, report)
    monkeypatch.setattr(JobStore, "save", fail_after_cache)
    question = "公開資料の出典付き比較"
    with pytest.raises(OSError) as error:
        await scoped(make_executor(settings, config), question)
    job_id = error.value.job_id
    report = json.loads((settings.output_dir / job_id / "report.json").read_text("utf-8"))
    artifact = json.loads((settings.output_dir / job_id / "discord-answer.json").read_text("utf-8"))
    assert report["state"] == artifact["status"] == "failed"
    namespace = cache_namespace(settings, config, Request(501, 100, 200, 300, 400))
    assert AnswerCache(settings.state_dir, config.answer_policy).lookup(question, namespace)[1] == "miss"


def test_public_smoke_rebinds_search_cache_without_touching_original(tmp_path):
    import importlib.util
    from pathlib import Path

    from ephy_worker.config import SearchRequestPolicy

    script = Path(__file__).parents[1] / "scripts" / "smoke_answer_routing.py"
    spec = importlib.util.spec_from_file_location("public_smoke_control", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_path = tmp_path / "production-search.sqlite3"
    original_path.write_bytes(b"unchanged-production-control")
    config = WorkerConfig(search=SearchConfig(base_url="http://127.0.0.1:9",
        request_policy=SearchRequestPolicy(sqlite_path=str(original_path), min_interval_seconds=3)))
    before = config.model_dump_json()
    isolated = module.isolate_search_state(config, tmp_path / "isolated-state")
    assert isolated.search.request_policy.sqlite_path == str(tmp_path / "isolated-state" / "search-cache.sqlite3")
    assert isolated.search.request_policy.min_interval_seconds == 3
    assert config.model_dump_json() == before and original_path.read_bytes() == b"unchanged-production-control"


@pytest.mark.parametrize("entrypoint", ["slash", "mention"])
@pytest.mark.asyncio
async def test_routed_sdk_callback_scopes_request_inert_delivery_once(tmp_path, monkeypatch, entrypoint):
    import discord
    settings, config, observed = setup(tmp_path, monkeypatch)
    settings = settings.model_copy(update={"entrypoint": entrypoint})
    adapter = DiscordAdapter(settings, Ledger(settings), make_executor(settings, config))
    client, tree, guild = build_client(settings, adapter, "fixture-bot-token")
    try:
        if entrypoint == "slash":
            item = SimpleNamespace(id=700, application_id=100, guild_id=200, channel_id=300,
                user=SimpleNamespace(id=400, bot=False), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()))
            command = tree.get_command("ephy-research", guild=guild)
            await command.callback(item, "DNSとは何ですか")
            await command.callback(item, "DNSとは何ですか")
            sends = item.followup.send.await_args_list
        else:
            client._connection.user = SimpleNamespace(id=900, bot=True)
            item = SimpleNamespace(id=700, guild=SimpleNamespace(id=200), channel=SimpleNamespace(id=300, send=AsyncMock()),
                author=SimpleNamespace(id=400, bot=False), webhook_id=None, type=discord.MessageType.default,
                reference=None, mention_everyone=False, mentions=[SimpleNamespace(id=900)], content="<@900> DNSとは何ですか")
            await client.on_message(item)
            await client.on_message(item)
            sends = item.channel.send.await_args_list
        assert observed["preflight"] == 1 and request_context.get() is None
        assert any("外部資料は未確認" in call.args[0] for call in sends)
        assert all(call.kwargs["allowed_mentions"].to_dict()["parse"] == [] and call.kwargs["suppress_embeds"] for call in sends)
    finally:
        await client.close()
        await adapter.close()


@pytest.mark.parametrize("failure", ["model_timeout", "model_schema_invalid", "cancelled"])
@pytest.mark.asyncio
async def test_stage_model_timeout_preserves_exact_notice_body_and_cancellation_priority(tmp_path, monkeypatch, failure):
    settings, config, observed = setup(tmp_path, monkeypatch)
    original_factory = research.ResearchExecutor

    def failing_factory(*args, **kwargs):
        worker = original_factory(*args, **kwargs)
        original_run = worker.model.run

        async def fail_extract(output, prompt, *, stage, **bounds):
            if stage == "extract":
                worker.budget.take("model_requests")
                worker.model.calls.append((stage, bounds))
                if failure == "cancelled":
                    worker.failure("extract", "model_timeout")
                    raise asyncio.CancelledError
                raise ModelError(failure)
            return await original_run(output, prompt, stage=stage, **bounds)

        worker.model.run = fail_extract
        return worker

    monkeypatch.setattr(research, "ResearchExecutor", failing_factory)
    answer = await scoped(make_executor(settings, config), "公開資料の出典付き比較")
    report = report_for(settings, answer)
    artifact = json.loads((settings.output_dir / answer.job_id / "discord-answer.json").read_text("utf-8"))
    notice = "もう少し調べるのに時間がかかりそうです．"
    body = "The specified item is available under the documented standard conditions."
    assert artifact["text"] == answer.text
    assert report["sources"] and observed["final"] == observed["preflight"] == 0
    assert report["answerability"] != "sufficient" and not report["claims"]
    assert observed["workers"][0].model.closed and observed["workers"][0].fetcher.closed
    if failure == "cancelled":
        assert report["state"] == answer.state == "cancelled"
        assert answer.text == "調査を取り消しました．"
        assert notice not in answer.text and body not in answer.text
        assert artifact["partial_progress"]["available"] is False
    else:
        assert report["state"] == answer.state == "failed" and report["stop_reason"] == "stage_failed"
        assert report["failures"] == [{"stage": "extract", "code": failure}]
        assert body in answer.text and "内容の照合は未完了" in answer.text
        assert artifact["partial_progress"]["stage"] == "acquired_document_unchecked"
        assert artifact["verified_facts"] == 0
        if failure == "model_timeout":
            assert answer.text.startswith(notice + "\n\n") and answer.text.count(notice) == 1
        else:
            assert notice not in answer.text


@pytest.mark.asyncio
async def test_public_smoke_stops_after_failed_knowledge_without_passing_missing_checks(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    from ephy_worker.budget import Budget
    from ephy_worker.discord_adapter import Answer
    from ephy_worker.schema import Report
    from ephy_worker.store import JobStore

    script = Path(__file__).parents[1] / "scripts" / "smoke_answer_routing.py"
    spec = importlib.util.spec_from_file_location("public_smoke_failed_knowledge_control", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = WorkerConfig(model_profiles={"p": profile()}, search=SearchConfig(base_url="http://127.0.0.1:9"),
        answer_policy=AnswerPolicy(enabled=True))
    monkeypatch.setattr(module, "load_config", lambda path: config)
    # The script installs its aggregate budget for this process；restore it at test exit．
    monkeypatch.setattr(module.answer_routing, "Budget", Budget)
    questions = []

    def factory(settings, active_config):
        async def execute(question):
            questions.append(question)
            assert len(questions) == 1
            store = JobStore(settings.output_dir)
            report = Report(job_id=store.job_id, question=question, profile_id="p", state="failed",
                stop_reason="stage_failed", metrics={"answer_routing": {"route": "research", "externally_verified": False}})
            store.save(report)
            return Answer("failed", "取得した資料の照合は未完了です．", report.job_id)
        return execute

    monkeypatch.setattr(module, "make_executor", factory)
    args = SimpleNamespace(output=tmp_path / "smoke", isolated_state=None, config=tmp_path / "unused.yaml",
        profile="p", max_model_requests=17, max_external_queries=6)
    assert await module.run(args) == 2
    result = json.loads((args.output / "smoke-results.json").read_text("utf-8"))
    assert len(questions) == len(result["jobs"]) == 1 and "DNS" in questions[0]
    assert result["checks"] == {"knowledge": False} and result["passed"] is False
    required = {"knowledge", "research", "repeat_cache_zero_calls", "original_source_times", "cross_user_miss", "expired_miss"}
    assert set(result["checks"]) != required
    assert result["aggregate"] == {"physical_model_requests": 0, "search_requests": 0, "unique_external_queries": 0}
    assert result["no_discord_login_or_posts"] is True and request_context.get() is None
