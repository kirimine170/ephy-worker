"""Bounded，read-only Discord observations．Message bodies are evidence，never instructions．"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError

from .budget import BudgetExceeded
from .config import ConfigurationError
from .contracts import (
    MAX_OBSERVE_BYTES,
    ObservationFailure,
    ObserveActivity,
    ObserveChannelCoverage,
    ObserveCoverage,
    ObservedChannel,
    ObservedGuild,
    ObservedMessage,
    ObservedRole,
    ObserveJobInput,
    ObservePayload,
    ObserveSnapshot,
    canonical_json,
)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000


class ObservationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamp must be aware")
    return result.astimezone(UTC)


def _snowflake_before(value: datetime) -> str:
    return str(max(1, (int(value.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22))


def activity(messages: list[ObservedMessage], input: ObserveJobInput) -> list[ObserveActivity]:
    result = []
    for days in (7, 30):
        selected = [m for m in messages if m.created_at >= input.observed_before - timedelta(days=days)]
        human = [m for m in selected if not m.author_bot]
        operator = [m for m in human if m.operator]
        other = [m for m in human if not m.operator]
        daily = Counter(m.created_at.astimezone(UTC).date().isoformat() for m in selected)
        result.append(ObserveActivity(
            period_days=days, message_count=len(selected), unique_authors=len({m.author_id for m in selected}),
            human_messages=len(human), bot_messages=len(selected) - len(human),
            operator_messages=len(operator), nonoperator_messages=len(other),
            reply_messages=sum(m.reply_to_message_id is not None for m in selected),
            operator_reply_messages=sum(m.reply_to_message_id is not None for m in operator),
            nonoperator_reply_messages=sum(m.reply_to_message_id is not None for m in other),
            operator_conversation_starts=sum(m.reply_to_message_id is None for m in operator),
            nonoperator_conversation_starts=sum(m.reply_to_message_id is None for m in other),
            channels=[{"channel_id": c, "message_count": sum(m.channel_id == c for m in selected),
                "unique_authors": len({m.author_id for m in selected if m.channel_id == c})} for c in input.channel_ids],
            daily=[{"date": date, "message_count": n} for date, n in sorted(daily.items())],
        ))
    return result


def empty_payload(input: ObserveJobInput, *, content_requested: bool = False) -> ObservePayload:
    return ObservePayload(snapshot=ObserveSnapshot(), messages=[], activity=activity([], input),
        coverage=ObserveCoverage(guild_id=input.guild_id, channel_ids=input.channel_ids,
            window_start=input.observed_before - timedelta(days=30), window_end=input.observed_before,
            observed_at=datetime.now(UTC), content_requested=content_requested, status="unavailable",
            channels=[ObserveChannelCoverage(channel_id=c, status="unavailable", reason="not_collected",
                message_count=0, history_permission="unknown") for c in input.channel_ids],
            limitations=["Only configured channels are observed; threads are excluded.",
                "Counts describe observed messages, not unreadable or deleted history.",
                "Conversation starts mean messages without a reply reference; reply counts do not prove relationships.",
                "Operators are explicitly configured IDs; no expertise or individual relationship inference is performed."]),
    )


class DiscordObserver:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False,
            timeout=30, limits=httpx.Limits(max_connections=1))

    async def aclose(self):
        await self.client.aclose()

    async def _get(self, path: str, budget, *, params=None):
        # Paths are constructed only from validated snowflakes and fixed endpoint suffixes．
        for retry in range(2):
            budget.take("fetch_requests")
            try:
                async with self.client.stream("GET", DISCORD_API + path, params=params,
                        headers={"Authorization": "Bot " + self.settings.token().get_secret_value()},
                        timeout=min(30, budget.remaining_seconds)) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        budget.check()
                        budget.counts["document_bytes"] += len(chunk)
                        if budget.counts["document_bytes"] > MAX_OBSERVE_BYTES - 65536:
                            raise ObservationError("response_byte_limit")
                        raw.extend(chunk)
                    if response.status_code in {401, 403}:
                        raise ObservationError("missing_access_or_permission")
                    if response.status_code == 404:
                        raise ObservationError("resource_unavailable")
                    if response.status_code == 429:
                        try:
                            delay = float(json.loads(raw).get("retry_after", 0))
                        except (ValueError, TypeError, AttributeError):
                            delay = 0
                        if retry or not 0 < delay <= min(30, budget.remaining_seconds):
                            raise ObservationError("rate_limited")
                        await asyncio.sleep(delay)
                        continue
                    if response.status_code != 200:
                        raise ObservationError("discord_http_error")
                    return json.loads(raw)
            except ConfigurationError:
                raise ObservationError("observer_token_missing_or_invalid") from None
            except httpx.HTTPError:
                raise ObservationError("discord_transport_failed") from None
            except (ValueError, UnicodeError):
                raise ObservationError("discord_response_invalid") from None

    async def observe(self, input: ObserveJobInput, budget, *, result: ObservePayload | None = None) -> ObservePayload:
        result = result or empty_payload(input, content_requested=self.settings.read_message_content)
        allowed = self.settings.guild_channels.get(input.guild_id, [])
        if any(c not in allowed for c in input.channel_ids):
            result.failures.append(ObservationFailure(stage="worker", code="observation_scope_not_allowed"))
            return result
        try:
            guild = await self._get(f"/guilds/{input.guild_id}", budget, params={"with_counts": "true"})
            if guild.get("id") != input.guild_id:
                raise ObservationError("guild_identity_mismatch")
            result.snapshot.guild = ObservedGuild(id=guild["id"], name=guild["name"],
                description=guild.get("description"), approximate_member_count=guild.get("approximate_member_count"))
            channels = await self._get(f"/guilds/{input.guild_id}/channels", budget)
            roles = await self._get(f"/guilds/{input.guild_id}/roles", budget)
            if not isinstance(channels, list) or not isinstance(roles, list):
                raise ObservationError("snapshot_invalid")
            snapshot_channels = [ObservedChannel(id=c["id"], guild_id=c["guild_id"],
                name=c.get("name", ""), type=c["type"], parent_id=c.get("parent_id")) for c in channels]
            snapshot_roles = [ObservedRole(id=r["id"], name=r["name"], permissions=r["permissions"]) for r in roles]
            if any(c.guild_id != input.guild_id for c in snapshot_channels):
                raise ObservationError("channel_guild_mismatch")
            result.snapshot.channels = snapshot_channels
            result.snapshot.roles = snapshot_roles
        except (ObservationError, BudgetExceeded, ValidationError, KeyError, TypeError, AttributeError) as exc:
            result.failures.append(ObservationFailure(stage="snapshot", code=_error_code(exc)))
            result.coverage.status = "partial" if result.snapshot.guild else "unavailable"
            return result

        # The bot's own member record permits an explicit READ_MESSAGE_HISTORY check．
        # Failure leaves permission unknown rather than treating an empty list as zero activity．
        member = None
        bot_id = None
        try:
            me = await self._get("/users/@me", budget)
            bot_id = me["id"]
            member = await self._get(f"/guilds/{input.guild_id}/members/{bot_id}", budget)
        except (ObservationError, BudgetExceeded, KeyError, TypeError):
            result.coverage.limitations.append("Bot history permissions could not be independently confirmed.")
        by_id = {c["id"]: c for c in channels}
        message_bytes = 0
        for coverage in result.coverage.channels:
            channel_id = coverage.channel_id
            try:
                channel = by_id.get(channel_id)
                if channel is None:
                    raise ObservationError("configured_channel_unavailable")
                if channel["type"] not in {0, 5}:
                    raise ObservationError("channel_type_not_supported")
                permission = _history_permission(guild, channel, roles, member, bot_id)
                coverage.history_permission = permission
                if permission == "denied":
                    raise ObservationError("history_permission_denied")
                before = _snowflake_before(input.observed_before)
                seen = set()
                while True:
                    if coverage.message_count >= input.max_messages_per_channel or len(result.messages) >= 2000:
                        raise ObservationError("message_limit")
                    page = await self._get(f"/channels/{channel_id}/messages", budget,
                        params={"limit": min(100, input.max_messages_per_channel - coverage.message_count,
                            2000 - len(result.messages)), "before": before})
                    if not isinstance(page, list):
                        raise ObservationError("history_invalid")
                    if not page:
                        coverage.status = "complete" if permission == "granted" else "unavailable"
                        coverage.reason = None if permission == "granted" else "empty_history_permission_unknown"
                        break
                    oldest = min(int(m["id"]) for m in page)
                    if oldest >= int(before):
                        raise ObservationError("pagination_not_advancing")
                    reached_start = False
                    for raw in page:
                        created = _timestamp(raw["timestamp"])
                        if raw.get("channel_id") != channel_id or int(raw["id"]) >= int(before):
                            raise ObservationError("message_identity_mismatch")
                        if created < result.coverage.window_start:
                            reached_start = True
                            continue
                        if created >= input.observed_before or raw["id"] in seen:
                            continue
                        if coverage.message_count >= input.max_messages_per_channel or len(result.messages) >= 2000:
                            raise ObservationError("message_limit")
                        message = self._message(input.guild_id, channel_id, raw, created)
                        size = len(canonical_json(message.model_dump(mode="json")).encode("utf-8"))
                        if message_bytes + size > 7 * 1024**2:
                            raise ObservationError("payload_byte_limit")
                        result.messages.append(message)
                        message_bytes += size
                        seen.add(message.message_id)
                        coverage.message_count += 1
                        coverage.oldest_observed_at = min(created, coverage.oldest_observed_at or created)
                    before = str(oldest)
                    if reached_start:
                        if permission == "granted":
                            coverage.status, coverage.reason = "complete", None
                        else:
                            coverage.status = "partial" if coverage.message_count else "unavailable"
                            coverage.reason = "history_permission_unknown"
                        break
            except (ObservationError, BudgetExceeded, ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
                coverage.status = "partial" if coverage.message_count else "unavailable"
                coverage.reason = _error_code(exc)
                result.failures.append(ObservationFailure(stage="history", channel_id=channel_id, code=coverage.reason))
        result.activity = activity(result.messages, input)
        content_missing = any(m.content_status != "available" or m.content_truncated for m in result.messages)
        result.coverage.status = "complete" if (all(c.status == "complete" for c in result.coverage.channels)
            and not content_missing and self.settings.read_message_content and not result.failures) else "partial"
        if content_missing or not self.settings.read_message_content:
            result.coverage.limitations.append("Message content is empty, unavailable, truncated or not requested; discussion coverage is limited.")
        # Revalidate the final mutated object，including the total serialized byte bound．
        return ObservePayload.model_validate(result.model_dump(mode="json"))

    def _message(self, guild: str, channel: str, raw: dict, created: datetime) -> ObservedMessage:
        text = raw.get("content")
        available = self.settings.read_message_content and isinstance(text, str) and bool(text)
        reference = raw.get("message_reference") or {}
        return ObservedMessage(guild_id=guild, channel_id=channel, message_id=raw["id"],
            author_id=raw["author"]["id"], author_bot=raw["author"].get("bot", False),
            operator=raw["author"]["id"] in self.settings.operator_user_ids, created_at=created,
            edited_at=_timestamp(raw["edited_timestamp"]) if raw.get("edited_timestamp") else None,
            reply_to_message_id=reference.get("message_id"),
            permalink=f"https://discord.com/channels/{guild}/{channel}/{raw['id']}",
            content=text[:4000] if available else None,
            content_status="available" if available else (
                "empty_or_unavailable" if self.settings.read_message_content else "not_requested"),
            content_truncated=bool(available and len(text) > 4000))


def _error_code(exc) -> str:
    if isinstance(exc, ObservationError):
        return exc.code
    if isinstance(exc, BudgetExceeded):
        return "budget_exhausted"
    return "discord_response_invalid"


def _history_permission(guild, channel, roles, member, bot_id) -> str:
    if not member or not bot_id:
        return "unknown"
    try:
        if guild.get("owner_id") == bot_id:
            return "granted"
        member_roles = set(member["roles"])
        permissions = 0
        for role in roles:
            if role["id"] == guild["id"] or role["id"] in member_roles:
                permissions |= int(role["permissions"])
        if permissions & 8:
            return "granted"
        overwrites = channel.get("permission_overwrites", [])
        for overwrite in overwrites:
            if overwrite["id"] == guild["id"]:
                permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
        allow = deny = 0
        for overwrite in overwrites:
            if overwrite["type"] == 0 and overwrite["id"] in member_roles:
                allow |= int(overwrite["allow"])
                deny |= int(overwrite["deny"])
        permissions = (permissions & ~deny) | allow
        for overwrite in overwrites:
            if overwrite["type"] == 1 and overwrite["id"] == bot_id:
                permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
        return "granted" if permissions & 1024 and permissions & 65536 else "denied"
    except (ValueError, KeyError, TypeError):
        return "unknown"
