from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from ephy_worker.budget import Budget
from ephy_worker.config import DiscordObserverConfig
from ephy_worker.contracts import ObserveJobInput
from ephy_worker.discord_observer import DiscordObserver
from ephy_worker.schema import Limits


def observation_input(**updates):
    return ObserveJobInput(mode="observe", guild_id="123", channel_ids=["456"],
        observed_before=datetime(2026, 10, 22, tzinfo=UTC), **updates)


def observer_config(**updates):
    return DiscordObserverConfig(guild_channels={"123": ["456"]}, read_message_content=True, **updates)


def fixture_message(*, text="今週の計画を相談しました．", days=1, author="789", reply=None):
    timestamp = datetime(2026, 10, 22, tzinfo=UTC) - timedelta(days=days)
    identifier = str((int(timestamp.timestamp() * 1000) - 1420070400000) << 22)
    return {"id": identifier, "channel_id": "456", "author": {"id": author},
        "timestamp": timestamp.isoformat(), "content": text,
        "message_reference": {"message_id": reply} if reply else None}


class DiscordFixture:
    def __init__(self, *, messages=None, permission=66560, member_failure=False, rate_limit=False):
        self.messages = messages if messages is not None else [fixture_message()]
        self.permission, self.member_failure, self.rate_limit = permission, member_failure, rate_limit
        self.calls = []
        self.pages = 0

    async def __call__(self, request):
        self.calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "discord.com"
        assert request.headers["authorization"] == "Bot " + "fixture-token-1234567890"
        path = request.url.path.removeprefix("/api/v10")
        if path == "/guilds/123":
            return httpx.Response(200, json={"id": "123", "name": "Fixture guild", "owner_id": "888"})
        if path == "/guilds/123/channels":
            return httpx.Response(200, json=[{"id": "456", "guild_id": "123", "name": "fixture", "type": 0},
                {"id": "457", "guild_id": "123", "name": "metadata-only", "type": 0}])
        if path == "/guilds/123/roles":
            return httpx.Response(200, json=[{"id": "123", "name": "everyone", "permissions": str(self.permission)}])
        if path == "/users/@me":
            return httpx.Response(200, json={"id": "999"})
        if path == "/guilds/123/members/999":
            return httpx.Response(403, json={}) if self.member_failure else httpx.Response(200, json={"roles": []})
        assert path == "/channels/456/messages", "unconfigured content must never be requested"
        self.pages += 1
        if self.rate_limit and self.pages == 1:
            return httpx.Response(429, json={"retry_after": 0.001})
        return httpx.Response(200, json=self.messages if self.pages == (2 if self.rate_limit else 1) else [])


async def collect(monkeypatch, fixture, *, config=None, input=None, limits=None):
    monkeypatch.setenv("EPHY_DISCORD_BOT_TOKEN", "fixture-token-1234567890")
    observer = DiscordObserver(config or observer_config(), transport=httpx.MockTransport(fixture))
    budget = Budget(limits or Limits())
    try:
        return await observer.observe(input or observation_input(), budget), budget
    finally:
        await observer.aclose()


async def test_get_only_snapshot_and_7_30d_activity_evidence(monkeypatch):
    fixture = DiscordFixture(messages=[fixture_message(), fixture_message(days=10, author="790", reply="800")])
    payload, budget = await collect(monkeypatch, fixture, config=observer_config(operator_user_ids=["789"]))
    assert payload.coverage.status == "complete"
    assert [a.message_count for a in payload.activity] == [1, 2]
    assert payload.activity[1].unique_authors == 2
    assert payload.activity[1].operator_conversation_starts == 1
    assert payload.activity[1].nonoperator_reply_messages == 1
    assert len(payload.snapshot.channels) == 2
    assert payload.messages[0].content_status == "available"
    assert payload.messages[1].permalink.endswith(payload.messages[1].message_id)
    assert budget.counts["fetch_requests"] == 7
    assert payload.coverage.threads_included is False


@pytest.mark.parametrize("empty,member_failure,permission,expected", [
    (True, False, 66560, "complete"), (True, True, 66560, "unavailable"), (False, False, 1024, "unavailable")])
async def test_empty_history_and_permission_are_distinguished(monkeypatch, empty, member_failure, permission, expected):
    fixture = DiscordFixture(messages=[] if empty else None, member_failure=member_failure, permission=permission)
    payload, _ = await collect(monkeypatch, fixture)
    assert payload.coverage.channels[0].status == expected
    if permission == 1024:
        assert fixture.pages == 0
        assert payload.coverage.channels[0].history_permission == "denied"
    if member_failure:
        assert payload.coverage.channels[0].reason == "empty_history_permission_unknown"


async def test_missing_content_is_partial_without_discarding_message_counts(monkeypatch):
    payload, _ = await collect(monkeypatch, DiscordFixture(messages=[fixture_message(text="")]))
    assert payload.coverage.status == "partial"
    assert payload.activity[0].message_count == 1
    assert payload.messages[0].content is None
    assert payload.messages[0].content_status == "empty_or_unavailable"


async def test_unknown_permission_reaching_window_start_keeps_evidence_partial(monkeypatch):
    fixture = DiscordFixture(member_failure=True,
        messages=[fixture_message(), fixture_message(days=31, author="790")])
    payload, _ = await collect(monkeypatch, fixture)
    assert payload.coverage.status == "partial"
    channel = payload.coverage.channels[0]
    assert channel.history_permission == "unknown" and channel.status == "partial"
    assert channel.reason == "history_permission_unknown"
    assert channel.message_count == 1 and len(payload.messages) == 1
    assert payload.messages[0].content == "今週の計画を相談しました．"
    assert [a.message_count for a in payload.activity] == [1, 1]


async def test_allowlist_failure_precedes_any_network(monkeypatch):
    fixture = DiscordFixture()
    payload, _ = await collect(monkeypatch, fixture,
        input=observation_input().model_copy(update={"channel_ids": ["457"]}))
    assert not fixture.calls
    assert payload.failures[0].code == "observation_scope_not_allowed"


async def test_message_limit_and_truncated_body_explicit(monkeypatch):
    payload, _ = await collect(monkeypatch, DiscordFixture(messages=[fixture_message(text="長" * 5000)]),
        input=observation_input(max_messages_per_channel=1))
    assert payload.coverage.status == "partial"
    assert payload.coverage.channels[0].reason == "message_limit"
    assert payload.messages[0].content_truncated and len(payload.messages[0].content) == 4000


async def test_rate_limit_counts_retried_get_and_budget_stop(monkeypatch):
    fixture = DiscordFixture(rate_limit=True)
    payload, budget = await collect(monkeypatch, fixture)
    assert payload.coverage.status == "complete" and budget.counts["fetch_requests"] == 8
    limited, _ = await collect(monkeypatch, DiscordFixture(), limits=Limits(fetch_requests=5))
    assert limited.coverage.status == "partial"
    assert limited.failures[0].code == "budget_exhausted"


async def test_response_byte_bound_and_redirect_not_followed(monkeypatch):
    async def huge(request):
        return httpx.Response(200, content=b" " * (9 * 1024**2))
    payload, budget = await collect(monkeypatch, huge)
    assert payload.failures[0].code == "response_byte_limit"
    assert budget.counts["document_bytes"] <= 8 * 1024**2
    async def redirect(request):
        return httpx.Response(302, headers={"location": "https://example.org/private"})
    payload, _ = await collect(monkeypatch, redirect)
    assert payload.failures[0].code == "discord_http_error"


def test_input_is_strict_and_has_no_question_or_context():
    data = observation_input().model_dump(mode="json")
    for update in ({"guild_id": 123}, {"channel_ids": ["456", "456"]}, {"query": "private question"},
                   {"max_messages_per_channel": True}, {"observed_before": "2026-10-22T00:00:00"}):
        with pytest.raises(ValidationError):
            ObserveJobInput.model_validate({**data, **update})
