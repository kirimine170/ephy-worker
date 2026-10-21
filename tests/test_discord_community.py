import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ephy_worker import discord_runtime
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
    ("昨日の議論をまとめて", "discussion", 7),
    ("昨日みんなが話していた件の結論は？", "discussion", 7),
    ("その話題の結論を教えて", "discussion", 30),
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
        "analysis_status": "verified", "progress_seq": 0, "phase": "collecting", **updates}


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


async def test_stagnant_pending_sequence_fails_on_idle_without_overall_poll_cap(tmp_path, monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(discord_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async def fixture(request):
        calls.append(request)
        clock[0] += 0.6
        return httpx.Response(202, json={"discord_observe_status": status(status="pending", phase="analyzing")})
    gateway = RuntimeGateway(settings(tmp_path, read_idle_timeout_seconds=1, max_polls=1),
        transport=httpx.MockTransport(fixture))
    try:
        with pytest.raises(BridgeError, match="runtime_observation_stalled"):
            await gateway.observe("昨日の議論をまとめて", request_id="100", guild_id="11", channel_id="20",
                operation="discussion", period_days=7)
        assert len(calls) == 3 and clock[0] > gateway.settings.read_idle_timeout_seconds
    finally:
        await gateway.aclose()


async def test_real_progress_can_exceed_idle_total_and_original_max_polls(tmp_path, monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(discord_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async def fixture(request):
        calls.append(request)
        clock[0] += 0.6
        pending = len(calls) <= 5
        return httpx.Response(202 if pending else 200, json={"discord_observe_status": status(
            status="pending" if pending else "completed", phase="analyzing" if pending else "rendering",
            progress_seq=len(calls)), "choices": [{"message": {"content": "わたしが確認した議論の結論です．"}}]})
    gateway = RuntimeGateway(settings(tmp_path, read_idle_timeout_seconds=1, max_polls=1),
        transport=httpx.MockTransport(fixture))
    try:
        answer = await gateway.observe("昨日の議論をまとめて", request_id="100", guild_id="11", channel_id="20",
            operation="discussion", period_days=7)
        assert "結論" in answer and len(calls) == 6
        assert clock[0] > 3 * gateway.settings.read_idle_timeout_seconds
        assert len(calls) > gateway.settings.max_polls
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("updates", [
    {"progress_seq": True}, {"progress_seq": -1}, {"progress_seq": None}, {"phase": "polling"},
])
async def test_pending_requires_real_typed_progress_fields(tmp_path, updates):
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(lambda request:
        httpx.Response(202, json={"discord_observe_status": status(status="pending", **updates)})))
    try:
        with pytest.raises(BridgeError, match="runtime_observation_progress_invalid"):
            await gateway.observe("昨日の議論をまとめて", request_id="100", guild_id="11", channel_id="20",
                operation="discussion", period_days=7)
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
    assert community_intent("昨日の国会での議論をニュースで調べて") is None
    assert community_intent("Pythonの公開仕様についての議論を公式ドキュメントで調べて") is None
    assert community_intent("コミュニティ運営の公開資料を検索して") is None
    assert community_intent("このサーバーの議論を公開情報と比べて") == ("discussion", 30)


@pytest.mark.parametrize("query", ["昨日の議論をまとめて", "昨日みんなが話していた件の結論は？"])
async def test_contextual_local_discussion_inbox_never_uses_public_search(tmp_path, query):
    class Gateway:
        def __init__(self):
            self.calls = []
        async def observe(self, query, previous_answer=None, **kwargs):
            self.calls.append((query, kwargs))
            return "わたしが観測した範囲の議論を確認しました．"
        async def answer(self, *args):
            raise AssertionError("ambiguous local discussion must stay private")
    gateway, config, sent = Gateway(), settings(tmp_path), []
    bridge = RuntimeDiscordBridge(config, gateway, BridgeLedger(config))
    async def send(text):
        sent.append(text)
        return 1000 + len(sent)
    await bridge.handle(DiscordEvent(100, 11, 20, 30, "<@10> " + query, mentioned=True), send)
    assert sent[0] == RECEIPT and len(sent) == 2
    assert gateway.calls == [(query, {"request_id": "100", "guild_id": "11", "channel_id": "20",
        "operation": "discussion", "period_days": 7})]


@pytest.mark.parametrize("repair", [True, False])
async def test_primary_owner_is_captured_and_classified_before_community_route(tmp_path, monkeypatch, repair):
    from ephy_worker.owner_repair import RepairStore
    monkeypatch.setattr("ephy_worker.owner_repair.OWNER_USER_ID", 30)
    query = "未解決の修正タスクを直して" if repair else "昨日の秘密の議論をまとめて"
    config, calls, sent = settings(tmp_path), [], []
    class Gateway:
        async def classify_repair(self, question, previous):
            with bridge.repairs.db() as db:
                captured = db.execute("SELECT text,receipt FROM intakes").fetchall()
            assert len(captured) == 1 and captured[0]["text"] == query and captured[0]["receipt"] == 1
            calls.append(("classify", question))
            return repair
        async def observe(self, question, previous=None, **kwargs):
            calls.append(("observe", question))
            assert kwargs == {"request_id": "100", "guild_id": "11", "channel_id": "20",
                "operation": "discussion", "period_days": 7}
            return "わたしが確認した範囲の議論です．"
        async def answer(self, *args):
            raise AssertionError("private owner community conversation must not reach public search")
    bridge = RuntimeDiscordBridge(config, Gateway(), BridgeLedger(config))
    bridge.repairs = RepairStore(tmp_path / "repairs")
    async def send(text):
        sent.append(text)
        return 1000 + len(sent)
    event = DiscordEvent(100, 11, 20, 30, "<@10> " + query, mentioned=True)
    await bridge.handle(event, send)
    with bridge.repairs.db() as db:
        assert db.execute("SELECT count(*) FROM intakes WHERE done=1").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == int(repair)
    if repair:
        assert calls == [("classify", query)]
        assert bridge.repairs.next()["text"] == query
        assert "修正依頼として受け付けました" in sent[-1]
    else:
        assert calls == [("classify", query), ("observe", query)]
        assert "議論です" in sent[-1] and bridge.repairs.next() is None
    assert sent[0] == RECEIPT
    await bridge.handle(event, send)
    assert len(calls) == (1 if repair else 2)


async def test_other_users_and_secondary_guild_cannot_enter_owner_repair(tmp_path, monkeypatch):
    from ephy_worker.owner_repair import RepairStore
    monkeypatch.setattr("ephy_worker.owner_repair.OWNER_USER_ID", 30)
    config = settings(tmp_path, additional_channels=({"guild_id": 12, "channel_id": 21},)).model_copy(
        update={"allowed_user_ids": (30, 31), "community_guild_ids": (11, 12)})
    observed = []
    class Gateway:
        async def classify_repair(self, *args):
            raise AssertionError("repair classification is restricted to the primary owner scope")
        async def observe(self, question, previous=None, **kwargs):
            observed.append(kwargs)
            return "わたしが確認した未解決項目の記録です．"
        async def answer(self, *args):
            raise AssertionError("community request must stay private")
    bridge = RuntimeDiscordBridge(config, Gateway(), BridgeLedger(config))
    bridge.repairs = RepairStore(tmp_path / "repairs")
    async def send(text):
        return 1000
    await bridge.handle(DiscordEvent(100, 11, 20, 31, "<@10> 未解決の修正タスクを直して", mentioned=True), send)
    await bridge.handle(DiscordEvent(101, 12, 21, 30, "<@10> 未解決の修正タスクを直して", mentioned=True), send)
    assert len(observed) == 2
    with bridge.repairs.db() as db:
        assert db.execute("SELECT count(*) FROM intakes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("chunk", [b" ", b": keepalive\n\n", b'{"discord_observe_status":'])
async def test_endless_response_body_stalls_closes_and_releases_next_inbox_request(tmp_path, chunk):
    class Keepalive(httpx.AsyncByteStream):
        def __init__(self):
            self.started, self.closed, self.chunks = asyncio.Event(), False, 0
        async def __aiter__(self):
            self.started.set()
            while True:
                await asyncio.sleep(0.02)
                self.chunks += 1
                yield chunk
        async def aclose(self):
            self.closed = True
    stream, calls = Keepalive(), []
    async def fixture(request):
        assert request.url.path == "/v1/discord/observe"
        data = json.loads(request.content)
        calls.append(data["request_id"])
        if data["request_id"] == "100":
            return httpx.Response(202, stream=stream)
        return httpx.Response(200, json={"discord_observe_status": status(request_id="101", job_id="observation2"),
            "choices": [{"message": {"content": "わたしは次の依頼の議論を確認しました．"}}]})
    config = settings(tmp_path, read_idle_timeout_seconds=1)
    gateway = RuntimeGateway(config, transport=httpx.MockTransport(fixture))
    bridge, sent = RuntimeDiscordBridge(config, gateway, BridgeLedger(config)), []
    async def send(text):
        sent.append(text)
        return 1000 + len(sent)
    event = DiscordEvent(100, 11, 20, 30, "<@10> 昨日の議論をまとめて", mentioned=True)
    first = asyncio.create_task(bridge.handle(event, send))
    try:
        await asyncio.wait_for(stream.started.wait(), timeout=1)
        await bridge.handle(DiscordEvent(101, 11, 20, 30, "<@10> 最近の議論をまとめて", mentioned=True), send)
        await asyncio.wait_for(first, timeout=3)
        assert stream.closed and stream.chunks > 1
        assert calls == ["100", "101"]
        assert any("進捗を一定時間確認できなかった" in text for text in sent)
        assert any("次の依頼の議論" in text for text in sent)
        assert bridge.inflight == 0 and not bridge.inbox_lock.locked() and not bridge.ledger.inbox_pending()
        await bridge.handle(event, send)
        assert calls == ["100", "101"]
    finally:
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await gateway.aclose()


async def test_header_wait_is_bounded_by_remaining_semantic_idle(tmp_path):
    cancelled = asyncio.Event()
    async def fixture(request):
        try:
            await asyncio.sleep(30)
            return httpx.Response(202, json={"discord_observe_status": status(status="pending")})
        finally:
            cancelled.set()
    gateway = RuntimeGateway(settings(tmp_path, read_idle_timeout_seconds=1), transport=httpx.MockTransport(fixture))
    try:
        with pytest.raises(BridgeError, match="runtime_observation_stalled"):
            await asyncio.wait_for(gateway.observe("昨日の議論をまとめて", request_id="100", guild_id="11",
                channel_id="20", operation="discussion", period_days=7), timeout=3)
        assert cancelled.is_set()
    finally:
        await gateway.aclose()
