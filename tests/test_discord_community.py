import asyncio
import json
import sqlite3
import time
from dataclasses import replace
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
        await asyncio.wait_for(stream.started.wait(), timeout=10)
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


async def test_all_split_parts_keep_generic_replies_private_and_explicit_intent_overrides(tmp_path):
    config, calls, sent = settings(tmp_path), [], []
    class Gateway:
        async def observe(self, question, previous=None, **kwargs):
            calls.append((question, previous, kwargs))
            return "わたしが確認した活動の記録です．" * (160 if len(calls) == 1 else 1)
        async def answer(self, *args):
            raise AssertionError("private community context must never reach public planning")
    ledger = BridgeLedger(config)
    bridge = RuntimeDiscordBridge(config, Gateway(), ledger)
    async def send(text):
        identifier = 1000 + len(sent)
        sent.append((identifier, text))
        return identifier
    await bridge.handle(DiscordEvent(100, 11, 20, 30, "<@10> HackSickの30日の活動状況を教えて", mentioned=True), send)
    parts = list(sent[1:])
    assert len(parts) >= 2
    for index, (answer_id, _text) in enumerate(parts):
        event = DiscordEvent(200 + index, 11, 20, 30, "詳しく教えて", reply_to=answer_id)
        previous = ledger.previous(event)
        await bridge.handle(event, send)
        assert calls[-1][0:2] == ("詳しく教えて", previous)
        assert calls[-1][2]["operation"] == "activity" and calls[-1][2]["period_days"] == 30
    await bridge.handle(DiscordEvent(300, 11, 20, 30, "今週の未解決の課題を教えて", reply_to=parts[0][0]), send)
    assert calls[-1][2]["operation"] == "unresolved" and calls[-1][2]["period_days"] == 7


async def test_public_answer_generic_reply_remains_public(tmp_path):
    config, calls = settings(tmp_path), []
    class Gateway:
        async def answer(self, question, previous):
            calls.append((question, previous))
            return "公開資料の補足です．"
        async def observe(self, *args, **kwargs):
            raise AssertionError("public context must retain public routing")
    ledger = BridgeLedger(config)
    ledger.remember(55, DiscordEvent(1, 11, 20, 30, "公開の質問"), "公開の回答です．")
    bridge = RuntimeDiscordBridge(config, Gateway(), ledger)
    async def send(text):
        return 1000
    await bridge.handle(DiscordEvent(100, 11, 20, 30, "詳しく教えて", reply_to=55), send)
    assert calls == [("詳しく教えて", "公開の回答です．")]


def test_private_provenance_uses_same_author_channel_guild_and_age_guards(tmp_path):
    ledger = BridgeLedger(settings(tmp_path))
    context = {"operation": "discussion", "period_days": 30}
    ledger.remember(55, DiscordEvent(1, 11, 20, 30, "議論"), "私的な議論の記録", community_context=context)
    event = DiscordEvent(100, 11, 20, 30, "詳しく教えて", reply_to=55)
    assert ledger.previous_context(event) == ("私的な議論の記録", context)
    for other in (replace(event, user_id=31), replace(event, channel_id=21), replace(event, guild_id=12)):
        assert ledger.previous(other) is None and ledger.previous_context(other) == (None, None)
    with ledger._db() as db:
        db.execute("UPDATE answers SET created=?", (time.time() - 86401,))
    assert ledger.previous_context(event) == (None, None)


async def test_pending_reply_route_survives_restart_without_answer_row(tmp_path):
    config, calls = settings(tmp_path), []
    class Gateway:
        async def observe(self, question, previous=None, **kwargs):
            calls.append((question, previous, kwargs))
            return "観測した議論の補足です．"
        async def answer(self, *args):
            raise AssertionError("recovered private context must not reach public planning")
    ledger = BridgeLedger(config)
    ledger.remember(55, DiscordEvent(1, 11, 20, 30, "議論"), "私的な過去回答",
        community_context={"operation": "discussion", "period_days": 30})
    marker = config.state_dir / "repair-drain"
    marker.write_text("fixture-drain")
    async def send(text):
        return 1000
    await RuntimeDiscordBridge(config, Gateway(), ledger).handle(
        DiscordEvent(100, 11, 20, 30, "詳しく教えて", reply_to=55), send)
    assert not calls and ledger.inbox_pending()
    with ledger._db() as db:
        db.execute("DELETE FROM answers")
    marker.unlink()
    restarted = RuntimeDiscordBridge(config, Gateway(), BridgeLedger(config))
    async def recovered(row, text):
        return 1001
    await restarted.recover_inbox(recovered)
    assert calls == [("詳しく教えて", "私的な過去回答", {"request_id": "100", "guild_id": "11", "channel_id": "20",
        "operation": "discussion", "period_days": 30})]
    assert not restarted.ledger.inbox_pending()


async def test_disabling_community_scope_never_sends_private_previous_to_public_gateway(tmp_path):
    original = settings(tmp_path)
    ledger = BridgeLedger(original)
    ledger.remember(55, DiscordEvent(1, 11, 20, 30, "議論"), "私的な過去回答",
        community_context={"operation": "discussion", "period_days": 30})
    class Gateway:
        async def answer(self, *args):
            raise AssertionError("disabled community context must fail closed")
        async def observe(self, *args, **kwargs):
            raise AssertionError("disabled community scope must not be used")
    config = original.model_copy(update={"community_guild_ids": ()})
    bridge, sent = RuntimeDiscordBridge(config, Gateway(), BridgeLedger(config)), []
    async def send(text):
        sent.append(text)
        return 1000
    await bridge.handle(DiscordEvent(100, 11, 20, 30, "詳しく教えて", reply_to=55), send)
    assert any("外部検索へは渡していません" in text for text in sent)
    assert not bridge.ledger.inbox_pending()


async def test_owner_orphan_intake_preserves_private_route_before_inbox_enqueue(tmp_path, monkeypatch):
    from ephy_worker.owner_repair import RepairStore
    monkeypatch.setattr("ephy_worker.owner_repair.OWNER_USER_ID", 30)
    config, calls = settings(tmp_path), []
    class Gateway:
        async def classify_repair(self, question, previous):
            calls.append("classify")
            return False
        async def observe(self, question, previous=None, **kwargs):
            calls.append((question, previous, kwargs))
            return "活動の補足です．"
        async def answer(self, *args):
            raise AssertionError("orphan owner private context must not reach public planning")
    ledger = BridgeLedger(config)
    context = {"operation": "activity", "period_days": 30}
    ledger.remember(55, DiscordEvent(1, 11, 20, 30, "活動"), "私的な過去回答", community_context=context)
    store = RepairStore(tmp_path / "repairs")
    bridge = RuntimeDiscordBridge(config, Gateway(), ledger)
    bridge.repairs = store
    def interrupted(*args, **kwargs):
        raise RuntimeError("fixture interruption after durable intake")
    monkeypatch.setattr(ledger, "enqueue", interrupted)
    async def send(text):
        return 1000
    await bridge.handle(DiscordEvent(100, 11, 20, 30, "詳しく教えて", reply_to=55), send)
    assert not ledger.inbox_has("100") and not calls
    identifier = store.capture_intake(guild_id=11, channel_id=20, message_id=100, author_id=30,
        text="詳しく教えて", previous_answer="私的な過去回答", community_context=context)
    with pytest.raises(RuntimeError, match="duplicate_repair_payload_changed"):
        store.capture_intake(guild_id=11, channel_id=20, message_id=100, author_id=30, text="詳しく教えて",
            previous_answer="私的な過去回答", community_context={"operation": "activity", "period_days": 7})
    restarted = RuntimeDiscordBridge(config, Gateway(), BridgeLedger(config))
    restarted.repairs = RepairStore(store.root)
    async def recovered(row, text):
        return 1001
    await restarted.recover_intakes(recovered)
    assert calls == ["classify", ("詳しく教えて", "私的な過去回答", {"request_id": "100", "guild_id": "11",
        "channel_id": "20", "operation": "activity", "period_days": 30})]
    with store.db() as db:
        assert db.execute("SELECT done FROM intakes WHERE id=?", (identifier,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_legacy_storage_migrates_to_null_provenance_without_changing_data(tmp_path):
    from ephy_worker.owner_repair import RepairStore
    config = settings(tmp_path)
    config.state_dir.mkdir(parents=True)
    with sqlite3.connect(config.state_dir / "discord-runtime.sqlite3") as db:
        db.execute("CREATE TABLE answers (id TEXT PRIMARY KEY,guild TEXT,channel TEXT,user TEXT,content TEXT,created REAL)")
        db.execute("INSERT INTO answers VALUES ('55','11','20','30',?,?)", ("既存の回答 https://discord.com/channels/11/20/1", time.time()))
    ledger = BridgeLedger(config)
    assert ledger.previous_context(DiscordEvent(100, 11, 20, 30, "詳しく教えて", reply_to=55)) == (
        "既存の回答 https://discord.com/channels/11/20/1", None)
    root = tmp_path / "repairs"
    root.mkdir()
    with sqlite3.connect(root / "owner-repair.sqlite3") as db:
        db.execute("CREATE TABLE intakes (id TEXT PRIMARY KEY,guild TEXT,channel TEXT,message TEXT,author TEXT,text TEXT,previous TEXT,route TEXT,receipt INTEGER,done INTEGER,consumer TEXT,created REAL,UNIQUE(guild,channel,message))")
        db.execute("INSERT INTO intakes VALUES ('legacy','11','20','100','30','既存の依頼','既存の過去回答',NULL,0,0,NULL,?)", (time.time(),))
    store = RepairStore(root)
    with store.db() as db:
        row = db.execute("SELECT * FROM intakes WHERE id='legacy'").fetchone()
        assert row["text"] == "既存の依頼" and row["previous"] == "既存の過去回答" and row["community_context"] is None


@pytest.mark.parametrize("query,operation", [
    ("未解決の件は？", "unresolved"), ("課題を教えて", "unresolved"), ("宿題をまとめて", "unresolved"),
    ("やり残しは？", "unresolved"), ("todoは何？", "unresolved"), ("タスクを教えて", "unresolved"),
    ("議論をまとめて", "discussion"), ("話題をまとめて", "discussion"), ("結論は？", "discussion"),
    ("決定を教えて", "discussion"), ("合意を教えて", "discussion"), ("みんなが話していた件は？", "discussion"),
    ("会話をまとめて", "discussion"), ("話し合ったことは？", "discussion"),
    ("活動を教えて", "activity"), ("活発だった？", "activity"), ("みんなの投稿をまとめて", "activity"),
    ("参加の様子は？", "activity"), ("メンバーの発言を教えて", "activity"), ("盛り上がっている？", "activity"),
    ("勢いを教えて", "activity"), ("静かだった？", "activity"),
    ("構成を教えて", "snapshot"), ("ロールを教えて", "snapshot"), ("チャンネル一覧は？", "snapshot"),
    ("全体像を教えて", "snapshot"), ("どんなサーバ？", "snapshot"), ("人数は？", "snapshot"),
    ("サーバー情報を教えて", "snapshot"), ("サーバ情報を教えて", "snapshot"),
])
def test_every_specific_operation_pattern_admits_local_community_requests(query, operation):
    assert community_intent(query) == (operation, 30)


@pytest.mark.parametrize("query", [
    "公式ドキュメントのタスク管理を調べて", "公開議論の合意をまとめて", "国会の発言を教えて",
    "公開仕様の構成を教えて", "https://example.com のロールを教えて", "詳しく教えて",
    "この製品の状態を教えて", "新型モデルについてまとめて", "改善の提案を教えて",
])
def test_specific_operation_routing_preserves_explicit_public_and_generic_questions(query):
    assert community_intent(query) is None


@pytest.mark.parametrize("query,operation", [
    ("みんなの投稿をまとめて", "activity"), ("メンバーの発言を教えて", "activity"),
    ("宿題を教えて", "unresolved"), ("合意を教えて", "discussion"), ("ロールを教えて", "snapshot"),
    ("このサーバーの発言と公開資料を照合して", "activity"),
    ("国会の発言を教えて", None), ("新型モデルについてまとめて", None),
])
async def test_specific_local_requests_reach_private_bridge_and_public_exceptions_stay_public(tmp_path, query, operation):
    calls = []
    class Gateway:
        async def observe(self, question, previous=None, **kwargs):
            assert operation is not None, "explicit public requests must keep their public route"
            calls.append(("private", question, kwargs["operation"]))
            return "観測した根拠に基づく回答です．"
        async def answer(self, question, previous):
            assert operation is None, "local community requests must never reach public planning"
            calls.append(("public", question, None))
            return "公開資料に基づく回答です．"
    config = settings(tmp_path)
    bridge = RuntimeDiscordBridge(config, Gateway(), BridgeLedger(config))
    async def send(text):
        return 1000
    await bridge.handle(DiscordEvent(100, 11, 20, 30, "<@10> " + query, mentioned=True), send)
    assert calls == [("private" if operation else "public", query, operation)]
