"""Opt-in Discord CLI. Check is offline; run/register require --connect."""

import asyncio
import json
import logging

from .discord_adapter import (
    Answer,
    DiscordAdapter,
    DiscordError,
    DiscordSettings,
    Ledger,
    Request,
    redact_secrets,
)


def build_client(settings, adapter, token):
    try:
        import discord
        from discord import app_commands
    except ImportError:
        raise DiscordError("discord_extra_required") from None

    if settings.entrypoint == "mention":
        return build_mention_client(settings, adapter, token)

    class SDKReply:
        def __init__(self, interaction):
            self.interaction = interaction

        async def defer(self):
            async with asyncio.timeout(2.5):
                await self.interaction.response.defer(ephemeral=True, thinking=True)

        async def send(self, text):
            async with asyncio.timeout(40):
                await self.interaction.followup.send(
                    redact_secrets(text, (token,)), ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True, wait=True,
                )

        async def reject(self, text):
            async with asyncio.timeout(2.5):
                await self.interaction.response.send_message(
                    text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True,
                )

    def request(interaction):
        return Request(
            interaction.id, interaction.application_id, interaction.guild_id, interaction.channel_id,
            interaction.user.id, interaction.user.bot,
        )

    class Client(discord.Client):
        async def on_error(self, event, *args, **kwargs):
            # The SDK default prints tracebacks and may expose interaction tokens.
            logging.getLogger(__name__).error("discord_event_failed")

    client = Client(
        intents=discord.Intents.none(), application_id=settings.application_id, max_messages=None,
        allowed_mentions=discord.AllowedMentions.none(), max_ratelimit_timeout=30.0,
    )
    tree = app_commands.CommandTree(client)
    guild = discord.Object(id=settings.guild_id)

    @tree.command(name="ephy-research", description="公開情報の質問を限定Workerで調査します", guild=guild)
    @app_commands.describe(question="公開情報の質問．秘密・私的会話を入力しないでください")
    async def research(interaction: discord.Interaction, question: app_commands.Range[str, 1, 1000]):
        await adapter.ask(request(interaction), SDKReply(interaction), question)

    @tree.command(name="ephy-cancel", description="自分がこの場所で開始した調査を取り消します", guild=guild)
    async def cancel(interaction: discord.Interaction, request_id: str):
        await adapter.cancel(request(interaction), SDKReply(interaction), request_id)

    @tree.error
    async def command_error(interaction, error):
        # Do not echo arguments, exception bodies, tokens, or local filesystem paths.
        await adapter._reject(SDKReply(interaction), "コマンドを処理できませんでした．")

    return client, tree, guild


def build_mention_client(settings, adapter, token):
    import re

    import discord

    class MessageReply:
        def __init__(self, message):
            self.message = message

        async def defer(self):
            # Ordinary messages do not have the interaction acknowledgement deadline.
            return None

        async def send(self, text):
            if (
                self.message.guild is None or self.message.guild.id != settings.guild_id
                or self.message.channel.id not in settings.allowed_channel_ids
            ):
                raise DiscordError("discord_reply_channel_not_allowed")
            async with asyncio.timeout(40):
                await self.message.channel.send(
                    redact_secrets(text, (token,)), allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )

        async def reject(self, text):
            await self.send(text)

    class MentionClient(discord.Client):
        async def on_error(self, event, *args, **kwargs):
            logging.getLogger(__name__).error("discord_event_failed")

        async def on_message(self, message):
            bot = self.user
            if (
                bot is None or message.guild is None
                or message.guild.id != settings.guild_id
                or message.channel.id not in settings.allowed_channel_ids
                or message.author.bot or message.author.id == bot.id
                or message.webhook_id is not None
                or message.type != discord.MessageType.default
                or message.reference is not None or message.mention_everyone
            ):
                return
            request = Request(
                message.id, settings.application_id, message.guild.id, message.channel.id,
                message.author.id, message.author.bot,
            )
            if not adapter.allowed(request):
                return
            # Require the real bot user in both the parsed mention list and an explicit leading token.
            if not any(user.id == bot.id for user in message.mentions) or not isinstance(message.content, str):
                return
            match = re.fullmatch(r"\s*<@!?" + str(bot.id) + r">(?:\s+(.*))?\s*", message.content, re.DOTALL)
            if match is None:
                return
            body = (match.group(1) or "").strip()
            reply = MessageReply(message)
            words = body.split(maxsplit=1)
            if words and words[0].casefold() == "cancel":
                cancel_match = re.fullmatch(r"cancel(?:\s+([0-9]+))?", body, re.IGNORECASE)
                if cancel_match is None:
                    target = ""  # Existing cancel boundary records/rejects malformed cancel requests.
                elif cancel_match.group(1):
                    target = cancel_match.group(1)
                else:
                    target = str(adapter.active[0].id) if adapter.active else ""
                await adapter.cancel(request, reply, target)
            else:
                await adapter.ask(request, reply, body)

    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = settings.read_message_content
    client = MentionClient(
        intents=intents, application_id=settings.application_id, max_messages=None,
        allowed_mentions=discord.AllowedMentions.none(), max_ratelimit_timeout=30.0,
    )
    return client, None, None


def make_executor(settings, config, *, known_secrets: tuple[str, ...] = ()):
    if config.answer_policy is not None and config.answer_policy.enabled:
        from .answer_routing import make_routed_executor

        return make_routed_executor(settings, config, known_secrets=known_secrets)

    async def execute(question):
        import time

        from .budget import Budget
        from .discord_answer import AnswerTimeoutFailure, compose_answer
        from .research import ResearchExecutor
        from .store import JobStore

        started = time.monotonic()
        total = min(settings.timeout_seconds, config.limits.job_seconds)
        limits = config.limits
        if total >= 60:
            reserve = min(25, total / 10)
            limits = limits.model_copy(update={
                "job_seconds": total - reserve,
                "model_requests": max(1, limits.model_requests - 1),
            })
        research_config = config.model_copy(update={"limits": limits})
        store = JobStore(settings.output_dir)
        executor = ResearchExecutor(research_config, settings.profile, store)
        report = await executor.run(question)
        answer_budget = Budget(config.limits.model_copy(update={"job_seconds": total}))
        answer_budget.started = started
        answer_budget.counts["model_requests"] = executor.budget.counts["model_requests"]
        try:
            text = await compose_answer(
                report, config.model_profiles[settings.profile], answer_budget, store,
                known_secrets=known_secrets,
            )
        except asyncio.CancelledError:
            # Research already finalized its artifacts；keep their ledger association．
            return Answer("cancelled", "調査を取り消しました．", report.job_id)
        except AnswerTimeoutFailure as exc:
            # Preserve the timeout notice and failed state without exposing cleanup details．
            return Answer("failed", exc.text, report.job_id)
        return Answer(report.state, text, report.job_id)
    return execute


async def run_command(args):
    settings = DiscordSettings.load(args.config)
    from .config import load_config

    config = load_config(settings.worker_config)
    if settings.profile not in config.model_profiles or config.search is None:
        raise DiscordError("discord_research_profile_or_search_missing")
    if args.discord_command == "check":
        intents = (513 | (32768 if settings.read_message_content else 0)) if settings.entrypoint == "mention" else 0
        print(json.dumps({
            "ok": True, "network": False, "executor": "research", "entrypoint": settings.entrypoint,
            "intents": intents, "unmentioned_content_enabled": settings.read_message_content,
        }))
        return 0
    if args.discord_command == "register" and settings.entrypoint == "mention":
        raise DiscordError("discord_registration_not_required_for_mentions")
    if not args.connect:
        raise DiscordError("discord_explicit_connect_required")
    # Resolve fixed executor configuration before any Discord connection.
    config.model_profiles[settings.profile].resolve()
    config.search.endpoint()
    config.search.resolve_api_key()
    token = settings.token()
    # SDK debug logging includes HTTP routes and Gateway payloads with tokens.
    logging.getLogger("discord").setLevel(logging.CRITICAL + 1)
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL + 1)
    ledger = Ledger(settings)
    adapter = DiscordAdapter(
        settings, ledger, make_executor(settings, config, known_secrets=(token,)), known_secrets=(token,)
    )
    try:
        client, tree, guild = build_client(settings, adapter, token)
        async with client:
            await client.login(token)
            if (await client.application_info()).id != settings.application_id:
                raise DiscordError("discord_application_identity_mismatch")
            if args.discord_command == "register":
                existing = await tree.fetch_commands(guild=guild)
                expected = {"ephy-research", "ephy-cancel"}
                if any(command.name not in expected for command in existing):
                    raise DiscordError("discord_registration_would_replace_other_commands")
                await tree.sync(guild=guild)
                print(json.dumps({"registered": sorted(expected)}))
                return 0
            await client.connect(reconnect=True)  # No automatic command registration.
    finally:
        await adapter.close()
    return 0
