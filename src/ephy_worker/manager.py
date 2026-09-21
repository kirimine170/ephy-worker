"""Phase 2 manager サービス（契約 0.4）：web.collect の job queue・worker registry・artifact store．

設計（docs/adr/0003）：

- 1 worker = 1 process，1 job = 1 attempt（並列 claim なし）．
- ``target_worker_id`` 必須の queue であり，broadcast claim はしない．
- job state：queued / running / cancel_requested / completed / partial /
  failed / cancelled / lost．
- queued への cancel は即 cancelled（再 queue しない）．running への cancel は
  cancel_requested として記録し，worker の finalization を待つ．
- waiting 理由を構造化して返す（未登録／offline／capability・contract 不一致）．
- artifact は job + attempt に紐づけ，名前は安全文字列，media type は許可リスト，
  download は job scope のみ．
- retry は parent の残 budget と残時間から child budget を導出する（全量再付与しない）．
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from aiohttp import web
from pydantic import ValidationError

from .contracts import (
    CONTRACT_VERSION,
    JOB_KIND,
    TERMINAL_STATES,
    ArtifactRef,
    JobUsage,
    ResultEnvelope,
    SubmitJob,
    WorkerRegistration,
    job_input_hash,
    required_capabilities,
)
from .schema import Limits
from .store import output_root

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
RETRY_MIN_REMAINING_SECONDS = 30.0
RETRY_TIME_MARGIN_SECONDS = 10.0
RETRY_MIN_CHILD_SECONDS = 15.0
ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
ARTIFACT_MEDIA_TYPES = {
    "application/json",
    "text/html",
    "application/pdf",
    "text/plain",
}


class ManagerError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


def serialized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    submit_key TEXT NOT NULL,
    parent_job_id TEXT,
    kind TEXT NOT NULL,
    mode TEXT NOT NULL,
    input TEXT NOT NULL,
    target_worker_id TEXT NOT NULL,
    budget TEXT NOT NULL,
    deadline_seconds REAL NOT NULL,
    input_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    state TEXT NOT NULL,
    worker_id TEXT,
    attempt_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    result TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_submit ON jobs (submit_key, input_hash);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_submit_key ON jobs (submit_key);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    attempt_number INTEGER NOT NULL,
    created_at REAL NOT NULL,
    finalized_at REAL
);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL,
    implementation_version TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    os TEXT NOT NULL,
    architecture TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'online',
    last_heartbeat_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    stored_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_job ON artifacts (job_id);
"""

_MIGRATED_JOB_COLUMNS = {
    "parent_job_id": "TEXT",
    "kind": "TEXT NOT NULL DEFAULT 'web.collect'",
    "mode": "TEXT NOT NULL DEFAULT 'search'",
    "target_worker_id": "TEXT NOT NULL DEFAULT ''",
}


class Manager:
    """SQLite単一接続の同期core．HTTP層はexecutor.runでasync化する．"""

    def __init__(self, settings):
        self.settings = settings
        self.database_path = Path(settings.database_path).expanduser().resolve()
        output_root(self.database_path.parent)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = output_root(settings.artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.artifact_dir, 0o700)
        self.lease_seconds = settings.lease_ttl_seconds
        self.offline_after_seconds = settings.worker_offline_seconds
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        if os.name == "posix":
            os.chmod(self.database_path, 0o600)
        self._conn.executescript(_SCHEMA)
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for name, decl in _MIGRATED_JOB_COLUMNS.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- workers ----------------------------------------------------------

    def _check_worker_credential(self, worker_id: str, credential: str) -> None:
        expected_env = self.settings.worker_credentials_env.get(worker_id)
        if expected_env is None:
            raise ManagerError("unknown_worker", 401)
        expected = os.environ.get(expected_env, "")
        if not expected or not hmac.compare_digest(credential, expected):
            raise ManagerError("unauthorized", 401)

    @serialized
    def upsert_worker(self, worker_id: str, credential: str, registration: WorkerRegistration) -> dict:
        self._check_worker_credential(worker_id, credential)
        if registration.worker_id != worker_id:
            raise ManagerError("worker_id_mismatch", 400)
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO workers (worker_id, capabilities, implementation_version, contract_version,
                                 os, architecture, concurrency, state, last_heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?)
            ON CONFLICT (worker_id) DO UPDATE SET
                capabilities = excluded.capabilities,
                implementation_version = excluded.implementation_version,
                contract_version = excluded.contract_version,
                os = excluded.os,
                architecture = excluded.architecture,
                concurrency = excluded.concurrency,
                state = 'online',
                last_heartbeat_at = excluded.last_heartbeat_at
            """,
            (
                worker_id,
                json.dumps(registration.capabilities),
                registration.implementation_version,
                registration.contract,
                registration.os,
                registration.architecture,
                registration.concurrency,
                now,
            ),
        )
        self._conn.commit()
        return self.worker_view(worker_id)

    def _worker_row(self, worker_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()

    @serialized
    def worker_view(self, worker_id: str) -> dict:
        row = self._worker_row(worker_id)
        if row is None:
            raise ManagerError("unknown_worker", 404)
        return {
            "worker_id": row["worker_id"],
            "capabilities": json.loads(row["capabilities"]),
            "implementation_version": row["implementation_version"],
            "contract_version": row["contract_version"],
            "os": row["os"],
            "architecture": row["architecture"],
            "concurrency": row["concurrency"],
            "state": row["state"],
            "last_heartbeat_at": row["last_heartbeat_at"],
        }

    @serialized
    def list_workers(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
        return [
            {
                "worker_id": row["worker_id"],
                "capabilities": json.loads(row["capabilities"]),
                "implementation_version": row["implementation_version"],
                "contract_version": row["contract_version"],
                "os": row["os"],
                "architecture": row["architecture"],
                "concurrency": row["concurrency"],
                "state": row["state"],
                "last_heartbeat_at": row["last_heartbeat_at"],
            }
            for row in rows
        ]

    # -- jobs -------------------------------------------------------------

    def _job_row(self, job_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def _wait_reason(self, row: sqlite3.Row) -> str | None:
        worker = self._worker_row(row["target_worker_id"])
        if worker is None:
            return "target_worker_unregistered"
        if worker["state"] != "online":
            return "target_worker_offline"
        if worker["contract_version"] != CONTRACT_VERSION:
            return "target_worker_contract_mismatch"
        capabilities = set(json.loads(worker["capabilities"]))
        if not set(required_capabilities(row["mode"])) <= capabilities:
            return "target_worker_capability_mismatch"
        return None

    def _job_view(self, row: sqlite3.Row) -> dict:
        result = json.loads(row["result"]) if row["result"] else None
        usage = JobUsage.model_validate((result or {}).get("usage", {})).model_dump() if result else JobUsage().model_dump()
        view = {
            "job_id": row["job_id"],
            "contract": CONTRACT_VERSION,
            "submit_key": row["submit_key"],
            "parent_job_id": row["parent_job_id"],
            "kind": row["kind"],
            "mode": row["mode"],
            "input": json.loads(row["input"]),
            "target_worker_id": row["target_worker_id"],
            "budget": json.loads(row["budget"]),
            "deadline_seconds": row["deadline_seconds"],
            "input_hash": row["input_hash"],
            "created_at": row["created_at"],
            "deadline_at": row["deadline_at"],
            "state": row["state"],
            "worker_id": row["worker_id"],
            "attempt_id": row["attempt_id"],
            "attempt_number": row["attempt_number"],
            "wait_reason": self._wait_reason(row) if row["state"] == "queued" else None,
            "stop_reason": row["stop_reason"],
            "usage": usage,
            "result": result,
        }
        return view

    @serialized
    def job_view(self, job_id: str) -> dict:
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        return self._job_view(row)

    @serialized
    def job_result(self, job_id: str) -> dict:
        view = self.job_view(job_id)
        if view["state"] not in TERMINAL_STATES:
            raise ManagerError("not_terminal", 409)
        if view["result"] is None:
            raise ManagerError("result_unavailable", 404)
        return view["result"]

    def _root_job(self, row: sqlite3.Row) -> sqlite3.Row:
        current = row
        while current["parent_job_id"] is not None:
            parent = self._job_row(current["parent_job_id"])
            if parent is None:
                raise ManagerError("unknown_parent_job", 404)
            current = parent
        return current

    def _validate_child_allocation(self, parent: sqlite3.Row, submit: SubmitJob) -> None:
        if parent["state"] not in TERMINAL_STATES:
            raise ManagerError("parent_not_terminal", 409)
        root = self._root_job(parent)
        now = time.time()
        if now + submit.deadline_seconds > root["deadline_at"]:
            raise ManagerError("child_deadline_exceeds_parent", 409)
        root_limits = Limits.model_validate(json.loads(root["budget"]))
        used = {"search_requests": 0, "fetch_requests": 0, "model_requests": 0}
        rows = self._conn.execute(
            """
            WITH RECURSIVE lineage(job_id) AS (
              SELECT job_id FROM jobs WHERE job_id = ?
              UNION ALL
              SELECT jobs.job_id FROM jobs JOIN lineage ON jobs.parent_job_id = lineage.job_id
            )
            SELECT jobs.* FROM jobs JOIN lineage USING (job_id)
            """,
            (root["job_id"],),
        ).fetchall()
        for row in rows:
            result = json.loads(row["result"]) if row["result"] else None
            if result is not None:
                usage = JobUsage.model_validate(result.get("usage", {}))
                used["search_requests"] += usage.search_requests
                used["fetch_requests"] += usage.fetch_requests
                used["model_requests"] += usage.model_requests
            elif row["job_id"] != root["job_id"] and row["state"] not in {"cancelled"}:
                reserved = Limits.model_validate(json.loads(row["budget"]))
                used["search_requests"] += reserved.search_requests
                used["fetch_requests"] += reserved.fetch_requests
                used["model_requests"] += reserved.model_requests
        available = {
            "max_queries": root_limits.max_queries - used["search_requests"],
            "search_requests": root_limits.search_requests - used["search_requests"],
            "max_sources": root_limits.max_sources - used["fetch_requests"],
            "fetch_requests": root_limits.fetch_requests - used["fetch_requests"],
            "model_requests": root_limits.model_requests - used["model_requests"],
        }
        for field, remaining in available.items():
            if getattr(submit.budget, field) > remaining:
                raise ManagerError("child_budget_exceeds_parent", 409)

    @serialized
    def submit(self, submit: SubmitJob) -> tuple[dict, bool]:
        if submit.contract != CONTRACT_VERSION:
            raise ManagerError("unsupported_contract", 409)
        payload = submit.input
        input_hash = job_input_hash(payload.model_dump())
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM jobs WHERE submit_key = ?", (submit.submit_key,)
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise ManagerError("submit_key_conflict", 409)
                return self._job_view(existing), False
            if submit.parent_job_id is not None:
                parent = self._job_row(submit.parent_job_id)
                if parent is None:
                    raise ManagerError("unknown_parent_job", 404)
                self._validate_child_allocation(parent, submit)
            now = time.time()
            job_id = uuid.uuid4().hex
            self._conn.execute(
                """
                INSERT INTO jobs (job_id, submit_key, parent_job_id, kind, mode, input, target_worker_id,
                                  budget, deadline_seconds, input_hash, created_at, deadline_at, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
                """,
                (
                    job_id,
                    submit.submit_key,
                    submit.parent_job_id,
                    submit.kind,
                    payload.mode,
                    json.dumps(payload.model_dump(), ensure_ascii=False),
                    submit.target_worker_id,
                    json.dumps(submit.budget.model_dump()),
                    submit.deadline_seconds,
                    input_hash,
                    now,
                    now + submit.deadline_seconds,
                ),
            )
            self._conn.commit()
            return self._job_view(self._job_row(job_id)), True

    @serialized
    def claim(self, worker_id: str, credential: str) -> dict | None:
        self._check_worker_credential(worker_id, credential)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                worker = self._worker_row(worker_id)
                if worker is None or worker["state"] != "online":
                    self._conn.rollback()
                    return None
                if worker["contract_version"] != CONTRACT_VERSION:
                    self._conn.rollback()
                    return None
                active = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE worker_id = ? AND state IN ('running','cancel_requested')",
                    (worker_id,),
                ).fetchone()["n"]
                if active >= worker["concurrency"]:
                    self._conn.rollback()
                    return None
                capabilities = set(json.loads(worker["capabilities"]))
                now = time.time()
                row = self._conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE state = 'queued' AND target_worker_id = ? AND deadline_at > ?
                    ORDER BY created_at ASC, job_id ASC
                    LIMIT 1
                    """,
                    (worker_id, now),
                ).fetchone()
                if row is None or not set(required_capabilities(row["mode"])) <= capabilities:
                    self._conn.rollback()
                    return None
                attempt_id = uuid.uuid4().hex
                lease_token = uuid.uuid4().hex
                attempt_number = row["attempt_number"] + 1
                self._conn.execute(
                    """
                    INSERT INTO attempts (attempt_id, job_id, worker_id, lease_token, lease_expires_at,
                                          attempt_number, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (attempt_id, row["job_id"], worker_id, lease_token,
                     now + self.lease_seconds, attempt_number, now),
                )
                changed = self._conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = ?, attempt_id = ?, attempt_number = ? "
                    "WHERE job_id = ? AND state = 'queued'",
                    (worker_id, attempt_id, attempt_number, row["job_id"]),
                ).rowcount
                if changed != 1:
                    raise ManagerError("claim_conflict", 409)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return {
                "job": self._job_view(self._job_row(row["job_id"])),
                "attempt": {
                    "job_id": row["job_id"],
                    "attempt_id": attempt_id,
                    "worker_id": worker_id,
                    "lease_token": lease_token,
                    "lease_expires_at": now + self.lease_seconds,
                    "attempt_number": attempt_number,
                },
            }

    def _attempt_row(self, job_id: str, attempt_id: str | None) -> sqlite3.Row | None:
        if attempt_id is None:
            return None
        return self._conn.execute(
            "SELECT * FROM attempts WHERE job_id = ? AND attempt_id = ?", (job_id, attempt_id)
        ).fetchone()

    @serialized
    def renew_lease(self, job_id: str, worker_id: str, credential: str, attempt_id: str, lease_token: str) -> dict:
        self._check_worker_credential(worker_id, credential)
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        attempt = self._attempt_row(job_id, attempt_id)
        if attempt is None or attempt["worker_id"] != worker_id or attempt["lease_token"] != lease_token:
            raise ManagerError("lease_conflict", 409)
        if attempt["finalized_at"] is not None or row["attempt_id"] != attempt_id:
            raise ManagerError("attempt_not_active", 409)
        if row["state"] in TERMINAL_STATES:
            return {"job_id": job_id, "attempt_id": attempt_id, "job_state": row["state"], "lease_expires_at": None}
        now = time.time()
        if attempt["lease_expires_at"] < now:
            raise ManagerError("lease_expired", 410)
        if row["deadline_at"] < now:
            return {"job_id": job_id, "attempt_id": attempt_id, "job_state": "deadline_passed", "lease_expires_at": None}
        self._conn.execute(
            "UPDATE attempts SET lease_expires_at = ? WHERE attempt_id = ?",
            (min(now + self.lease_seconds, row["deadline_at"]), attempt_id),
        )
        self._conn.commit()
        return {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "job_state": row["state"],
            "lease_expires_at": min(now + self.lease_seconds, row["deadline_at"]),
        }

    @serialized
    def cancel(self, job_id: str) -> dict:
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        if row["state"] in TERMINAL_STATES:
            return self._job_view(row)
        if row["state"] == "queued":
            # queued の cancel は即 cancelled．再 queue に戻さない．
            self._conn.execute(
                "UPDATE jobs SET state = 'cancelled', stop_reason = 'cancelled_by_requester' WHERE job_id = ?",
                (job_id,),
            )
        elif row["state"] == "running":
            self._conn.execute("UPDATE jobs SET state = 'cancel_requested' WHERE job_id = ?", (job_id,))
        self._conn.commit()
        return self._job_view(self._job_row(job_id))

    @serialized
    def finalize(
        self,
        job_id: str,
        worker_id: str,
        credential: str,
        lease_token: str,
        result: ResultEnvelope,
    ) -> dict:
        self._check_worker_credential(worker_id, credential)
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        attempt = self._attempt_row(job_id, row["attempt_id"])
        if attempt is None or attempt["worker_id"] != worker_id or attempt["lease_token"] != lease_token:
            raise ManagerError("lease_conflict", 409)
        if attempt["finalized_at"] is not None:
            stored = json.loads(row["result"]) if row["result"] else None
            if stored == result.model_dump(mode="json"):
                return self._job_view(row)
            raise ManagerError("result_conflict", 409)
        if attempt["lease_expires_at"] < time.time():
            raise ManagerError("lease_expired", 409)
        if result.job_id != job_id or result.attempt_id != attempt["attempt_id"]:
            raise ManagerError("attempt_mismatch", 409)
        if result.payload.mode != row["mode"]:
            raise ManagerError("mode_mismatch", 409)
        state = row["state"]
        status = result.status
        if state == "running" and status in {"succeeded", "partial", "failed"}:
            new_state = {"succeeded": "completed", "partial": "partial", "failed": "failed"}[status]
        elif state == "cancel_requested" and status == "cancelled":
            new_state = "cancelled"
        elif state == "running" and status == "cancelled" or state == "cancel_requested":
            raise ManagerError("result_conflicts_with_cancel", 409)
        else:
            raise ManagerError("not_runnable", 409)
        # manifest の各 ref がこの attempt の保存済み artifact と一致すること．
        for ref in result.manifest:
            stored = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? AND job_id = ? AND attempt_id = ?",
                (ref.artifact_id, job_id, attempt["attempt_id"]),
            ).fetchone()
            if stored is None:
                raise ManagerError("artifact_missing", 409)
            if (
                stored["name"] != ref.name
                or stored["media_type"] != ref.media_type
                or stored["bytes"] != ref.bytes
                or stored["sha256"] != ref.sha256
                or stored["schema_version"] != ref.schema_version
            ):
                raise ManagerError("artifact_mismatch", 409)
        stop_reason = result.stop_reason
        if new_state == "cancelled" and stop_reason is None:
            stop_reason = "cancelled_by_requester"
        self._conn.execute(
            "UPDATE jobs SET state = ?, stop_reason = ?, result = ? WHERE job_id = ?",
            (new_state, stop_reason, json.dumps(result.model_dump(), ensure_ascii=False), job_id),
        )
        self._conn.execute(
            "UPDATE attempts SET finalized_at = ? WHERE attempt_id = ?",
            (time.time(), attempt["attempt_id"]),
        )
        self._conn.commit()
        return self._job_view(self._job_row(job_id))

    @serialized
    def retry(self, job_id: str, submit_key: str | None = None) -> tuple[dict, bool]:
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        if row["state"] not in {"failed", "partial", "lost"}:
            raise ManagerError("not_retryable", 409)
        now = time.time()
        remaining = max(0.0, row["deadline_at"] - now)
        if remaining < RETRY_MIN_REMAINING_SECONDS:
            raise ManagerError("no_remaining_time", 409)
        result = json.loads(row["result"]) if row["result"] else None
        if row["state"] == "lost" and result is None:
            # The worker may have consumed any part of the assigned allowance．Without
            # a signed result there is no safe amount to return to the parent budget．
            raise ManagerError("no_remaining_budget", 409)
        usage = JobUsage.model_validate((result or {}).get("usage", {})) if result else JobUsage()
        parent = Limits.model_validate(json.loads(row["budget"]))
        # 残り budget = parent budget - 既知 usage - 保守的な予約（search 1 回・fetch 1 回）．
        remaining_queries = parent.max_queries - usage.search_requests - 1
        remaining_search = parent.search_requests - usage.search_requests - 1
        remaining_sources = parent.max_sources - usage.fetch_requests - 1
        remaining_fetch = parent.fetch_requests - usage.fetch_requests - 1
        if min(remaining_queries, remaining_search, remaining_sources, remaining_fetch) < 1:
            raise ManagerError("no_remaining_budget", 409)
        child_budget = Limits(
            max_queries=remaining_queries,
            search_requests=remaining_search,
            max_sources=remaining_sources,
            fetch_requests=remaining_fetch,
            model_requests=max(0, parent.model_requests - usage.model_requests),
            job_seconds=max(RETRY_MIN_CHILD_SECONDS, min(parent.job_seconds, remaining - RETRY_TIME_MARGIN_SECONDS)),
            request_seconds=parent.request_seconds,
            parser_seconds=parent.parser_seconds,
            parser_memory_mb=parent.parser_memory_mb,
            html_bytes=parent.html_bytes,
            pdf_bytes=parent.pdf_bytes,
            text_chars=parent.text_chars,
            pdf_pages=parent.pdf_pages,
            cache_bytes=parent.cache_bytes,
            initial_sources=parent.initial_sources,
            passage_chars=parent.passage_chars,
            selected_passages=parent.selected_passages,
            max_redirects=parent.max_redirects,
        )
        children = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE parent_job_id = ?", (job_id,)
        ).fetchone()["n"]
        key = submit_key or f"{row['submit_key']}:retry:{children + 1}"
        payload = SubmitJob(
            contract=CONTRACT_VERSION,
            submit_key=key,
            parent_job_id=job_id,
            kind=JOB_KIND,
            input=json.loads(row["input"]),
            target_worker_id=row["target_worker_id"],
            budget=child_budget,
            deadline_seconds=max(RETRY_MIN_CHILD_SECONDS, min(remaining - RETRY_TIME_MARGIN_SECONDS, 3600.0)),
        )
        # input の型再検証（discriminated union を通す）．
        payload = SubmitJob.model_validate(payload.model_dump())
        return self.submit(payload)

    @serialized
    def delete_job(self, job_id: str) -> dict:
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        if row["state"] not in TERMINAL_STATES:
            raise ManagerError("not_terminal", 409)
        artifacts = self._conn.execute(
            "SELECT * FROM artifacts WHERE job_id = ?", (job_id,)
        ).fetchall()
        for artifact in artifacts:
            try:
                Path(artifact["stored_path"]).unlink(missing_ok=True)
            except OSError:
                pass
        self._conn.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
        self._conn.execute("DELETE FROM attempts WHERE job_id = ?", (job_id,))
        self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        self._conn.commit()
        job_dir = self.artifact_dir / job_id
        if job_dir.is_dir():
            shutil.rmtree(job_dir, ignore_errors=True)
        return {"job_id": job_id, "deleted_artifacts": len(artifacts)}

    # -- artifacts ----------------------------------------------------------

    def _active_attempt(self, job_id: str, attempt_id: str) -> sqlite3.Row:
        row = self._attempt_row(job_id, attempt_id)
        if row is None:
            raise ManagerError("unknown_attempt", 404)
        return row

    @serialized
    def add_artifact(
        self,
        job_id: str,
        worker_id: str,
        credential: str,
        attempt_id: str,
        lease_token: str,
        ref: ArtifactRef,
        data: bytes,
    ) -> ArtifactRef:
        self._check_worker_credential(worker_id, credential)
        if len(data) != ref.bytes:
            raise ManagerError("artifact_size_mismatch", 409)
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ManagerError("artifact_sha_mismatch", 409)
        if not ARTIFACT_NAME_PATTERN.fullmatch(ref.name):
            raise ManagerError("invalid_artifact_name", 400)
        if ".." in ref.name.split("."):
            raise ManagerError("invalid_artifact_name", 400)
        if ref.media_type not in ARTIFACT_MEDIA_TYPES:
            raise ManagerError("invalid_artifact_media_type", 400)
        job = self._job_row(job_id)
        if job is None:
            raise ManagerError("unknown_job", 404)
        attempt = self._active_attempt(job_id, attempt_id)
        if (
            attempt["worker_id"] != worker_id
            or attempt["lease_token"] != lease_token
            or job["attempt_id"] != attempt_id
        ):
            raise ManagerError("lease_conflict", 409)
        if attempt["finalized_at"] is not None or job["state"] not in {"running", "cancel_requested"}:
            raise ManagerError("attempt_not_active", 409)
        if attempt["lease_expires_at"] < time.time():
            raise ManagerError("lease_expired", 410)
        count = self._conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE job_id = ?", (job_id,)
        ).fetchone()["n"]
        existing_id = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (ref.artifact_id,)
        ).fetchone()
        if existing_id is not None:
            if (
                existing_id["job_id"] == job_id
                and existing_id["attempt_id"] == attempt_id
                and existing_id["name"] == ref.name
                and existing_id["media_type"] == ref.media_type
                and existing_id["bytes"] == ref.bytes
                and existing_id["sha256"] == ref.sha256
                and existing_id["schema_version"] == ref.schema_version
            ):
                return ref
            raise ManagerError("artifact_id_conflict", 409)
        total_bytes = self._conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS n FROM artifacts WHERE job_id = ?", (job_id,)
        ).fetchone()["n"]
        job_limits = Limits.model_validate(json.loads(job["budget"]))
        if total_bytes + ref.bytes > job_limits.cache_bytes:
            raise ManagerError("artifact_bytes_limit", 409)
        if count >= self.settings.max_artifacts_per_job:
            raise ManagerError("artifact_limit", 409)
        existing = self._conn.execute(
            "SELECT artifact_id FROM artifacts WHERE job_id = ? AND name = ?", (job_id, ref.name)
        ).fetchone()
        if existing is not None and existing["artifact_id"] != ref.artifact_id:
            raise ManagerError("artifact_name_conflict", 409)
        target_dir = self.artifact_dir / job_id / attempt["attempt_id"]
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(target_dir, 0o700)
        target = target_dir / ref.name
        fd, temp_name = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        self._conn.execute(
            """
            INSERT INTO artifacts (artifact_id, job_id, attempt_id, name, media_type, bytes, sha256,
                                   schema_version, stored_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.artifact_id,
                job_id,
                attempt["attempt_id"],
                ref.name,
                ref.media_type,
                ref.bytes,
                ref.sha256,
                ref.schema_version,
                str(target),
            ),
        )
        self._conn.commit()
        return ref

    @serialized
    def get_artifact(self, job_id: str, artifact_id: str) -> tuple[ArtifactRef, Path]:
        row = self._job_row(job_id)
        if row is None:
            raise ManagerError("unknown_job", 404)
        artifact = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND job_id = ?", (artifact_id, job_id)
        ).fetchone()
        if artifact is None:
            raise ManagerError("unknown_artifact", 404)
        path = Path(artifact["stored_path"])
        if not path.is_file():
            raise ManagerError("artifact_missing_on_disk", 500)
        ref = ArtifactRef(
            artifact_id=artifact["artifact_id"],
            name=artifact["name"],
            media_type=artifact["media_type"],
            bytes=artifact["bytes"],
            sha256=artifact["sha256"],
            schema_version=artifact["schema_version"],
        )
        return ref, path

    # -- sweeper ------------------------------------------------------------

    @serialized
    def sweep_once(self) -> dict:
        now = time.time()
        lost = self._conn.execute(
            "UPDATE jobs SET state = 'lost', stop_reason = 'lease_expired' "
            "WHERE state = 'running' AND EXISTS ("
            "  SELECT 1 FROM attempts a WHERE a.job_id = jobs.job_id AND a.attempt_id = jobs.attempt_id "
            "  AND a.lease_expires_at < ?)"
            , (now,)
        ).rowcount
        # cancel_requested is deliberately retained after a lease expires．Only the
        # assigned worker can confirm that execution stopped by finalizing a
        # cancelled result．A communications failure is not stop confirmation．
        cancel_unconfirmed = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state = 'cancel_requested' AND EXISTS ("
            "  SELECT 1 FROM attempts a WHERE a.job_id = jobs.job_id AND a.attempt_id = jobs.attempt_id "
            "  AND a.lease_expires_at < ?)",
            (now,),
        ).fetchone()["n"]
        expired = self._conn.execute(
            "UPDATE jobs SET state = 'lost', stop_reason = 'deadline_expired' "
            "WHERE state = 'queued' AND deadline_at < ?",
            (now,),
        ).rowcount
        offline = self._conn.execute(
            "UPDATE workers SET state = 'offline' WHERE state = 'online' AND last_heartbeat_at < ?",
            (now - self.offline_after_seconds,),
        ).rowcount
        self._conn.commit()
        stale_files = 0
        for temp_file in self.artifact_dir.glob("*/*/.tmp-*"):
            try:
                if time.time() - temp_file.stat().st_mtime > 3600:
                    temp_file.unlink()
                    stale_files += 1
            except OSError:
                pass
        return {"lost": lost, "cancel_unconfirmed": cancel_unconfirmed, "expired_queued": expired, "offline_workers": offline,
                "stale_temp_files": stale_files}

    def wait_for_state(self, job_id: str, states: set[str], timeout: float = 30.0) -> dict:
        """同期 polling helper（テスト・CLI の待機用）．"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.job_view(job_id)
            if view["state"] in states:
                return view
            time.sleep(0.02)
        raise ManagerError("timeout", 408)


class ManagerApp:
    def __init__(self, settings):
        self.manager = Manager(settings)
        self.settings = settings
        self._sweeper_task: asyncio.Task | None = None
        self.artifact_limit = min(settings.max_artifact_bytes, MAX_ARTIFACT_BYTES)
        self.app = web.Application(middlewares=[_manager_error_middleware])
        self.app.on_startup.append(self._startup)
        self.app.on_cleanup.append(self._cleanup)
        self.app.router.add_get("/health", self._health)
        self.app.router.add_get("/workers", self._list_workers)
        self.app.router.add_post("/workers/register", self._register_worker)
        self.app.router.add_post("/jobs", self._submit_job)
        self.app.router.add_post("/jobs/claim", self._claim_job)
        self.app.router.add_get("/jobs/{job_id}", self._get_job)
        self.app.router.add_post("/jobs/{job_id}/cancel", self._cancel_job)
        self.app.router.add_post("/jobs/{job_id}/retry", self._retry_job)
        self.app.router.add_get("/jobs/{job_id}/result", self._get_result)
        self.app.router.add_delete("/jobs/{job_id}", self._delete_job)
        self.app.router.add_post("/jobs/{job_id}/lease", self._renew_lease)
        self.app.router.add_post("/jobs/{job_id}/finalize", self._finalize_job)
        self.app.router.add_post("/jobs/{job_id}/artifacts", self._upload_artifact)
        self.app.router.add_get("/jobs/{job_id}/artifacts/{artifact_id}", self._download_artifact)

    async def _startup(self, _app) -> None:
        await asyncio.to_thread(self.manager.sweep_once)
        self._sweeper_task = asyncio.create_task(self._sweeper_loop(), name="manager-sweeper")

    async def _sweeper_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await asyncio.to_thread(self.manager.sweep_once)

    async def _cleanup(self, _app) -> None:
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
        self.manager.close()

    def _requester_credential(self) -> str:
        env = self.settings.requester_credential_env
        return os.environ.get(env, "")

    async def _authenticate_requester(self, request: web.Request) -> None:
        expected = self._requester_credential()
        provided = request.headers.get("X-Ephy-Credential", "")
        if not expected or not hmac.compare_digest(provided, expected):
            raise ManagerError("unauthorized", 401)

    async def _authenticate_worker(self, request: web.Request, worker_id: str, credential: str) -> None:
        await asyncio.to_thread(self.manager._check_worker_credential, worker_id, credential)

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "contract": CONTRACT_VERSION, "kind": JOB_KIND})

    async def _list_workers(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        return web.json_response(await asyncio.to_thread(self.manager.list_workers))

    async def _register_worker(self, request: web.Request) -> web.Response:
        worker_id = request.headers.get("X-Ephy-Worker-Id", "")
        credential = request.headers.get("X-Ephy-Credential", "")
        body = await _read_json(request, 1024 * 1024)
        try:
            registration = WorkerRegistration.model_validate(body)
        except (ValidationError, TypeError) as exc:
            raise ManagerError("invalid_payload", 422) from exc
        await self._authenticate_worker(request, worker_id, credential)
        view = await asyncio.to_thread(self.manager.upsert_worker, worker_id, credential, registration)
        return web.json_response(view)

    async def _submit_job(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        body = await _read_json(request, 256 * 1024)
        try:
            submit = SubmitJob.model_validate(body)
        except (ValidationError, TypeError) as exc:
            raise ManagerError("invalid_payload", 422) from exc
        view, created = await asyncio.to_thread(self.manager.submit, submit)
        return web.json_response({**view, "created": created}, status=202 if created else 200)

    async def _get_job(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        view = await asyncio.to_thread(self.manager.job_view, request.match_info["job_id"])
        return web.json_response(view)

    async def _cancel_job(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        view = await asyncio.to_thread(self.manager.cancel, request.match_info["job_id"])
        return web.json_response(view)

    async def _retry_job(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        body = await _read_json(request, 64 * 1024)
        submit_key = body.get("submit_key") if body else None
        if submit_key is not None and not isinstance(submit_key, str):
            raise ManagerError("invalid_payload", 422)
        view, created = await asyncio.to_thread(self.manager.retry, request.match_info["job_id"], submit_key)
        return web.json_response({**view, "created": created}, status=202 if created else 200)

    async def _get_result(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        view = await asyncio.to_thread(self.manager.job_view, request.match_info["job_id"])
        if view["state"] not in TERMINAL_STATES:
            raise ManagerError("not_terminal", 409)
        return web.json_response(
            {
                "job_id": view["job_id"],
                "state": view["state"],
                "stop_reason": view["stop_reason"],
                "usage": view["usage"],
                "result": view["result"],
            }
        )

    async def _delete_job(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        result = await asyncio.to_thread(self.manager.delete_job, request.match_info["job_id"])
        return web.json_response(result, status=202)

    async def _claim_job(self, request: web.Request) -> web.Response:
        worker_id = request.headers.get("X-Ephy-Worker-Id", "")
        credential = request.headers.get("X-Ephy-Credential", "")
        await self._authenticate_worker(request, worker_id, credential)
        claim = await asyncio.to_thread(self.manager.claim, worker_id, credential)
        if claim is None:
            return web.json_response({"job": None, "attempt": None})
        return web.json_response(claim)

    async def _renew_lease(self, request: web.Request) -> web.Response:
        worker_id = request.headers.get("X-Ephy-Worker-Id", "")
        credential = request.headers.get("X-Ephy-Credential", "")
        attempt_id = request.headers.get("X-Ephy-Attempt-Id", "")
        lease_token = request.headers.get("X-Ephy-Lease-Token", "")
        await self._authenticate_worker(request, worker_id, credential)
        info = await asyncio.to_thread(
            self.manager.renew_lease, request.match_info["job_id"], worker_id, credential, attempt_id, lease_token
        )
        status = 410 if info["job_state"] in TERMINAL_STATES or info["job_state"] == "deadline_passed" else 200
        return web.json_response(info, status=status)

    async def _finalize_job(self, request: web.Request) -> web.Response:
        worker_id = request.headers.get("X-Ephy-Worker-Id", "")
        credential = request.headers.get("X-Ephy-Credential", "")
        lease_token = request.headers.get("X-Ephy-Lease-Token", "")
        await self._authenticate_worker(request, worker_id, credential)
        body = await _read_json(request, 8 * 1024 * 1024)
        try:
            result = ResultEnvelope.model_validate(body)
        except (ValidationError, TypeError) as exc:
            raise ManagerError("invalid_payload", 422) from exc
        view = await asyncio.to_thread(
            self.manager.finalize, request.match_info["job_id"], worker_id, credential, lease_token, result
        )
        return web.json_response(view)

    async def _upload_artifact(self, request: web.Request) -> web.Response:
        worker_id = request.headers.get("X-Ephy-Worker-Id", "")
        credential = request.headers.get("X-Ephy-Credential", "")
        await self._authenticate_worker(request, worker_id, credential)
        attempt_id = request.headers.get("X-Ephy-Attempt-Id", "")
        lease_token = request.headers.get("X-Ephy-Lease-Token", "")
        artifact_id = request.headers.get("X-Ephy-Artifact-Id", "")
        name = request.headers.get("X-Ephy-Artifact-Name", "")
        media_type = request.headers.get("X-Ephy-Artifact-Media-Type", "")
        sha256 = request.headers.get("X-Ephy-Artifact-SHA256", "")
        schema_version = request.headers.get("X-Ephy-Schema-Version", "1")
        try:
            declared_bytes = int(request.headers.get("X-Ephy-Artifact-Bytes", "0") or 0)
        except ValueError as exc:
            raise ManagerError("invalid_artifact_size", 400) from exc
        if declared_bytes < 1 or declared_bytes > self.artifact_limit:
            raise ManagerError("invalid_artifact_size", 400)
        data = await _read_body(request, self.artifact_limit)
        if len(data) != declared_bytes:
            raise ManagerError("artifact_size_mismatch", 409)
        try:
            ref = ArtifactRef(
                artifact_id=artifact_id,
                name=name,
                media_type=media_type,
                bytes=len(data),
                sha256=sha256,
                schema_version=int(schema_version),
            )
        except (ValidationError, ValueError) as exc:
            raise ManagerError("invalid_payload", 422) from exc
        job_id = request.match_info["job_id"]
        saved = await asyncio.to_thread(
            self.manager.add_artifact,
            job_id,
            worker_id,
            credential,
            attempt_id,
            lease_token,
            ref,
            data,
        )
        return web.json_response(saved.model_dump(), status=201)

    async def _download_artifact(self, request: web.Request) -> web.Response:
        await self._authenticate_requester(request)
        job_id = request.match_info["job_id"]
        artifact_id = request.match_info["artifact_id"]
        ref, path = await asyncio.to_thread(self.manager.get_artifact, job_id, artifact_id)
        return web.FileResponse(
            path,
            headers={"Content-Type": ref.media_type, "Cache-Control": "no-store"},
        )


async def _read_json(request: web.Request, max_bytes: int) -> dict:
    data = await _read_body(request, max_bytes)
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagerError("invalid_json", 422) from exc
    if not isinstance(body, dict):
        raise ManagerError("invalid_payload", 422)
    return body


async def _read_body(request: web.Request, max_bytes: int) -> bytes:
    data = bytearray()
    async for chunk in request.content.iter_any():
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ManagerError("payload_too_large", 413)
    return bytes(data)


async def _manager_error_handler(exc: BaseException) -> web.Response:
    if isinstance(exc, ManagerError):
        return web.json_response({"error": {"code": exc.code, "message": str(exc)}}, status=exc.status)
    if isinstance(exc, (ValidationError, TypeError)):
        return web.json_response({"error": {"code": "invalid_payload", "message": "invalid payload"}}, status=422)
    return web.json_response({"error": {"code": "internal_error", "message": type(exc).__name__}}, status=500)


@web.middleware
async def _manager_error_middleware(request: web.Request, handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — API boundary returns a credential-safe error body．
        return await _manager_error_handler(exc)


def create_app(settings) -> web.Application:
    return ManagerApp(settings).app


def implementation_version() -> str:
    try:
        from importlib.metadata import version

        return version("ephy-worker")
    except Exception:  # noqa: BLE001 — 未インストール開発 tree では fallback．
        return "dev"


def platform_info() -> dict:
    return {"os": platform.system(), "architecture": platform.machine()}
