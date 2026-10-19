"""Offline boundary and lifecycle checks; no Discord credentials or model requests."""

import asyncio
from dataclasses import replace

import pytest

from ephy_worker.discord_adapter import (
    Answer,
    DiscordAdapter,
    DiscordError,
    DiscordSettings,
    Ledger,
    Request,
    split_reply,
)


@pytest.fixture
def settings(tmp_path):
    return DiscordSettings(
        entrypoint="slash", application_id=100, guild_id=200, allowed_channel_ids=[300, 301],
        allowed_user_ids=[400, 401], worker_config=tmp_path / "worker.yaml",
        profile="fixture", output_dir=tmp_path / "results", state_dir=tmp_path / "state",
        timeout_seconds=1,
    )


def request(number=500, **kwargs):
    return replace(Request(number, 100, 200, 300, 400), **kwargs)


class FakeReply:
    def __init__(self, *, fail_defer=False, fail_send=False):
        self.events = []
        self.fail_defer, self.fail_send = fail_defer, fail_send

    async def defer(self):
        self.events.append(("defer", ""))
        if self.fail_defer:
            raise RuntimeError("untrusted exception body")

    async def send(self, text):
        if self.fail_send:
            raise RuntimeError("untrusted exception body")
        self.events.append(("send", text))

    async def reject(self, text):
        self.events.append(("reject", text))


@pytest.mark.parametrize("changes", [
    {"application_id": 999}, {"guild_id": None}, {"guild_id": 999}, {"channel_id": 999},
    {"user_id": 999}, {"bot": True}, {"id": -1},
])
async def test_fail_closed_allowlists_and_bots(settings, changes):
    calls = []
    async def execute(question):
        calls.append(question)
        return Answer("completed", "fixture")
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    reply = FakeReply()
    await adapter.ask(request(**changes), reply, "public fixture")
    assert not calls
    assert reply.events[0][0] == "reject"
    assert ledger.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    await adapter.close()


async def test_ack_before_execute_and_persistent_dedup(settings):
    reply, calls = FakeReply(), []
    async def execute(question):
        assert [event[0] for event in reply.events] == ["defer", "send"]
        calls.append(question)
        return Answer("completed", "日本語😀" * 2200, "fixture-job")
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    await adapter.ask(request(), reply, "public fixture")
    await adapter.ask(request(), FakeReply(), "public fixture")
    assert calls == ["public fixture"]
    assert len(reply.events) == 7  # defer + receipt + five bounded result parts
    assert "出力上限" in reply.events[-1][1]
    assert all(len(text.encode("utf-16-le")) // 2 <= 1800 for kind, text in reply.events[2:])
    dump = "\n".join(ledger.db.iterdump())
    assert "public fixture" not in dump and "日本語" not in dump
    assert ledger.db.execute("SELECT status, job FROM requests").fetchone() == ("completed", "fixture-job")
    await adapter.close()
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    duplicate = FakeReply()
    await adapter.ask(request(), duplicate, "public fixture")
    assert len(calls) == 1 and not duplicate.events
    await adapter.close()


@pytest.mark.parametrize("question", [
    "", "a" * 1001, "please read C:\\Users\\fixture\\private.txt",
    "A" * 30 + "." + "B" * 6 + "." + "C" * 30,
])
async def test_private_oversize_and_token_input_rejected(settings, question):
    async def forbidden(_):
        raise AssertionError("executor must not run")
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, forbidden)
    reply = FakeReply()
    await adapter.ask(request(), reply, question)
    assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("rejected",)
    assert [event[0] for event in reply.events] == ["reject"]
    await adapter.close()


@pytest.mark.parametrize("failure", ["defer", "send"])
async def test_failed_ack_or_receipt_never_executes_or_retries(settings, failure):
    calls = []
    async def execute(question):
        calls.append(question)
        return Answer("completed", "fixture")
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    reply = FakeReply(fail_defer=failure == "defer", fail_send=failure == "send")
    await adapter.ask(request(), reply, "public fixture")
    await adapter.ask(request(), FakeReply(), "public fixture")
    assert not calls
    assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("delivery_failed",)
    await adapter.close()


async def test_busy_owner_cancel_and_channel_boundary(settings):
    started = asyncio.Event()
    async def execute(question):
        started.set()
        await asyncio.Future()
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    task = asyncio.create_task(adapter.ask(request(), FakeReply(), "public fixture"))
    await started.wait()
    busy = FakeReply()
    await adapter.ask(request(501), busy, "second fixture")
    assert ledger.db.execute("SELECT status FROM requests WHERE id=501").fetchone() == ("busy",)
    for number, changes in [(502, {"user_id": 401}), (503, {"channel_id": 301})]:
        await adapter.cancel(request(number, **changes), FakeReply(), "500")
        assert not task.done()
    await adapter.cancel(request(504), FakeReply(), "500")
    await task
    assert ledger.db.execute("SELECT status FROM requests WHERE id=500").fetchone() == ("cancelled",)
    await adapter.close()


async def test_timeout_even_if_executor_swallows_cancellation(settings):
    async def execute(question):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return Answer("cancelled", "partial fixture", "fixture-job")
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    reply = FakeReply()
    await adapter.ask(request(), reply, "public fixture")
    assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("timed_out",)
    assert "timed_out" in reply.events[-1][1]
    await adapter.close()


async def test_shutdown_cancels_and_does_not_send_new_results(settings):
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def execute(question):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()
    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    reply = FakeReply()
    task = asyncio.create_task(adapter.ask(request(), reply, "public fixture"))
    await started.wait()
    await adapter.close()
    await task
    assert cleaned.is_set()
    assert [event[0] for event in reply.events] == ["defer", "send"]
    ledger = Ledger(settings)
    assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("cancelled",)
    ledger.close()


async def test_executor_exception_does_not_echo_or_store_exception(settings):
    marker = "private-exception-detail"
    async def execute(question):
        raise RuntimeError(marker)
    ledger = Ledger(settings)
    adapter = DiscordAdapter(settings, ledger, execute)
    reply = FakeReply()
    await adapter.ask(request(), reply, "public fixture")
    assert marker not in repr(reply.events) + "\n".join(ledger.db.iterdump())
    assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("executor_failed",)
    await adapter.close()


def test_lock_identity_and_interrupted_ledger(settings):
    ledger = Ledger(settings)
    with pytest.raises(DiscordError, match="already_in_use"):
        Ledger(settings)
    assert ledger.claim(request())
    ledger.status(500, "running")
    ledger.close()
    ledger = Ledger(settings)
    assert not ledger.claim(request())
    assert ledger.db.execute("SELECT status FROM requests").fetchone() == ("interrupted",)
    ledger.close()
    with pytest.raises(DiscordError, match="identity_mismatch"):
        Ledger(settings.model_copy(update={"guild_id": 999}))


def test_split_preserves_text_without_overflow_and_redacts_tokens():
    text = "a😀日本語\n" * 400
    parts = split_reply(text)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 1800 for part in parts)
    secret = "A" * 30 + "." + "B" * 6 + "." + "C" * 30
    assert split_reply(secret) == ["[redacted]"]


def test_ledger_capacity_fails_closed(settings):
    ledger = Ledger(settings)
    ledger.db.execute("BEGIN")
    ledger.db.executemany(
        "INSERT INTO requests (id, status) VALUES (?, 'completed')", ((i,) for i in range(1, 10001))
    )
    ledger.db.execute("COMMIT")
    assert not ledger.claim(request(1))
    with pytest.raises(DiscordError, match="ledger_full"):
        ledger.claim(request(10001))
    ledger.close()
