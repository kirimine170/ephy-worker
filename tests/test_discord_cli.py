"""SDK callbacks and real executor lifecycle use synthetic offline fixtures only."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from ephy_worker.cli import main
from ephy_worker.config import load_config
from ephy_worker.discord_adapter import Answer, DiscordAdapter, DiscordError, DiscordSettings, Ledger
from ephy_worker.discord_cli import build_client, make_executor, run_command


def configuration(tmp_path):
    worker = tmp_path / "worker.yaml"
    worker.write_text(yaml.safe_dump({
        "search": {"base_url": "http://127.0.0.1:9", "engine": "fixture"},
        "model_profiles": {"fixture": {
            "family": "qwen", "base_url": "http://127.0.0.1:9/v1",
            "model_id": "fixture", "output_mode": "tool",
        }},
    }), encoding="utf-8")
    discord_config = tmp_path / "discord.toml"
    discord_config.write_text(
        'entrypoint="slash"\napplication_id=100\nguild_id=200\nallowed_channel_ids=[300]\nallowed_user_ids=[400]\n'
        'worker_config="worker.yaml"\nprofile="fixture"\noutput_dir="results"\nstate_dir="state"\n',
        encoding="utf-8",
    )
    return discord_config


def test_cli_offline_check_and_explicit_connect_guard(tmp_path, monkeypatch, capsys):
    path = configuration(tmp_path)
    monkeypatch.delenv("EPHY_DISCORD_BOT_TOKEN", raising=False)
    def no_client(*args):
        raise AssertionError("offline command must not build a Discord client")
    monkeypatch.setattr("ephy_worker.discord_cli.build_client", no_client)
    assert main(["discord", "check", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["network"] is False
    for command in ("run", "register"):
        assert main(["discord", command, "--config", str(path)]) == 1
        assert "discord_explicit_connect_required" in capsys.readouterr().err
    assert not (tmp_path / "state").exists() and not (tmp_path / "results").exists()


@pytest.mark.parametrize("addition", [
    'token="private-marker"\n', 'allowed_role_ids=[9]\n',
])
def test_configuration_rejects_unknown_fields_without_secret_echo(tmp_path, capsys, addition):
    path = configuration(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + addition, encoding="utf-8")
    assert main(["discord", "check", "--config", str(path)]) == 1
    error = capsys.readouterr().err
    assert "discord_config_invalid" in error and "private-marker" not in error


def test_config_paths_ids_and_token_separation(tmp_path, monkeypatch):
    path = configuration(tmp_path)
    settings = DiscordSettings.load(path)
    assert settings.state_dir == (tmp_path / "state").resolve()
    monkeypatch.setenv(settings.token_env, "Bot fixture-secret")
    assert settings.token() == "fixture-secret"
    for replacement in ('allowed_user_ids=[]', 'allowed_user_ids=[true]'):
        path.write_text(
            path.read_text(encoding="utf-8").replace('allowed_user_ids=[400]', replacement), encoding="utf-8"
        )
        with pytest.raises(DiscordError):
            DiscordSettings.load(path)
        configuration(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace(
        'state_dir="state"', 'state_dir="' + str(Path(__file__).parents[1]).replace("\\", "/") + '"'
    ), encoding="utf-8")
    with pytest.raises(DiscordError):
        DiscordSettings.load(path)


async def test_real_sdk_callbacks_no_message_intents_or_mentions(tmp_path):
    import discord
    path = configuration(tmp_path)
    settings = DiscordSettings.load(path)
    calls = []
    async def execute(question):
        calls.append(question)
        return Answer("completed", "@everyone fixture-secret")
    adapter = DiscordAdapter(settings, Ledger(settings), execute)
    client, tree, guild = build_client(settings, adapter, "fixture-secret")
    try:
        assert client.intents.value == 0 and not hasattr(client, "on_message")
        assert client.allowed_mentions.to_dict()["parse"] == []
        assert client.http.max_ratelimit_timeout == 30
        commands = {command.name: command for command in tree.get_commands(guild=guild)}
        assert set(commands) == {"ephy-research", "ephy-cancel"}
        response, followup = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), AsyncMock()
        interaction = SimpleNamespace(
            id=500, application_id=100, guild_id=200, channel_id=300,
            user=SimpleNamespace(id=400, bot=False), response=response, followup=followup,
        )
        await commands["ephy-research"].callback(interaction, "public fixture")
        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert calls == ["public fixture"]
        for call in followup.send.await_args_list:
            assert call.kwargs["ephemeral"] and call.kwargs["suppress_embeds"]
            assert call.kwargs["allowed_mentions"].to_dict()["parse"] == []
            assert "fixture-secret" not in call.args[0]
        # SDK rate limit exhaustion must not start an executor or trigger application retry.
        interaction.id = 501
        interaction.followup.send.side_effect = discord.RateLimited(31)
        await commands["ephy-research"].callback(interaction, "public second fixture")
        assert calls == ["public fixture"]
        assert adapter.ledger.db.execute("SELECT status FROM requests WHERE id=501").fetchone() == (
            "delivery_failed",
        )
    finally:
        await client.close()
        await adapter.close()


async def test_real_research_executor_lifecycle_with_fixture_stage(tmp_path, monkeypatch):
    from ephy_worker.research import ResearchExecutor
    settings = DiscordSettings.load(configuration(tmp_path))
    async def fixture_stage(self, urls):
        self.report.activity_summary = "synthetic offline lifecycle"
    monkeypatch.setattr(ResearchExecutor, "_execute", fixture_stage)
    answer = await make_executor(settings, load_config(settings.worker_config))("public fixture")
    report_path = settings.output_dir / answer.job_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["question"] == "public fixture"
    assert report["state"] == answer.state and report["finished_at"]
    assert report["metrics"]["requests"] == {
        "search_requests": 0, "fetch_requests": 0, "model_requests": 0,
    }


async def test_real_research_executor_cancel_saves_artifacts(tmp_path, monkeypatch):
    from ephy_worker.research import ResearchExecutor
    settings = DiscordSettings.load(configuration(tmp_path))
    entered = asyncio.Event()
    async def paused(self, urls):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(ResearchExecutor, "_execute", paused)
    task = asyncio.create_task(make_executor(settings, load_config(settings.worker_config))("public fixture"))
    await entered.wait()
    task.cancel()
    answer = await task
    report = json.loads((settings.output_dir / answer.job_id / "report.json").read_text(encoding="utf-8"))
    assert report["state"] == "cancelled" and answer.state == "cancelled"


async def test_registration_refuses_unrelated_commands_and_never_connects_gateway(tmp_path, monkeypatch):
    settings_path = configuration(tmp_path)
    client = AsyncMock()
    client.application_info.return_value = SimpleNamespace(id=100)
    client.__aenter__.return_value = client
    tree = SimpleNamespace(
        fetch_commands=AsyncMock(return_value=[SimpleNamespace(name="other-project-command")]),
        sync=AsyncMock(),
    )
    monkeypatch.setenv("EPHY_DISCORD_BOT_TOKEN", "fixture-secret")
    monkeypatch.setattr(
        "ephy_worker.discord_cli.build_client", lambda *args: (client, tree, SimpleNamespace(id=200))
    )
    with pytest.raises(DiscordError, match="registration_would_replace_other_commands"):
        await run_command(SimpleNamespace(discord_command="register", config=settings_path, connect=True))
    tree.sync.assert_not_awaited()
    client.connect.assert_not_awaited()


def synthetic_secret(kind):
    # Generated fixture strings only; these are never valid configured credentials.
    if kind == "github":
        return "ghp_" + "A" * 32
    if kind == "github_pat":
        return "github_pat_" + "B_" * 20
    if kind == "discord":
        return "A_" * 16 + "." + "B" * 6 + "." + "C_" * 20
    if kind == "tavily":
        return "tvly-dev-" + "D_" * 20
    return "known_bot_" + "K_" * 20


def fake_interaction(number):
    return SimpleNamespace(
        id=number, application_id=100, guild_id=200, channel_id=300,
        user=SimpleNamespace(id=400, bot=False),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize("kind", ["github", "github_pat", "discord", "tavily", "known"])
async def test_sdk_callback_redacts_secret_after_report_markdown(tmp_path, monkeypatch, kind):
    import re

    from ephy_worker.research import ResearchExecutor

    settings = DiscordSettings.load(configuration(tmp_path))
    secret = synthetic_secret(kind)
    bot_token = secret if kind == "known" else "fixture-bot-token"

    async def fixture_stage(self, urls):
        self.report.gaps = ["synthetic citation " + secret]

    monkeypatch.setattr(ResearchExecutor, "_execute", fixture_stage)
    adapter = DiscordAdapter(
        settings, Ledger(settings), make_executor(settings, load_config(settings.worker_config))
    )
    client, tree, guild = build_client(settings, adapter, bot_token)
    interaction = fake_interaction(700)
    try:
        command = tree.get_command("ephy-research", guild=guild)
        await command.callback(interaction, "public fixture")
        sent = "\n".join(call.args[0] for call in interaction.followup.send.await_args_list)
        decoded = re.sub(r"\\([_\\\[\]])", r"\1", sent)
        assert secret not in decoded and secret not in sent
        assert "[redacted]" in decoded
    finally:
        await client.close()
        await adapter.close()


@pytest.mark.parametrize("kind", ["github", "github_pat", "discord", "tavily", "known"])
async def test_report_secrets_removed_before_markdown_projection(tmp_path, monkeypatch, kind):
    from ephy_worker import report as report_module
    from ephy_worker.research import ResearchExecutor

    settings = DiscordSettings.load(configuration(tmp_path))
    secret = synthetic_secret(kind)
    real_render = report_module.render_markdown
    seen = []

    async def fixture_stage(self, urls):
        self.report.gaps = ["synthetic citation " + secret]

    def checked_render(report):
        seen.append(report)
        if len(seen) == 2:  # First render saves the local canonical artifact; second is Discord projection.
            assert secret not in json.dumps(report.model_dump(), ensure_ascii=False)
        return real_render(report)

    monkeypatch.setattr(ResearchExecutor, "_execute", fixture_stage)
    monkeypatch.setattr(report_module, "render_markdown", checked_render)
    await make_executor(
        settings, load_config(settings.worker_config), known_secrets=(secret,) if kind == "known" else ()
    )("public fixture")
    assert len(seen) == 2
    assert secret in json.dumps(seen[0].model_dump(), ensure_ascii=False)


@pytest.mark.parametrize("action", [
    "cancel_twice", "cancel_then_shutdown", "shutdown_twice", "deadline_after_cancel",
])
async def test_sdk_repeated_stop_preserves_real_executor_cleanup_and_artifacts(
    tmp_path, monkeypatch, action
):
    from ephy_worker.research import ResearchExecutor

    settings = DiscordSettings.load(configuration(tmp_path)).model_copy(update={"timeout_seconds": 1})
    entered, cleanup_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup_interrupted, cleanup_finished = asyncio.Event(), asyncio.Event()
    workers = []

    async def paused(self, urls):
        workers.append(self)
        original_close = self.fetcher.aclose

        async def delayed_close():
            await original_close()
            cleanup_entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cleanup_interrupted.set()
                raise
            cleanup_finished.set()

        self.fetcher.aclose = delayed_close
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(ResearchExecutor, "_execute", paused)
    adapter = DiscordAdapter(
        settings, Ledger(settings), make_executor(settings, load_config(settings.worker_config))
    )
    client, tree, guild = build_client(settings, adapter, "fixture-bot-token")
    research = tree.get_command("ephy-research", guild=guild)
    cancel = tree.get_command("ephy-cancel", guild=guild)
    task = asyncio.create_task(research.callback(fake_interaction(800), "public fixture"))
    close_tasks = []
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if action == "deadline_after_cancel":
            await asyncio.sleep(0.85)
        if action == "shutdown_twice":
            close_tasks.append(asyncio.create_task(adapter.close()))
        else:
            await cancel.callback(fake_interaction(801), "800")
        await asyncio.wait_for(cleanup_entered.wait(), timeout=5)

        if action == "cancel_twice":
            await cancel.callback(fake_interaction(802), "800")
        elif action in {"cancel_then_shutdown", "shutdown_twice"}:
            close_tasks.append(asyncio.create_task(adapter.close()))
        else:
            # Cross the former adapter deadline while still within the executor's cleanup timeout.
            await asyncio.sleep(0.25)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        if close_tasks:
            await asyncio.wait_for(asyncio.gather(*close_tasks), timeout=5)

        assert cleanup_finished.is_set() and not cleanup_interrupted.is_set()
        directory = workers[0].store.directory
        saved = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        assert saved["state"] == status["state"] == "cancelled"
        assert saved["finished_at"]
        assert json.loads((directory / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])[
            "type"
        ] == "finished"
        if close_tasks:
            ledger = Ledger(settings)
        else:
            ledger = adapter.ledger
        try:
            assert ledger.db.execute("SELECT status FROM requests WHERE id=800").fetchone() == (
                "cancelled",
            )
        finally:
            if close_tasks:
                ledger.close()
    finally:
        release.set()
        await client.close()
        if not close_tasks:
            await adapter.close()
        await asyncio.gather(task, *close_tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ["github", "github_pat", "discord", "tavily", "known"])
def test_secret_redaction_precedes_utf16_message_boundaries(kind):
    import re

    from ephy_worker.discord_adapter import split_reply

    secret = synthetic_secret(kind)
    escaped = secret.replace("_", r"\_")
    parts = split_reply("p" * 95 + escaped + " tail", limit=100, known_secrets=(secret,))
    combined = "".join(parts)
    decoded = re.sub(r"\\([_\\\[\]])", r"\1", combined)
    assert secret not in decoded and "[redacted]" in decoded
    assert all(len(part.encode("utf-16-le")) // 2 <= 100 for part in parts)
