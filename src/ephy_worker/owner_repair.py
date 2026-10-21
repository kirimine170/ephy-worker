"""Owner-only repair queue and persistent controller, separate from Discord．"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from .store import output_root

OWNER_USER_ID = 425581535748423680


def request_release_limits(text: str) -> tuple[str, ...]:
    """Explicit request restrictions never inherit the standing full-cycle grant．"""
    lowered = text.casefold()
    limits = []
    for action, words in (("merge", r"merge|merging|マージ"),
                          ("deploy", r"deploy(?:ment)?|デプロイ|本番(?:には|へは|へ|に)?(?:反映|適用|更新)|反映"),
                          ("push", r"push(?:ing)?|プッシュ"), ("commit", r"commit(?:ting)?|コミット"),
                          ("restart", r"restart(?:ing)?|再起動")):
        if (re.search(r"(?:no[-\s]+|(?:do\s+not|don't|must\s+not|never|without)\s+)(?:\w+\s+){0,2}(?:" + words + r")\b", lowered)
                or re.search(r"(?:" + words + r")\s*(?:は|を|まで)?\s*(?:まだ|絶対に|決して|今回は|今は)?(?:しない|しなく|せず|するな|禁止|不要|しちゃだめ|行わない|行わず|実施しない)", lowered)):
            limits.append(action)
    if re.search(r"proposal[ -]only|only\s+(?:propose|investigate|review)|do\s+not\s+(?:implement|change)|(?:提案|調査|確認|説明|pr(?:作成)?|プルリク(?:エスト)?)(?:だけ|のみ|まで)|(?:コミット|commit|push|プッシュ)まで|実装(?:だけ|のみ)|本番.*(?:触らない|触らず)", lowered):
        limits.append("proposal_only")
    return tuple(limits)


def strict_object(raw: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json_constant")))
    if not isinstance(value, dict):
        raise ValueError("json_object_required")  # noqa: TRY004 — malformed JSON value is a parser error．
    return value


def parse_repair_intent(raw: str) -> bool:
    value = strict_object(raw)
    if set(value) != {"action"} or value["action"] not in ("repair", "conversation"):
        raise ValueError("invalid_repair_classification")
    return value["action"] == "repair"


class RepairStore:
    """SQLite is private state, never a model tool or a source artifact．"""

    def __init__(self, root: Path):
        if not root.is_absolute():
            raise ValueError("repair_state_must_be_absolute")
        self.root = output_root(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "owner-repair.sqlite3"
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, guild TEXT, channel TEXT, message TEXT, author TEXT, text TEXT, previous TEXT, stage TEXT, data TEXT, created REAL, updated REAL, UNIQUE(guild,channel,message))")
            db.execute("CREATE TABLE IF NOT EXISTS outbox (job TEXT, ordinal INTEGER, text TEXT, state TEXT, message TEXT, PRIMARY KEY(job,ordinal))")
            db.execute("CREATE TABLE IF NOT EXISTS intakes (id TEXT PRIMARY KEY,guild TEXT,channel TEXT,message TEXT,author TEXT,text TEXT,previous TEXT,route TEXT,receipt INTEGER,done INTEGER,consumer TEXT,created REAL,UNIQUE(guild,channel,message))")
            if "community_context" not in {row["name"] for row in db.execute("PRAGMA table_info(intakes)")}:
                db.execute("ALTER TABLE intakes ADD COLUMN community_context TEXT")

    def capture_intake(self, *, guild_id: int, channel_id: int, message_id: int, author_id: int,
                       text: str, previous_answer: str | None = None, community_context: dict | None = None) -> str:
        if type(author_id) is not int or author_id != OWNER_USER_ID:
            raise ValueError("repair_owner_required")
        if (any(type(value) is not int or not 0 < value < 2**64 for value in (guild_id, channel_id, message_id))
                or not isinstance(text, str) or not text.strip() or len(text) > 4000
                or previous_answer is not None and (not isinstance(previous_answer, str) or len(previous_answer) > 8000)):
            raise ValueError("repair_intake_invalid")
        if community_context is not None and (
                not isinstance(community_context, dict) or set(community_context) != {"operation", "period_days"}
                or community_context["operation"] not in ("report", "snapshot", "activity", "discussion", "unresolved")
                or type(community_context["period_days"]) is not int or community_context["period_days"] not in (7, 30)):
            raise ValueError("repair_context_invalid")
        context = json.dumps(community_context, sort_keys=True, separators=(",", ":")) if community_context else None
        identifier = "repair-" + hashlib.sha256(f"{guild_id}:{channel_id}:{message_id}:{author_id}".encode()).hexdigest()[:32]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM intakes WHERE id=?", (identifier,)).fetchone()
            if old:
                if old["text"] != text or old["previous"] != previous_answer or old["community_context"] != context:
                    raise RuntimeError("duplicate_repair_payload_changed")
            else:
                if db.execute("SELECT count(*) FROM intakes").fetchone()[0] >= 10000:
                    raise RuntimeError("repair_queue_full")
                db.execute("INSERT INTO intakes(id,guild,channel,message,author,text,previous,route,receipt,done,consumer,created,community_context) "
                    "VALUES (?,?,?,?,?,?,?,NULL,0,0,NULL,?,?)", (identifier,
                    str(guild_id), str(channel_id), str(message_id), str(author_id), text, previous_answer, time.time(), context))
        return identifier

    def pending_intakes(self, *, guild_id: int, channel_ids: tuple[int, ...]) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT * FROM intakes WHERE done=0 ORDER BY created").fetchall()
        return [dict(row) for row in rows if row["guild"] == str(guild_id)
            and int(row["channel"]) in channel_ids and row["author"] == str(OWNER_USER_ID)]

    def claim_intake(self, identifier: str, consumer: str) -> dict | None:
        import psutil
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM intakes WHERE id=? AND done=0", (identifier,)).fetchone()
            if row is None:
                return None
            if row["consumer"]:
                pid, created = row["consumer"].split(":")
                try:
                    if row["consumer"] != consumer and psutil.Process(int(pid)).create_time() == float(created):
                        return None
                except psutil.NoSuchProcess:
                    pass  # A dead consumer cannot resume this durable intake．
            db.execute("UPDATE intakes SET consumer=? WHERE id=?", (consumer, identifier))
            return dict(row)

    def update_intake(self, identifier: str, consumer: str, *, route: str | None = None,
                      receipt: bool = False, done: bool = False, release: bool = False) -> None:
        if route not in {None, "repair", "conversation"}:
            raise ValueError("repair_intake_route_invalid")
        with self.db() as db:
            db.execute("UPDATE intakes SET route=COALESCE(?,route),receipt=MAX(receipt,?),done=MAX(done,?),consumer=CASE WHEN ? THEN NULL ELSE consumer END WHERE id=? AND consumer=?",
                (route, int(receipt), int(done), int(release), identifier, consumer))

    @contextmanager
    def db(self):
        for path in (self.root, *self.root.parents):
            if path.is_symlink() or path.is_junction():
                raise RuntimeError("repair_state_linked")
        output_root(self.root)
        for path in (self.path, self.path.with_name(self.path.name + "-journal")):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 67108864:
                    raise RuntimeError("repair_state_unsafe")
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, *, guild_id: int, channel_id: int, message_id: int, author_id: int,
                text: str, previous_answer: str | None = None) -> str:
        # Authorization precedes even opening the database．
        if type(author_id) is not int or author_id != OWNER_USER_ID:
            raise ValueError("repair_owner_required")
        if any(type(value) is not int or value <= 0 or value >= 2**64
               for value in (guild_id, channel_id, message_id)):
            raise ValueError("repair_identity_invalid")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("repair_directive_invalid")
        if previous_answer is not None and (not isinstance(previous_answer, str) or len(previous_answer) > 8000):
            raise ValueError("repair_context_invalid")
        identity = f"{guild_id}:{channel_id}:{message_id}:{author_id}"
        identifier = "repair-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if existing:
                if existing["text"] != text or existing["previous"] != previous_answer:
                    raise RuntimeError("duplicate_repair_payload_changed")
                return identifier
            if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 10000:
                raise RuntimeError("repair_queue_full")
            now = time.time()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (identifier, str(guild_id),
                str(channel_id), str(message_id), str(author_id), text, previous_answer, "queued", "{}", now, now))
        return identifier

    def get(self, identifier: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("repair_not_found")
        result = dict(row)
        result["data"] = strict_object(result["data"])
        return result

    def next(self) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT id FROM jobs WHERE stage NOT IN ('completed','failed') ORDER BY created LIMIT 1").fetchone()
        return self.get(row["id"]) if row else None

    def transition(self, identifier: str, stage: str, **data) -> None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("repair_not_found")
            merged = {**strict_object(row["data"]), **data}
            db.execute("UPDATE jobs SET stage=?, data=?, updated=? WHERE id=?",
                (stage, json.dumps(merged, ensure_ascii=False), time.time(), identifier))

    def notify(self, identifier: str, text: str) -> None:
        # Freeze message chunks before delivery；a restart uses the same bytes．
        from .discord_runtime import safe_final, split_discord_text
        parts = split_discord_text(safe_final(text))
        with self.db() as db:
            if db.execute("SELECT 1 FROM outbox WHERE job=?", (identifier,)).fetchone():
                return
            db.executemany("INSERT INTO outbox VALUES (?,?,?,'pending',NULL)",
                [(identifier, index, part) for index, part in enumerate(parts)])

    def finish(self, identifier: str, stage: str, text: str, **data) -> None:
        """Terminal result and its outbox become durable in one transaction．"""
        from .discord_runtime import safe_final, split_discord_text
        parts = split_discord_text(safe_final(text))
        if stage not in {"completed", "failed"}:
            raise ValueError("terminal_stage_required")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("repair_not_found")
            merged = {**strict_object(row["data"]), **data}
            db.execute("UPDATE jobs SET stage=?,data=?,updated=? WHERE id=?",
                (stage, json.dumps(merged, ensure_ascii=False), time.time(), identifier))
            if not db.execute("SELECT 1 FROM outbox WHERE job=?", (identifier,)).fetchone():
                db.executemany("INSERT INTO outbox VALUES (?,?,?,'pending',NULL)",
                    [(identifier, index, part) for index, part in enumerate(parts)])

    def pending(self, *, guild_id: int, channel_ids: tuple[int, ...]) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT o.*, j.guild, j.channel, j.message AS request_message, j.author FROM outbox o JOIN jobs j ON j.id=o.job WHERE o.state='pending' ORDER BY j.created,o.ordinal").fetchall()
        return [dict(row) for row in rows if row["guild"] == str(guild_id)
            and int(row["channel"]) in channel_ids and row["author"] == str(OWNER_USER_ID)]

    def claim_delivery(self, identifier: str, ordinal: int) -> bool:
        # Claim BEFORE send. An ambiguous crash is retained, never replayed blindly．
        with self.db() as db:
            changed = db.execute("UPDATE outbox SET state='sending' WHERE job=? AND ordinal=? AND state='pending'",
                (identifier, ordinal)).rowcount
        return changed == 1

    def delivered(self, identifier: str, ordinal: int, message_id: int) -> None:
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("delivery_message_invalid")
        with self.db() as db:
            db.execute("UPDATE outbox SET state='sent', message=? WHERE job=? AND ordinal=? AND state='sending'",
                (str(message_id), identifier, ordinal))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from .formal_runtime import exclusive_lock
    from .owner_repair_pipeline import RepairController, load_config
    config = load_config(args.config)
    controller = RepairController(config)
    # An OS-held lock prevents two controller processes handling the same queue．
    with exclusive_lock(config.state_dir / "controller.lock"):
        print("EPHY_OWNER_REPAIR_CONTROLLER_READY", flush=True)
        while True:
            controller.tick()
            if args.once:
                break
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
