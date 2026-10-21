import asyncio
import json

import httpx
import pytest

from ephy_worker.discord_community import community_intent
from ephy_worker.discord_runtime import (
    PROGRESS,
    RECEIPT,
    BridgeError,
    BridgeLedger,
    DiscordEvent,
    DiscordRuntimeSettings,
    RuntimeDiscordBridge,
    RuntimeGateway,
)


@pytest.mark.parametrize("query,operation,days", [
    ("HackSickの今週の活動を教えて", "activity", 7),
    ("このサーバーの構成とロールを確認して", "snapshot", 30),
    ("最近の議論で何が決まった？", "discussion", 30),
    ("未解決の課題は何が残っている？", "unresolved", 30),
    ("HackSickの現状と運営の改善案を教えて", "report", 30),
])
def test_natural_japanese_community_intents(query, operation, days):
    assert community_intent(query) == (operation, days)


def settings(tmp_path, **updates):
    return DiscordRuntimeSettings(application_id=10, guild_id=11, allowed_channel_ids=(20,),
        allowed_user_ids=(30,), community_guild_ids=(11,), state_dir=tmp_path / "state",
        poll_interval_seconds=0.05, **updates)


def status(**updates):
    return {"provider": "worker", "contract": "0.4", "request_id": "100", "job_id": "observation1",
        "input_hash": "a" * 64, "worker_id": "worker-a", "status": "completed", "state": "completed",
        "analysis_status": "verified", **updates}


async def test_dedicated_gateway_pending_identity_and_partial_completion(tmp_path):
    calls = []
    async def fixture(request):
        calls.append(request)
        assert request.url.path == "/v1/discord/observe"
        if len(calls) == 1:
            return httpx.Response(202, json={"discord_observe_status": status(status="pending", worker_id=None,
                job_id=None, input_hash=None)})
        if len(calls) == 2:
            return httpx.Response(202, json={"discord_observe_status": status(status="pending", worker_id=None)})
        return httpx.Response(200, json={"discord_observe_status": status(status="partial", analysis_status="unverified"),
            "choices": [{"message": {"content": "わたしが確認した範囲では，議論の本文は取得できていません．"}}]})
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(fixture))
    try:
        answer = await gateway.observe("このサーバーの私的な議論を教えて", request_id="100", guild_id="11",
            channel_id="20", operation="discussion", period_days=30)
        assert "取得できていません" in answer
        payloads = [json.loads(call.content) for call in calls]
        assert payloads[0] == payloads[1]
        assert len(payloads) == 3
        assert set(payloads[0]) == {"request_id", "guild_id", "channel_id", "operation", "period_days", "query", "previous_answer"}
        assert calls[0].extensions["timeout"]["read"] == 600
    finally:
        await gateway.aclose()


async def test_gateway_rejects_changed_job_or_worker_identity(tmp_path):
    n = 0
    async def fixture(request):
        nonlocal n
        n += 1
        return httpx.Response(202 if n == 1 else 200, json={"discord_observe_status":
            status(status="pending" if n == 1 else "completed", worker_id="worker-a" if n == 1 else "worker-b")})
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(fixture))
    try:
        with pytest.raises(BridgeError, match="identity_mismatch"):
            await gateway.observe("HackSickの活動", request_id="100", guild_id="11", channel_id="20",
                operation="activity", period_days=7)
    finally:
        await gateway.aclose()


async def test_community_inbox_ack_progress_split_reply_without_public_search(tmp_path):
    class Gateway:
        def __init__(self):
            self.calls = []
        async def observe(self, query, previous_answer=None, **kwargs):
            self.calls.append((query, previous_answer, kwargs))
            await asyncio.sleep(0.03)
            return "わたしは観測した投稿を確認しました．" * 120
        async def answer(self, *args):
            raise AssertionError("community data must not enter public web search")
    gateway = Gateway()
    config = settings(tmp_path)
    bridge = RuntimeDiscordBridge(config, gateway, BridgeLedger(config), progress_seconds=0.001)
    sent = []
    async def send(text):
        sent.append(text)
        return 1000 + len(sent)
    event = DiscordEvent(100, 11, 20, 30, "<@10> HackSickの秘密の議論をまとめて", mentioned=True)
    await bridge.handle(event, send)
    assert sent[0] == RECEIPT and sent[1] == PROGRESS
    assert len(sent) > 3 and all(len(s.encode("utf-16-le")) // 2 <= 1800 for s in sent[2:])
    assert gateway.calls[0][2] == {"request_id": "100", "guild_id": "11", "channel_id": "20",
        "operation": "discussion", "period_days": 30}
    await bridge.handle(event, send)
    assert len(gateway.calls) == 1
    assert bridge.ledger.previous(DiscordEvent(101, 11, 20, 30, "続き", reply_to=1000 + len(sent)))


def test_ordinary_public_question_is_not_community_intent():
    assert community_intent("Pythonの公開仕様を調べて") is None
    assert community_intent("Discordコミュニティの作り方を調べて") is None
