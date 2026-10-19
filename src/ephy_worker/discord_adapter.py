"""Bounded, guild-only Discord adapter; Discord text never selects tools or configuration."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .search import validate_public_text
from .store import output_root

SECRET = re.compile(r"(?:mfa\.[\w-]{20,}|[\w-]{24,}\.[\w-]{6}\.[\w-]{20,}|(?:gh[pousr]_|github_pat_)[\w]{20,}|sk-[\w-]{16,}|tvly-[\w-]{10,})")


def redact_secrets(text: str, known_secrets: tuple[str, ...] = ()) -> str:
    """Redact raw values and Markdown-escaped equivalents without rewriting other text."""
    spans = [(match.start(), match.end()) for match in SECRET.finditer(text)]
    for secret in known_secrets:
        if secret:
            spans.extend((match.start(), match.end()) for match in re.finditer(re.escape(secret), text))
    characters, positions = [], []
    index = 0
    while index < len(text):
        start = index
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in r"\`*_{}[]()#!|-":
            index += 1
        characters.append(text[index])
        index += 1
        positions.append((start, index))
    inspected = "".join(characters)
    matches = list(SECRET.finditer(inspected))
    for secret in known_secrets:
        if secret:
            matches.extend(re.finditer(re.escape(secret), inspected))
    spans.extend((positions[match.start()][0], positions[match.end() - 1][1]) for match in matches)
    output, end = [], 0
    for start, stop in sorted(spans):
        if start < end:
            # Overlapping exact/pattern/escaped matches must be removed as one span.
            end = max(end, stop)
            continue
        output.extend((text[end:start], "[redacted]"))
        end = stop
    output.append(text[end:])
    return "".join(output)


def redact_value(value, known_secrets: tuple[str, ...] = ()):
    """Create a redacted outbound projection; never mutate canonical Job artifacts."""
    if isinstance(value, str):
        return redact_secrets(value, known_secrets)
    if isinstance(value, list):
        return [redact_value(item, known_secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, known_secrets) for key, item in value.items()}
    return value


class DiscordError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DiscordSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entrypoint: Literal["mention", "slash"] = "mention"
    read_message_content: bool = Field(default=False, strict=True)
    application_id: int = Field(strict=True, gt=0)
    guild_id: int = Field(strict=True, gt=0)
    allowed_channel_ids: frozenset[int] = Field(min_length=1)
    allowed_user_ids: frozenset[int] = Field(min_length=1)
    token_env: str = Field(default="EPHY_DISCORD_BOT_TOKEN", pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    worker_config: Path
    profile: str = Field(min_length=1)
    output_dir: Path
    state_dir: Path
    timeout_seconds: float = Field(default=300, ge=1, le=600)

    @field_validator("allowed_channel_ids", "allowed_user_ids", mode="before")
    @classmethod
    def ids(cls, value):
        if not isinstance(value, (list, set, frozenset)) or any(type(i) is not int or i <= 0 for i in value):
            raise ValueError("IDs must be positive integers")
        return value

    @model_validator(mode="after")
    def entry_scope(self):
        if self.entrypoint == "mention" and len(self.allowed_channel_ids) != 1:
            raise ValueError("mention work/replies require exactly one test channel")
        if self.entrypoint == "slash" and self.read_message_content:
            raise ValueError("message content option belongs to mention entrypoint")
        return self

    @classmethod
    def load(cls, path: Path) -> DiscordSettings:
        try:
            if path.stat().st_size > 32768:
                raise ValueError("oversize configuration")
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            settings = cls.model_validate(data)
            parent = path.resolve().parent
            resolved = {
                key: (parent / getattr(settings, key)).resolve()
                for key in ("worker_config", "output_dir", "state_dir")
            }
            output_root(resolved["output_dir"])
            output_root(resolved["state_dir"])
            return settings.model_copy(update=resolved)
        except (OSError, ValueError) as exc:
            raise DiscordError("discord_config_invalid") from exc

    def token(self) -> str:
        value = os.environ.get(self.token_env, "").strip()
        if value.startswith("Bot "):
            value = value[4:].strip()
        if not value or any(char.isspace() for char in value):
            raise DiscordError("discord_token_missing_or_invalid")
        return value


@dataclass(frozen=True)
class Request:
    id: int
    application_id: int
    guild_id: int | None
    channel_id: int | None
    user_id: int
    bot: bool = False


@dataclass(frozen=True)
class Answer:
    state: str
    text: str
    job_id: str = ""


class Reply(Protocol):
    async def defer(self) -> None: ...
    async def send(self, text: str) -> None: ...
    async def reject(self, text: str) -> None: ...


class Ledger:
    """At-most-once execution, including uncertain deliveries and process restarts.

    Only IDs/status/job IDs are persisted. A process lock prevents concurrent owners.
    No automatic retry/resume of interrupted work or delivery is attempted.
    """

    def __init__(self, settings: DiscordSettings):
        root = output_root(settings.state_dir)
        root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            root.chmod(0o700)
        self.lock = (root / "adapter.lock").open("a+b")
        try:
            self.lock.seek(0, os.SEEK_END)
            if self.lock.tell() == 0:
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise DiscordError("discord_state_already_in_use") from None
        try:
            self.db = sqlite3.connect(root / "interactions.sqlite3", isolation_level=None)
            self.db.execute("CREATE TABLE IF NOT EXISTS identity (application INTEGER, guild INTEGER)")
            identity = (settings.application_id, settings.guild_id)
            rows = self.db.execute("SELECT application, guild FROM identity").fetchall()
            if not rows:
                self.db.execute("INSERT INTO identity VALUES (?, ?)", identity)
            elif rows != [identity]:
                raise DiscordError("discord_state_identity_mismatch")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS requests "
                "(id INTEGER PRIMARY KEY, guild INTEGER, channel INTEGER, user INTEGER, "
                "status TEXT NOT NULL, job TEXT NOT NULL DEFAULT '')"
            )
            self.db.execute("UPDATE requests SET status='interrupted' WHERE status IN ('received', 'running')")
            if os.name == "posix":
                (root / "interactions.sqlite3").chmod(0o600)
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    def claim(self, request: Request) -> bool:
        if self.db.execute("SELECT 1 FROM requests WHERE id=?", (request.id,)).fetchone():
            return False
        if self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= 10000:
            raise DiscordError("discord_ledger_full")
        self.db.execute(
            "INSERT INTO requests (id, guild, channel, user, status) VALUES (?, ?, ?, ?, 'received')",
            (request.id, request.guild_id, request.channel_id, request.user_id),
        )
        return True

    def status(self, request_id: int, status: str, job: str = "") -> None:
        self.db.execute("UPDATE requests SET status=?, job=? WHERE id=?", (status, job, request_id))

    def close(self) -> None:
        self.db.close()
        self.lock.close()


def split_reply(
    text: str, limit: int = 1800, parts: int = 5, *, known_secrets: tuple[str, ...] = ()
) -> list[str]:
    """Bound UTF-16 units (including astral emoji), preserving all text when it fits."""
    if not 100 <= limit <= 2000 or not 1 <= parts <= 5:
        raise ValueError("invalid reply bounds")
    text = redact_secrets(text, known_secrets) or "回答は空でした．"
    chunks, current, units = [], "", 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append(current)
            if len(chunks) == parts:
                marker = "…（出力上限．全文はローカル成果物で確認してください．）"
                last = chunks[-1]
                while len((last + marker).encode("utf-16-le")) // 2 > limit:
                    last = last[:-1]
                chunks[-1] = last + marker
                return chunks
            current, units = "", 0
        current += char
        units += width
    if current:
        chunks.append(current)
    return chunks


class DiscordAdapter:
    def __init__(
        self, settings: DiscordSettings, ledger: Ledger, execute: Callable[[str], Awaitable[Answer]],
        *, known_secrets: tuple[str, ...] = (),
    ):
        self.settings, self.ledger, self.execute = settings, ledger, execute
        self.active: tuple[Request, asyncio.Task] | None = None
        self.cancelling: set[int] = set()
        self.closing = False
        self._known_secrets = known_secrets
        self._deadline: asyncio.Timeout | None = None
        self._close_task: asyncio.Task | None = None

    def allowed(self, request: Request) -> bool:
        return (
            not self.closing
            and type(request.id) is int and 0 < request.id < 2**63
            and not request.bot
            and request.application_id == self.settings.application_id
            and request.guild_id == self.settings.guild_id
            and request.channel_id in self.settings.allowed_channel_ids
            and request.user_id in self.settings.allowed_user_ids
        )

    async def ask(self, request: Request, reply: Reply, question: str) -> None:
        if not self.allowed(request):
            await self._reject(reply, "この場所または利用者は許可されていません．")
            return
        if not self.ledger.claim(request):
            return
        if self.active is not None:
            self.ledger.status(request.id, "busy")
            await self._reject(reply, "別の質問を処理中です．終了後に新しいコマンドで再送してください．")
            return
        try:
            if (
                not isinstance(question, str) or not question.strip() or len(question) > 1000
                or redact_secrets(question, self._known_secrets) != question
            ):
                raise ValueError("question rejected")
            validate_public_text(question)
        except ValueError:
            self.ledger.status(request.id, "rejected")
            await self._reject(reply, "公開情報の質問を1〜1000文字で指定してください．秘密や私的情報は送れません．")
            return
        self.active = (request, asyncio.current_task())
        try:
            await reply.defer()  # No executor call before the initial Discord acknowledgement succeeds.
            receipt = "分かりました，調べてみますね．"
            if self.settings.entrypoint == "slash":
                receipt += f"\n取消：/ephy-cancel request_id:{request.id}"
            await reply.send(receipt)
            self.ledger.status(request.id, "running")
            try:
                async with asyncio.timeout(self.settings.timeout_seconds) as deadline:
                    self._deadline = deadline
                    from .answer_routing import request_context

                    context_token = request_context.set(request)
                    try:
                        answer = await self.execute(question)
                    finally:
                        request_context.reset(context_token)
                state = "timed_out" if deadline.expired() else answer.state
                if request.id in self.cancelling:
                    state = "cancelled"
            except TimeoutError:
                state, answer = "timed_out", Answer("timed_out", "もう少し調べるのに時間がかかりそうです．")
            except asyncio.CancelledError:
                state, answer = "cancelled", Answer("cancelled", "処理を取り消しました．")
            except Exception as exc:  # noqa: BLE001 — persist sanitized executor failure, never exception contents.
                state, answer = "executor_failed", Answer("failed", "調査中に問題が起きたため，回答をまとめられませんでした．", getattr(exc, "job_id", ""))
            finally:
                self._deadline = None
            if state not in {"completed", "partial", "failed", "cancelled", "timed_out", "executor_failed"}:
                state, answer = "executor_failed", Answer("failed", "調査結果を回答にまとめられませんでした．")
            self.ledger.status(request.id, state, answer.job_id)
            if not self.closing:
                text = answer.text
                if state == "timed_out":
                    notice = "もう少し調べるのに時間がかかりそうです．"
                    text = notice if answer.state == "cancelled" else text if text.startswith(notice) else notice + "\n\n" + text
                elif state == "cancelled":
                    text = "調査を取り消しました．"
                for part in split_reply(text, known_secrets=self._known_secrets):
                    await reply.send(part)
        except asyncio.CancelledError:
            self.ledger.status(request.id, "cancelled")
        except Exception:  # noqa: BLE001 — delivery errors are persisted without token-bearing details.
            # Delivery is ambiguous. Do not resend or reexecute automatically.
            self.ledger.status(request.id, "delivery_failed")
        finally:
            self.active = None
            self._deadline = None
            self.cancelling.discard(request.id)

    async def cancel(self, request: Request, reply: Reply, target: str) -> None:
        if not self.allowed(request):
            await self._reject(reply, "この場所または利用者は許可されていません．")
            return
        if not self.ledger.claim(request):
            return
        active = self.active
        if (
            active is None or str(active[0].id) != target
            or (active[0].user_id, active[0].guild_id, active[0].channel_id)
            != (request.user_id, request.guild_id, request.channel_id)
        ):
            self.ledger.status(request.id, "rejected")
            await self._reject(reply, "この場所で自分が実行中の受付IDだけを取り消せます．")
            return
        self._request_cancel(active)
        self.ledger.status(request.id, "cancel_requested")
        await self._reject(reply, "取消を要求しました．共有モデルサービス自体は停止しません．")

    @staticmethod
    async def _reject(reply: Reply, text: str) -> None:
        try:
            await reply.reject(text)
        except Exception:  # noqa: BLE001 — log a safe delivery marker; keep the rejection effective.
            logging.getLogger(__name__).warning("discord_denial_delivery_failed")

    def _request_cancel(self, active: tuple[Request, asyncio.Task]) -> None:
        request, task = active
        # Disarm our deadline before cancellation can enter executor cleanup.
        if self._deadline is not None and not self._deadline.expired():
            self._deadline.reschedule(None)
        if request.id in self.cancelling or task.done() or task.cancelling():
            return
        self.cancelling.add(request.id)
        task.cancel()

    async def close(self) -> None:
        if self._close_task is None:
            self.closing = True
            self._close_task = asyncio.create_task(self._close())
        # Repeated or cancelled shutdown callers cannot cancel executor finalization.
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self.active is not None:
            active = self.active
            self._request_cancel(active)
            _, pending = await asyncio.wait({active[1]}, timeout=10)
            if pending:
                # Unlike wait_for/gather cancellation, this does not inject a second cancel.
                raise DiscordError("discord_shutdown_timeout")
        self.ledger.close()
