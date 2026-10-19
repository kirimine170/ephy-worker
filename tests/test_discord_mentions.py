"""Offline SDK mention routing. No token file, Discord connection or model workload."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from pydantic import ValidationError

from ephy_worker.discord_adapter import Answer, DiscordAdapter, DiscordSettings, Ledger
from ephy_worker.discord_cli import build_client


@pytest.fixture
def settings(tmp_path):
    return DiscordSettings(
        application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture",
        output_dir=tmp_path / "results", state_dir=tmp_path / "state",
    )


def message(number=500, content="<@900> public fixture", **updates):
    data = {
        "id": number, "content": content, "guild": SimpleNamespace(id=200),
        "channel": SimpleNamespace(id=300, send=AsyncMock()),
        "author": SimpleNamespace(id=400, bot=False), "webhook_id": None,
        "type": discord.MessageType.default, "reference": None,
        "mention_everyone": False, "mentions": [SimpleNamespace(id=900)],
    }
    data.update(updates)
    return SimpleNamespace(**data)


def client_for(settings, adapter):
    client, tree, guild = build_client(settings, adapter, "fixture-bot-token")
    client._connection.user = SimpleNamespace(id=900, bot=True)
    assert tree is None and guild is None
    return client


async def test_real_mention_executes_once_with_bounded_inert_public_reply(settings):
    calls = []
    async def execute(question):
        assert item.channel.send.await_args_list[0].args[0] == "分かりました，調べてみますね．"
        calls.append(question)
        return Answer("completed", "@everyone " + "ghp_" + "F" * 30)
    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    client = client_for(settings, adapter)
    item = message()
    try:
        assert client.intents.guilds and client.intents.guild_messages
        assert not client.intents.message_content and not client.intents.members
        assert not client.intents.dm_messages
        assert client._connection.max_messages is None
        await client.on_message(item)
        await client.on_message(item)
        assert calls == ["public fixture"]
        assert item.channel.send.await_count == 2
        sent = " ".join(call.args[0] for call in item.channel.send.await_args_list)
        assert "ghp_" not in sent and "受付ID" not in sent and "状態:" not in sent
        for call in item.channel.send.await_args_list:
            assert call.kwargs["allowed_mentions"].to_dict()["parse"] == []
            assert call.kwargs["suppress_embeds"]
            assert "ephemeral" not in call.kwargs
    finally:
        await client.close()
        await adapter.close()


@pytest.mark.parametrize("updates", [
    {"content": "ordinary text", "mentions": []},
    {"content": "@ephy public fixture", "mentions": []},
    {"content": "<@&900> public fixture", "mentions": []},
    {"content": "@everyone public fixture", "mention_everyone": True, "mentions": []},
    {"content": "reply text", "type": discord.MessageType.reply, "reference": object()},
    {"content": "<@900> explicit reply", "reference": object()},
    {"content": "`<@900> quoted mention`"},
    {"content": "<@901> wrong bot"},
    {"content": "<@900> missing parsed mention", "mentions": []},
    {"guild": None},
    {"guild": SimpleNamespace(id=201)},
    {"author": SimpleNamespace(id=401, bot=False)},
    {"author": SimpleNamespace(id=400, bot=True)},
    {"author": SimpleNamespace(id=900, bot=False)},
    {"webhook_id": 700},
])
async def test_false_mentions_and_unauthorized_sources_never_execute_or_reply(settings, updates):
    async def forbidden(_):
        raise AssertionError("must not execute")
    adapter = DiscordAdapter(settings, Ledger(settings), forbidden)
    client = client_for(settings, adapter)
    item = message(**updates)
    try:
        await client.on_message(item)
        item.channel.send.assert_not_awaited()
        assert adapter.ledger.db.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)
    finally:
        await client.close()
        await adapter.close()


@pytest.mark.parametrize("read_content", [False, True])
async def test_discord_read_capability_does_not_expand_work_or_reply_scope(settings, read_content):
    settings = settings.model_copy(update={"read_message_content": read_content})
    async def forbidden(_):
        raise AssertionError("read scope must not authorize a task")
    adapter = DiscordAdapter(settings, Ledger(settings), forbidden)
    client = client_for(settings, adapter)
    outside = message(channel=SimpleNamespace(id=301, send=AsyncMock()))
    try:
        assert client.intents.message_content is read_content
        await client.on_message(outside)
        outside.channel.send.assert_not_awaited()
        assert adapter.active is None
    finally:
        await client.close()
        await adapter.close()


@pytest.mark.parametrize("syntax", ["<@900> cancel", "<@!900> cancel 500", "<@900> CANCEL 500"])
async def test_mention_cancel_reuses_owner_and_duplicate_cancel_guards(settings, syntax):
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def execute(question):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()
    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    client = client_for(settings, adapter)
    task = asyncio.create_task(client.on_message(message()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await client.on_message(message(501, syntax))
        await client.on_message(message(502, syntax))
        await asyncio.wait_for(task, 5)
        assert cleaned.is_set()
        assert adapter.ledger.db.execute("SELECT status FROM requests WHERE id=500").fetchone() == (
            "cancelled",
        )
    finally:
        await client.close()
        await adapter.close()
        await asyncio.gather(task, return_exceptions=True)


def test_mention_requires_one_task_channel_and_explicit_boolean(settings):
    payload = settings.model_dump()
    with pytest.raises(ValidationError):
        DiscordSettings.model_validate({**payload, "allowed_channel_ids": [300, 301]})
    with pytest.raises(ValidationError):
        DiscordSettings.model_validate({**payload, "read_message_content": "false"})


def test_mention_check_and_register_refusal_do_not_resolve_token(tmp_path, monkeypatch, capsys):
    import json

    import yaml

    from ephy_worker.cli import main

    worker = tmp_path / "worker.yaml"
    worker.write_text(yaml.safe_dump({
        "search": {"base_url": "http://127.0.0.1:9", "engine": "fixture"},
        "model_profiles": {"fixture": {
            "family": "qwen", "base_url": "http://127.0.0.1:9/v1", "model_id": "fixture",
            "output_mode": "tool",
        }},
    }), encoding="utf-8")
    config = tmp_path / "discord.toml"
    config.write_text(
        'application_id=100\nguild_id=200\nallowed_channel_ids=[300]\nallowed_user_ids=[400]\n'
        'worker_config="worker.yaml"\nprofile="fixture"\noutput_dir="results"\nstate_dir="state"\n',
        encoding="utf-8",
    )

    def no_token(*args):
        raise AssertionError("offline mention preflight must not resolve token")

    monkeypatch.setattr(DiscordSettings, "token", no_token)
    assert main(["discord", "check", "--config", str(config)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["entrypoint"] == "mention" and result["intents"] == 513
    assert result["network"] is False and result["unmentioned_content_enabled"] is False
    assert main(["discord", "register", "--config", str(config), "--connect"]) == 1
    assert "registration_not_required_for_mentions" in capsys.readouterr().err
