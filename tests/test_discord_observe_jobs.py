import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_discord_observer import DiscordFixture, observation_input, observer_config
from test_worker_auto_scheduling import manager as manager_fixture
from test_worker_auto_scheduling import register, submit

from ephy_worker.config import SearchConfig
from ephy_worker.contracts import JobUsage, ObservedGuild, ResultEnvelope, SubmitJob, observation_capability
from ephy_worker.discord_observer import DiscordObserver, empty_payload
from ephy_worker.fixture_backend import InProcessManagerClient
from ephy_worker.manager import ManagerError
from ephy_worker.schema import Limits
from ephy_worker.worker_service import WorkerService

manager = manager_fixture


def observation_submit(key="observe", *, channels=None):
    input = observation_input().model_dump(mode="json")
    if channels:
        input["channel_ids"] = channels
    return SubmitJob(contract="0.4", submit_key=key, kind="discord.observe", target_worker_id="auto",
        input=input, budget=Limits(), deadline_seconds=600)


async def test_observe_worker_executes_auto_persists_idempotently_and_expires_raw(manager, monkeypatch):
    monkeypatch.setenv("EPHY_DISCORD_BOT_TOKEN", "fixture-token-1234567890")
    fixture = DiscordFixture()
    observer = DiscordObserver(observer_config(), transport=httpx.MockTransport(fixture))
    service = WorkerService(InProcessManagerClient(manager, "a-secret"), "worker-a",
        search_config=SearchConfig(base_url="http://127.0.0.1:8080"), observer_config=observer_config(), observer=observer)
    try:
        await service.register()
        view, _ = manager.submit(observation_submit())
        claimed = manager.claim("worker-a", "a-secret")
        assert await service._execute_job(claimed) == 0
        result = manager.job_result(view["job_id"])
        assert manager.job_view(view["job_id"])["state"] == "completed"
        assert result["payload"]["mode"] == "observe"
        assert result["payload"]["messages"][0]["content"] == "今週の計画を相談しました．"
        envelope = ResultEnvelope.model_validate(result)
        manager.finalize(view["job_id"], "worker-a", "a-secret", claimed["attempt"]["lease_token"], envelope)
        assert manager._conn.execute("SELECT COUNT(*) FROM discord_observations").fetchone()[0] == 1
        ref, path = manager.get_artifact(view["job_id"], result["manifest"][0]["artifact_id"])
        assert json.loads(path.read_bytes())["payload"]["mode"] == "observe"
        assert ref.bytes == path.stat().st_size
        manager._conn.execute("UPDATE discord_observations SET expires_at=0")
        manager._conn.commit()
        with pytest.raises(ManagerError, match="observation_retention_expired"):
            manager.job_result(view["job_id"])
        assert not path.exists()
        expired = manager.job_view(view["job_id"])
        assert expired["result"] is None and expired["stop_reason"] == "observation_retention_expired"
        assert not manager._conn.execute("SELECT * FROM discord_observations").fetchall()
    finally:
        await observer.aclose()


def test_observe_requires_exact_scoped_capability_and_does_not_block_explicit_web(manager):
    register(manager)
    register(manager, "worker-b", capabilities=["web.collect", "discord.observe", observation_capability("123", "457")])
    view, _ = manager.submit(observation_submit())
    assert view["wait_reason"] == "no_eligible_worker"
    web, _ = manager.submit(submit("web-behind-observe", target="worker-a"))
    assert manager.claim("worker-a", "a-secret")["job"]["job_id"] == web["job_id"]
    assert manager.claim("worker-b", "b-secret") is None
    register(manager, "worker-b", capabilities=["web.collect", *observer_config().capabilities()])
    assert manager.claim("worker-b", "b-secret")["job"]["job_id"] == view["job_id"]


def test_kind_mode_and_final_scope_mismatch_rejected(manager):
    with pytest.raises(ValidationError):
        SubmitJob.model_validate({**observation_submit().model_dump(mode="json"), "kind": "web.collect"})
    register(manager, capabilities=["web.collect", *observer_config().capabilities()])
    view, _ = manager.submit(observation_submit())
    claim = manager.claim("worker-a", "a-secret")
    payload = empty_payload(observation_input())
    payload.coverage.guild_id = "124"
    envelope = ResultEnvelope(contract="0.4", job_id=view["job_id"], attempt_id=claim["attempt"]["attempt_id"],
        status="failed", usage=JobUsage(), elapsed_seconds=0, payload=payload)
    with pytest.raises(ManagerError, match="observation_scope_mismatch"):
        manager.finalize(view["job_id"], "worker-a", "a-secret", claim["attempt"]["lease_token"], envelope)


def test_observe_partial_retry_preserves_kind_scope_and_remaining_http_budget(manager):
    register(manager, capabilities=["web.collect", *observer_config().capabilities()])
    request = observation_submit().model_copy(update={"budget": Limits(max_sources=1, fetch_requests=20)})
    view, _ = manager.submit(request)
    claim = manager.claim("worker-a", "a-secret")
    envelope = ResultEnvelope(contract="0.4", job_id=view["job_id"], attempt_id=claim["attempt"]["attempt_id"],
        status="partial", usage=JobUsage(fetch_requests=7), elapsed_seconds=0,
        payload=empty_payload(observation_input()))
    manager.finalize(view["job_id"], "worker-a", "a-secret", claim["attempt"]["lease_token"], envelope)
    child, created = manager.retry(view["job_id"])
    assert created and child["kind"] == "discord.observe" and child["target_worker_id"] == "auto"
    assert child["input"] == view["input"]
    assert child["budget"]["fetch_requests"] == 12 and child["budget"]["max_sources"] == 1


async def test_observe_cancel_renews_and_preserves_captured_evidence(manager):
    class SlowObserver:
        async def observe(self, input, budget, *, result):
            result.snapshot.guild = ObservedGuild(id=input.guild_id, name="fixture")
            await asyncio.sleep(30)
        async def aclose(self):
            pass
    config = observer_config()
    service = WorkerService(InProcessManagerClient(manager, "a-secret"), "worker-a",
        search_config=SearchConfig(base_url="http://127.0.0.1:8080"), observer_config=config, observer=SlowObserver(),
        lease_renew_ratio=0.01)
    await service.register()
    view, _ = manager.submit(observation_submit())
    claim = manager.claim("worker-a", "a-secret")
    task = asyncio.create_task(service._execute_job(claim))
    await asyncio.sleep(0.02)
    manager.cancel(view["job_id"])
    assert await asyncio.wait_for(task, timeout=3) == 0
    assert manager.job_view(view["job_id"])["state"] == "cancelled"


def bounded_observation_result(input, view, claim, *, evidence_count=2, reported_count=None):
    from datetime import timedelta

    from ephy_worker.contracts import ObservedMessage
    from ephy_worker.discord_observer import activity

    payload = empty_payload(input, content_requested=True)
    payload.messages = [ObservedMessage(guild_id=input.guild_id, channel_id=channel,
        message_id=str(index + 1), author_id="789", author_bot=False, operator=False,
        created_at=input.observed_before - timedelta(days=1),
        permalink=f"https://discord.com/channels/{input.guild_id}/{channel}/{index + 1}",
        content=f"fixture evidence {channel}/{index + 1}", content_status="available")
        for channel in input.channel_ids for index in range(evidence_count)]
    payload.activity = activity(payload.messages, input)
    payload.coverage.status = "complete"
    for channel in payload.coverage.channels:
        channel.status, channel.reason, channel.history_permission = "complete", None, "granted"
        channel.message_count = evidence_count if reported_count is None else reported_count
    return ResultEnvelope(contract="0.4", job_id=view["job_id"], attempt_id=claim["attempt"]["attempt_id"],
        status="succeeded", usage=JobUsage(), elapsed_seconds=0, payload=payload)


@pytest.mark.parametrize("evidence_count,reported_count", [(3, 2), (1, 3)])
def test_finalize_rejects_per_channel_evidence_or_reported_count_over_request_cap(manager, evidence_count, reported_count):
    register(manager, capabilities=["web.collect", *observer_config().capabilities()])
    input = observation_input(max_messages_per_channel=2)
    request = observation_submit().model_copy(update={"input": input})
    view, _ = manager.submit(request)
    claim = manager.claim("worker-a", "a-secret")
    result = bounded_observation_result(input, view, claim,
        evidence_count=evidence_count, reported_count=reported_count)
    original = result.model_dump(mode="json")
    with pytest.raises(ManagerError, match="observation_budget_exceeded") as error:
        manager.finalize(view["job_id"], "worker-a", "a-secret", claim["attempt"]["lease_token"], result)
    assert error.value.status == 409
    assert result.model_dump(mode="json") == original
    assert manager.job_view(view["job_id"])["state"] == "running"
    assert manager.job_view(view["job_id"])["result"] is None
    assert manager._conn.execute("SELECT finalized_at FROM attempts").fetchone()[0] is None
    assert manager._conn.execute("SELECT COUNT(*) FROM discord_observations").fetchone()[0] == 0


def test_finalize_accepts_exact_cap_per_channel_without_truncating_evidence(manager):
    register(manager, capabilities=["web.collect", "discord.observe",
        observation_capability("123", "456"), observation_capability("123", "457")])
    input = observation_input(max_messages_per_channel=2).model_copy(update={"channel_ids": ["456", "457"]})
    view, _ = manager.submit(observation_submit().model_copy(update={"input": input}))
    claim = manager.claim("worker-a", "a-secret")
    result = bounded_observation_result(input, view, claim)
    final = manager.finalize(view["job_id"], "worker-a", "a-secret", claim["attempt"]["lease_token"], result)
    assert final["state"] == "completed"
    assert manager.job_result(view["job_id"]) == result.model_dump(mode="json")
    stored = json.loads(manager._conn.execute("SELECT payload FROM discord_observations").fetchone()[0])
    assert stored["messages"] == result.model_dump(mode="json")["payload"]["messages"]
    assert len(stored["messages"]) == 4
    assert [channel["message_count"] for channel in stored["coverage"]["channels"]] == [2, 2]


async def test_http_result_retention_expiry_and_existing_result_wire_shapes(tmp_path, monkeypatch):
    from test_manager import result as web_result
    from test_manager_http import _start_manager

    from ephy_worker.contracts import WorkerRegistration
    from ephy_worker.manager_client import ManagerAPIError, ManagerClient

    app, runner, endpoint = await _start_manager(tmp_path, monkeypatch)
    requester, worker = ManagerClient(endpoint, "requester-secret"), ManagerClient(endpoint, "worker-secret")
    try:
        await worker.register_worker("worker-http", WorkerRegistration(contract="0.4", worker_id="worker-http",
            capabilities=["web.collect", *observer_config().capabilities()], implementation_version="fixture",
            os="fixture", architecture="fixture", concurrency=1).model_dump(mode="json"))
        with pytest.raises(ManagerAPIError) as unknown:
            await requester.job_result("unknown-job")
        assert (unknown.value.status, unknown.value.code) == (404, "unknown_job")
        input = observation_input(max_messages_per_channel=2)
        request = observation_submit().model_copy(update={"input": input})
        view = await requester.submit_job(request.model_dump(mode="json"))
        with pytest.raises(ManagerAPIError) as pending:
            await requester.job_result(view["job_id"])
        assert (pending.value.status, pending.value.code) == (409, "not_terminal")
        claim = await worker.claim_job("worker-http")
        result = bounded_observation_result(input, view, claim)
        final = await worker.finalize_job("worker-http", view["job_id"], claim["attempt"]["lease_token"],
            result.model_dump(mode="json"))
        assert await requester.job_result(view["job_id"]) == {key: final[key]
            for key in ("job_id", "state", "stop_reason", "usage", "result")}
        app.manager._conn.execute("UPDATE discord_observations SET expires_at=0")
        app.manager._conn.commit()
        with pytest.raises(ManagerAPIError) as expired:
            await requester.job_result(view["job_id"])
        assert (expired.value.status, expired.value.code) == (410, "observation_retention_expired")
        assert (await requester.get_job(view["job_id"]))["result"] is None
        assert app.manager._conn.execute("SELECT COUNT(*) FROM discord_observations").fetchone()[0] == 0

        web = await requester.submit_job(submit("http-web", target="worker-http").model_dump(mode="json"))
        with pytest.raises(ManagerAPIError) as web_pending:
            await requester.job_result(web["job_id"])
        assert (web_pending.value.status, web_pending.value.code) == (409, "not_terminal")
        web_claim = await worker.claim_job("worker-http")
        completed = await worker.finalize_job("worker-http", web["job_id"], web_claim["attempt"]["lease_token"],
            web_result(web["job_id"], web_claim["attempt"]["attempt_id"]).model_dump(mode="json"))
        assert await requester.job_result(web["job_id"]) == {key: completed[key]
            for key in ("job_id", "state", "stop_reason", "usage", "result")}
        cancelled = await requester.submit_job(submit("http-web-cancel", target="worker-http").model_dump(mode="json"))
        cancelled = await requester.cancel_job(cancelled["job_id"])
        assert await requester.job_result(cancelled["job_id"]) == {key: cancelled[key]
            for key in ("job_id", "state", "stop_reason", "usage", "result")}
        assert cancelled["state"] == "cancelled" and cancelled["result"] is None
    finally:
        await requester.aclose()
        await worker.aclose()
        await runner.cleanup()
