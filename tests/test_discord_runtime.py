"""Discord transport controls run offline without importing the optional SDK．"""

import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from ephy_worker.cli import main
from ephy_worker.discord_runtime import (
    PERSONA,
    PROGRESS,
    RECEIPT,
    BridgeError,
    BridgeLedger,
    DiscordEvent,
    DiscordRuntimeSettings,
    RuntimeDiscordBridge,
    RuntimeGateway,
    run_discord,
    safe_final,
)


def settings(tmp_path, **updates):
    return DiscordRuntimeSettings(application_id=10, guild_id=11, allowed_channel_ids=(20,),
        allowed_user_ids=(30, 31), state_dir=tmp_path / "state", poll_interval_seconds=0.05, **updates)


def final_stream(content="わたしは公開資料を確認しました．", *, finish="stop", done=True,
                 status="completed", plan_id="plan1", job_id="job1", reasoning="hidden-reasoning"):
    outcome = {"status": status, "provider": "worker", "plan_id": plan_id, "job_id": job_id}
    blocks = ["event: route\ndata: " + json.dumps({"provider": "fixture"}) + "\n\n",
        "event: web_search_status\ndata: " + json.dumps(outcome) + "\n\n",
        "event: sources\ndata: " + json.dumps({"sources": [{"url": "https://example.org"}]}) + "\n\n",
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {
            "content": content, "reasoning_content": reasoning}, "finish_reason": None}]}) + "\n\n"]
    if finish:
        blocks.append("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n")
    if done:
        blocks.append("data: [DONE]\n\n")
    return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"},
        content="".join(blocks).encode())


class RuntimeFixture:
    def __init__(self, *, decision="allow", pending=0, response=None):
        self.decision, self.pending, self.response = decision, pending, response
        self.calls = []

    async def __call__(self, request):
        data = json.loads(request.content)
        self.calls.append((request.url.path, data, request.extensions))
        if request.url.path == "/v1/web-search/plan":
            return httpx.Response(200, json={"decision": self.decision, "plan_id": "plan1"})
        if request.url.path == "/v1/web-search/cancel":
            return httpx.Response(200, json={"status": "cancel_requested"})
        assert request.url.path == "/v1/chat/completions"
        if self.pending:
            self.pending -= 1
            return httpx.Response(202, json={"status": "pending", "job_id": "job1", "plan_id": "plan1"})
        return self.response or final_stream()


@pytest.mark.parametrize("url", ["http://example.org", "http://localhost:8000", "http://127.0.0.1@evil.test",
    "http://127.0.0.1:8000/path", "http://127.0.0.1:8000?token=value", "http://[::ffff:127.0.0.1]:8000"])
def test_settings_reject_nonliteral_or_ambiguous_gateway(tmp_path, url):
    with pytest.raises(ValidationError):
        settings(tmp_path, runtime_url=url)


@pytest.mark.parametrize("field,value", [("guild_id", True), ("allowed_user_ids", ()),
    ("allowed_channel_ids", (20, 20)), ("token_env", "secret value"), ("state_dir", "."),
    ("read_message_content", "yes")])
def test_frozen_settings_fail_closed(tmp_path, field, value):
    data = settings(tmp_path).model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        DiscordRuntimeSettings.model_validate(data)
    with pytest.raises(ValidationError):
        settings(tmp_path).guild_id = 12


def test_settings_git_and_linked_ledger_rejected(tmp_path):
    git = tmp_path / "repo"
    git.mkdir()
    (git / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    with pytest.raises(ValidationError):
        settings(git)
    config = settings(tmp_path)
    ledger = BridgeLedger(config)
    linked = tmp_path / "linked"
    linked.mkdir()
    os.link(ledger.path, linked / "discord-runtime.sqlite3")
    with pytest.raises(BridgeError):
        BridgeLedger(config.model_copy(update={"state_dir": linked}))


@pytest.mark.asyncio
async def test_pending_resumes_same_payload_then_consumes_only_final_content(tmp_path):
    fixture = RuntimeFixture(pending=2)
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(fixture))
    try:
        result = await gateway.answer("公開の技術を教えて")
        assert result == "わたしは公開資料を確認しました．" and "hidden-reasoning" not in result
        chats = [data for path, data, _ in fixture.calls if path.endswith("completions")]
        assert len(chats) == 3 and all(data == chats[0] for data in chats)
        assert chats[0]["stream"] is True
        assert chats[0]["metadata"] == {"web_search": True, "web_search_plan_id": "plan1"}
        assert chats[0]["messages"][0]["content"] == PERSONA
        assert "必ずわたしなりの意見" in PERSONA and "不確かな点" in PERSONA
        assert fixture.calls[0][1] == {"query": "公開の技術を教えて"}
        assert fixture.calls[-1][2]["timeout"]["read"] == 600
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("decision", ["confirm", "block"])
@pytest.mark.asyncio
async def test_unapproved_plan_never_chats_or_approves(tmp_path, decision):
    fixture = RuntimeFixture(decision=decision)
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(fixture))
    try:
        with pytest.raises(BridgeError) as error:
            await gateway.answer("公開の質問")
        assert error.value.code == ("search_confirmation_required" if decision == "confirm" else "search_blocked")
        assert [path for path, _, _ in fixture.calls] == ["/v1/web-search/plan"]
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("fault", ["finish-missing", "done-missing", "length", "negative-status", "wrong-plan",
    "pending-plan", "pending-job", "redirect", "large", "error-event", "invalid-json", "reasoning-only"])
@pytest.mark.asyncio
async def test_bad_stream_or_identity_is_explicit_failure(tmp_path, fault):
    calls = []
    async def handler(request):
        calls.append(request.url)
        if request.url.path.endswith("plan"):
            if fault == "redirect": return httpx.Response(307, headers={"location": "http://example.org/secret"})
            if fault == "large": return httpx.Response(200, content=b"x" * 131073)
            return httpx.Response(200, json={"decision": "allow", "plan_id": "plan1"})
        if fault in {"pending-plan", "pending-job"}:
            return httpx.Response(202, json={"status": "pending", "plan_id": "bad" if fault == "pending-plan" else "plan1",
                "job_id": "job1" if len(calls) == 2 else "job2"})
        if fault == "error-event":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b'event: error\ndata: {"code":"failed"}\n\n')
        if fault == "invalid-json":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b'data: invalid\n\n')
        return final_stream(finish=None if fault == "finish-missing" else "length" if fault == "length" else "stop",
            done=fault != "done-missing", status="failed" if fault == "negative-status" else "completed",
            plan_id="different" if fault == "wrong-plan" else "plan1", content="" if fault == "reasoning-only" else "最終回答")
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BridgeError):
            await gateway.answer("公開の質問")
        assert all(url.host == "127.0.0.1" for url in calls)
    finally:
        await gateway.aclose()


def test_final_redacts_secrets_and_thinking_and_bounds_utf16():
    secret = "fake-discord-token-" + "private"
    text = safe_final("<think>internal-only</think>わたしの回答。 " + secret + "😀" * 3000, (secret,))
    assert "internal-only" not in text and secret not in text and "[redacted]" in text
    assert len(text.encode("utf-16-le")) <= 3600 and "回答．" in text
    with pytest.raises(BridgeError): safe_final("<analysis>unfinished secret")


def event(**updates):
    return replace(DiscordEvent(1, 11, 20, 30, "<@10> 公開の質問", mentioned=True), **updates)


@pytest.mark.parametrize("updates", [{"guild_id": None}, {"guild_id": 99}, {"channel_id": 99},
    {"user_id": 99}, {"author_bot": True}, {"mentioned": False}, {"mentioned": False, "reply_to": 999},
    {"webhook": True}, {"system": True}, {"mention_everyone": True}])
@pytest.mark.asyncio
async def test_unauthorized_and_unrelated_events_have_no_http_or_receipt(tmp_path, updates):
    config = settings(tmp_path)
    fixture = RuntimeFixture()
    gateway = RuntimeGateway(config, transport=httpx.MockTransport(fixture))
    bridge = RuntimeDiscordBridge(config, gateway, BridgeLedger(config))
    sent = []
    async def send(text): sent.append(text)
    try:
        await bridge.handle(event(**updates), send)
        assert not sent and not fixture.calls
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_receipt_progress_final_duplicate_restart_and_own_reply_context(tmp_path):
    config = settings(tmp_path)
    fixture = RuntimeFixture(pending=1)
    gateway = RuntimeGateway(config, transport=httpx.MockTransport(fixture))
    ledger = BridgeLedger(config)
    bridge = RuntimeDiscordBridge(config, gateway, ledger, progress_seconds=0.01)
    sent = []
    async def send(text):
        sent.append(text)
        return 100 + len(sent)
    try:
        await asyncio.gather(bridge.handle(event(), send), bridge.handle(event(), send))
        assert sent == [RECEIPT, PROGRESS, "わたしは公開資料を確認しました．"]
        before = len(fixture.calls)
        restarted = RuntimeDiscordBridge(config, gateway, BridgeLedger(config))
        await restarted.handle(event(), send)
        assert len(fixture.calls) == before
        await restarted.handle(event(message_id=2, mentioned=False, reply_to=103, content="それはどう使いますか"), send)
        plan = fixture.calls[before][1]["query"]
        chat = fixture.calls[before + 1][1]
        assert "前の公開回答" in plan and "公開資料を確認" in plan and "それはどう使いますか" in plan
        assert chat["messages"][-1]["content"] == plan
        assert chat["messages"][1] == {"role": "assistant", "content": "わたしは公開資料を確認しました．"}
        before = len(fixture.calls)
        await restarted.handle(event(message_id=3, user_id=31, mentioned=False, reply_to=103), send)
        assert len(fixture.calls) == before
        with sqlite3.connect(ledger.path) as db:
            assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 2
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_cancellation_cleans_up_gateway_and_does_not_publish_or_replay(tmp_path):
    config = settings(tmp_path)
    started, cancelled = asyncio.Event(), asyncio.Event()
    class SlowGateway:
        async def answer(self, *args):
            started.set()
            try: await asyncio.sleep(60)
            finally: cancelled.set()
    bridge = RuntimeDiscordBridge(config, SlowGateway(), BridgeLedger(config))
    sent = []
    async def send(text): sent.append(text)
    task = asyncio.create_task(bridge.handle(event(), send))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert cancelled.is_set() and sent == [RECEIPT] and bridge.inflight == 0
    await bridge.handle(event(), send)
    assert sent == [RECEIPT]


@pytest.mark.asyncio
async def test_secret_rejection_and_terminal_failure_never_pretend_success(tmp_path, caplog):
    token = "fixture-discord-token-private"
    fixture = RuntimeFixture()
    gateway = RuntimeGateway(settings(tmp_path), transport=httpx.MockTransport(fixture), secrets=(token,))
    try:
        with pytest.raises(BridgeError): await gateway.answer(token)
        assert not fixture.calls and token not in caplog.text
    finally:
        await gateway.aclose()
    class BrokenGateway:
        async def answer(self, *args): raise RuntimeError(token)
    config = settings(tmp_path / "failure")
    bridge = RuntimeDiscordBridge(config, BrokenGateway(), BridgeLedger(config))
    sent = []
    async def send(text): sent.append(text)
    await bridge.handle(event(), send)
    assert sent[0] == RECEIPT and "失敗" in sent[-1] and token not in str(sent) + caplog.text


def test_cli_check_is_inert_and_run_requires_connect(tmp_path, capsys, monkeypatch):
    config = tmp_path / "bridge.toml"
    config.write_text('application_id=10\nguild_id=11\nallowed_channel_ids=[20]\nallowed_user_ids=[30]\n'
        + 'state_dir=' + json.dumps(str(tmp_path / "private")) + '\n', encoding="utf-8")
    monkeypatch.delenv("EPHY_DISCORD_BOT_TOKEN", raising=False)
    assert main(["discord-runtime", "check", "--config", str(config)]) == 0
    assert not (tmp_path / "private").exists()
    assert main(["discord-runtime", "run", "--config", str(config)]) == 1
    assert "discord_runtime_run_requires_connect" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_active_stream_outlives_request_timeout_without_overall_deadline(tmp_path):
    class ActiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raw = final_stream().content
            for offset in range(0, len(raw), 100):
                await asyncio.sleep(0.03)
                yield raw[offset:offset + 100]
    async def handler(request):
        if request.url.path.endswith("plan"):
            return httpx.Response(200, json={"decision": "allow", "plan_id": "plan1"})
        assert request.extensions["timeout"]["read"] == 600
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ActiveStream())
    gateway = RuntimeGateway(settings(tmp_path, request_timeout_seconds=0.1), transport=httpx.MockTransport(handler))
    try:
        started = time.monotonic()
        assert await gateway.answer("公開の質問") == "わたしは公開資料を確認しました．"
        assert time.monotonic() - started > 0.1
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_sdk_boundary_uses_inert_mentions_no_history_and_explicit_content_intent(tmp_path, monkeypatch):
    from ephy_worker import discord_runtime

    sent, constructed, closed = [], [], []
    inert = object()
    class FakeGateway:
        def __init__(self, *args, **kwargs): pass
        async def answer(self, query, previous):
            assert query == "公開の質問" and previous is None
            return "わたしの回答です．"
        async def aclose(self): closed.append("gateway")
    class FakeClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.application_id = kwargs["application_id"]
            self.user = SimpleNamespace(id=10)
        def event(self, callback):
            setattr(self, callback.__name__, callback)
            return callback
        async def start(self, token, **kwargs):
            assert token == "fixture-discord-token-private" and kwargs == {"reconnect": True}
            await self.on_ready()
            async def reply(text, **options):
                assert options == {"mention_author": False, "allowed_mentions": inert, "suppress_embeds": True}
                sent.append(text)
                return SimpleNamespace(id=100 + len(sent))
            message = SimpleNamespace(id=1, guild=SimpleNamespace(id=11), channel=SimpleNamespace(id=20),
                author=SimpleNamespace(id=30, bot=False), content="<@10> 公開の質問",
                mentions=[self.user], reference=None, webhook_id=None, mention_everyone=False,
                is_system=lambda: False, reply=reply)
            await self.on_message(message)
        async def close(self): closed.append("discord")
    sdk = SimpleNamespace(Client=FakeClient, Intents=SimpleNamespace(none=lambda: SimpleNamespace()),
        AllowedMentions=SimpleNamespace(none=lambda: inert), LoginFailure=type("LoginFailure", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "discord", sdk)
    monkeypatch.setattr(discord_runtime, "RuntimeGateway", FakeGateway)
    monkeypatch.setenv("EPHY_DISCORD_BOT_TOKEN", "fixture-discord-token-private")
    assert await run_discord(settings(tmp_path, read_message_content=True)) == 0
    assert sent == [RECEIPT, "わたしの回答です．"]
    assert constructed[0]["max_messages"] is None and constructed[0]["allowed_mentions"] is inert
    assert constructed[0]["intents"].message_content is True
    assert closed == ["discord", "gateway"]
