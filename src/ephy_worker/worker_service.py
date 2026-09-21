"""worker サービス（契約 0.4）：manager から web.collect job を claim して決定論的に実行する．

- 1 worker process は 1 回の claim のみを保持する（並列 job なし）．
- lease は manager の lease TTL の半分ごとに更新する．更新で cancel_requested
  を観測したら即座に停止し，status=cancelled で finalize する．
- 成功・部分結果・失敗は result pack（JSON）として artifact 化し，manifest に
  登録してから finalize する．本文（html/pdf）は document hook 経由で artifact
  化する．upload 失敗は job 全体を失敗にしない（artifact 系の failure として記録）．
- SIGINT/SIGTERM は実行中 job の完了を待ってから worker を停止する
  （finalization 保証）．
"""

from __future__ import annotations

import asyncio
import platform
import signal
import sys
import time
import uuid
from contextlib import suppress

from .budget import Budget
from .collector import ExtractStageResult, SearchStageResult, run_extract_stage, run_search_stage
from .contracts import (
    COLLECT_CAPABILITY,
    CONTRACT_VERSION,
    RESULT_PACK_MEDIA_TYPE,
    RESULT_PACK_NAME,
    RESULT_PACK_SCHEMA_VERSION,
    TERMINAL_STATES,
    ArtifactRef,
    ExtractPayload,
    Failure,
    JobUsage,
    QueryResult,
    ResultEnvelope,
    SearchDiagnostic,
    SearchPayload,
    WorkerRegistration,
    canonical_json,
    sha256_hex,
)
from .fetch import PublicFetcher
from .manager_client import ManagerAPIError, ManagerClient
from .schema import Candidate, Limits, Query
from .search import create_search_provider

IMPLEMENTATION_VERSION = "0.4"
LEASE_SECONDS = 45.0
MAX_FAILURES_REPORTED = 200


class WorkerService:
    """1 worker process の lifecycle（register → claim → execute → finalize）．"""

    def __init__(
        self,
        client: ManagerClient,
        worker_id: str,
        *,
        search_config,
        search=None,
        fetcher: PublicFetcher | None = None,
        heartbeat_interval: float = 20.0,
        poll_interval: float = 2.0,
        lease_renew_ratio: float = 0.5,
    ):
        self.client = client
        self.worker_id = worker_id
        self.search_config = search_config
        self._search = search
        self._fetcher = fetcher
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.lease_renew_ratio = lease_renew_ratio
        self._stop = asyncio.Event()
        self._doc_refs: list[ArtifactRef] = []
        self._hook_errors: list[str] = []
        self._current_job: tuple[str, str, str] | None = None
        self._job_in_flight = False

    # ------------------------------------------------------------------ control

    def request_stop(self) -> None:
        """SIGINT/SIGTERM 用．実行中 job は完了（finalization）を待ってから停止する．"""
        self._stop.set()

    @property
    def job_in_flight(self) -> bool:
        return self._job_in_flight

    def _registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            contract=CONTRACT_VERSION,
            worker_id=self.worker_id,
            implementation_version=IMPLEMENTATION_VERSION,
            capabilities=[COLLECT_CAPABILITY],
            os=platform.system() or sys.platform,
            architecture=platform.machine() or "unknown",
            concurrency=1,
        )

    async def register(self) -> None:
        await self.client.register_worker(self.worker_id, self._registration().model_dump())

    # --------------------------------------------------------------------- loop

    async def run(self) -> int:
        """worker メインループ．SIGINT まで claim/execute を繰り返し，exit code を返す．"""
        await self.register()
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat")
        exit_code = 0
        try:
            while not self._stop.is_set():
                claimed = None
                try:
                    claimed = await self.client.claim_job(self.worker_id)
                except ManagerAPIError:
                    claimed = None
                if claimed is None:
                    await self._sleep_interruptible(self.poll_interval)
                    continue
                self._job_in_flight = True
                try:
                    code = await self._execute_job(claimed)
                    exit_code = exit_code or code
                finally:
                    self._job_in_flight = False
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await self._close_resources()
            await self.client.aclose()
        return exit_code

    async def _sleep_interruptible(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.register()
            except ManagerAPIError:
                continue

    async def _close_resources(self) -> None:
        if self._search is not None:
            with suppress(Exception):  # provider cleanup is best-effort during shutdown．
                await self._search.aclose()
        if self._fetcher is not None:
            with suppress(Exception):  # fetch cleanup is best-effort during shutdown．
                await self._fetcher.aclose()

    # -------------------------------------------------------------------- lease

    async def _lease_loop(
        self,
        job_id: str,
        attempt_id: str,
        lease_token: str,
        lease_expires_at: float,
        cancel_event: asyncio.Event,
        lost_event: asyncio.Event,
    ) -> None:
        """lease を定期更新し，cancel_requested／lost を event で通知する．"""
        interval = max(0.1, (lease_expires_at - time.time()) * self.lease_renew_ratio)
        while True:
            await asyncio.sleep(interval)
            if lost_event.is_set():
                return
            try:
                info = await self.client.renew_lease(self.worker_id, job_id, attempt_id, lease_token)
            except ManagerAPIError as exc:
                if exc.status in (404, 410) or exc.code in {"unknown_job", "lease_conflict", "lease_expired"}:
                    lost_event.set()
                    return
                continue
            state = info.get("job_state")
            if state == "cancel_requested":
                cancel_event.set()
                return
            if state in TERMINAL_STATES or state == "deadline_passed":
                lost_event.set()
                return
            renewed_until = float(info.get("lease_expires_at") or 0)
            interval = max(0.1, (renewed_until - time.time()) * self.lease_renew_ratio)

    # ----------------------------------------------------------------- execution

    async def _execute_job(self, claimed: dict) -> int:
        job = claimed["job"]
        attempt = claimed["attempt"]
        job_id = job["job_id"]
        attempt_id = attempt["attempt_id"]
        lease_token = attempt["lease_token"]
        self._doc_refs = []
        self._hook_errors = []
        self._current_job = (job_id, attempt_id, lease_token)
        cancel_event = asyncio.Event()
        lost_event = asyncio.Event()
        lease_task = asyncio.create_task(
            self._lease_loop(job_id, attempt_id, lease_token, float(attempt.get("lease_expires_at", time.time() + LEASE_SECONDS)), cancel_event, lost_event),
            name="worker-lease",
        )
        started = time.monotonic()
        try:
            limits = Limits.model_validate(job["budget"])
            budget = Budget(limits)
            stage = await self._run_stage(job, budget, cancel_event, lost_event)
            usage = _job_usage(stage.usage)
            status, stop_reason = _stage_status(job["mode"], stage)
            if cancel_event.is_set():
                status, stop_reason = "cancelled", "cancelled_by_requester"
            failures = _stage_failures(stage)
            for error in self._hook_errors:
                failures.append(Failure(stage="artifact", code="artifact_upload_failed", detail=error[:1000]))
            if job["mode"] == "search":
                payload = SearchPayload(
                    mode="search",
                    queries=[
                        QueryResult(
                            query_id=entry["query_id"],
                            text=entry["text"],
                            role=entry["role"],
                            reason=entry["reason"],
                            status=entry["status"],
                            result_count=entry["result_count"],
                            diagnostic=_diagnostic(entry.get("diagnostic")),
                        )
                        for entry in stage.queries
                    ],
                    candidates=list(stage.candidates),
                    failures=failures,
                )
            else:
                payload = ExtractPayload(
                    mode="extract",
                    sources=list(stage.sources),
                    failures=failures,
                )
            envelope = ResultEnvelope(
                contract=CONTRACT_VERSION,
                job_id=job_id,
                attempt_id=attempt_id,
                status=status,
                stop_reason=stop_reason,
                usage=usage,
                elapsed_seconds=round(time.monotonic() - started, 3),
                payload=payload,
                manifest=list(self._doc_refs),
            )
            await self._finalize(job_id, lease_token, envelope, cancel_fallback=True)
            return 0
        except ManagerAPIError as exc:
            # finalize の 409（cancel 競合で再 finalize 済み，sweeper が lost 化済み等）は
            # 管理側が状態を持っているので worker 側では諦める．
            if exc.status == 409:
                return 0
            return 1
        finally:
            lease_task.cancel()
            try:
                await lease_task
            except asyncio.CancelledError:
                pass
            self._current_job = None

    async def _run_stage(self, job: dict, budget: Budget, cancel_event: asyncio.Event, lost_event: asyncio.Event):
        cancel_check = lambda: cancel_event.is_set() or lost_event.is_set()
        if job["mode"] == "search":
            items = [
                (entry["query_id"], Query(text=entry["text"], role=entry["role"], reason=entry["reason"]))
                for entry in job["input"]["queries"]
            ]
            search = self._search
            owned = search is None
            if owned:
                search = create_search_provider(self.search_config, budget)
                self._search = search
            result = SearchStageResult()
            try:
                return await self._run_cancellable(
                    run_search_stage(
                        search,
                        budget,
                        items,
                        cancel_check=cancel_check,
                        result=result,
                    ),
                    result,
                    cancel_event,
                    lost_event,
                )
            finally:
                if owned:
                    with suppress(Exception):  # preserve the stage result if cleanup fails．
                        await search.aclose()
                    if self._search is search:
                        self._search = None
        else:
            items = [(f"s{i}", Candidate.model_validate(entry)) for i, entry in enumerate(job["input"]["candidates"], 1)]
            fetcher = self._fetcher
            owned = fetcher is None
            if owned:
                fetcher = PublicFetcher(budget, document_hook=self._document_hook)
                self._fetcher = fetcher
            result = ExtractStageResult()
            try:
                return await self._run_cancellable(
                    run_extract_stage(
                        fetcher,
                        budget,
                        items,
                        cancel_check=cancel_check,
                        result=result,
                    ),
                    result,
                    cancel_event,
                    lost_event,
                )
            finally:
                if owned:
                    with suppress(Exception):  # preserve the stage result if cleanup fails．
                        await fetcher.aclose()
                    if self._fetcher is fetcher:
                        self._fetcher = None

    async def _run_cancellable(self, coroutine, partial_result, cancel_event, lost_event):
        stage_task = asyncio.create_task(coroutine, name="worker-stage")
        cancel_task = asyncio.create_task(cancel_event.wait(), name="worker-cancel-wait")
        lost_task = asyncio.create_task(lost_event.wait(), name="worker-lease-loss-wait")
        waiters = (cancel_task, lost_task)
        try:
            done, _ = await asyncio.wait(
                (stage_task, *waiters),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stage_task in done:
                return await stage_task
            stage_task.cancel()
            with suppress(asyncio.CancelledError):
                await stage_task
            return partial_result
        finally:
            for task in waiters:
                task.cancel()
            for task in waiters:
                with suppress(asyncio.CancelledError):
                    await task

    # --------------------------------------------------------------- finalization

    async def _finalize(self, job_id: str, lease_token: str, envelope: ResultEnvelope, *, cancel_fallback: bool) -> None:
        pack_ref = await self._upload_result_pack(job_id, lease_token, envelope)
        envelope.manifest = [*envelope.manifest, pack_ref]
        try:
            await self.client.finalize_job(self.worker_id, job_id, lease_token, envelope.model_dump())
        except ManagerAPIError as exc:
            if cancel_fallback and exc.code == "result_conflicts_with_cancel":
                # request 側の cancel が lease 更新の観測より先に入った競合：
                # 集まった結果は payload に残し，status=cancelled で再 finalize する．
                fallback = envelope.model_copy(
                    update={"status": "cancelled", "stop_reason": "cancelled_by_requester"}
                )
                await self.client.finalize_job(self.worker_id, job_id, lease_token, fallback.model_dump())
            else:
                raise

    async def _upload_result_pack(
        self, job_id: str, lease_token: str, envelope: ResultEnvelope
    ) -> ArtifactRef:
        content = canonical_json(envelope.model_dump(mode="json"))
        artifact_id = uuid.uuid4().hex
        await self.client.upload_artifact(
            self.worker_id,
            job_id,
            envelope.attempt_id,
            lease_token,
            artifact_id,
            RESULT_PACK_NAME,
            RESULT_PACK_MEDIA_TYPE,
            content.encode("utf-8"),
            sha256_hex(content.encode("utf-8")),
            RESULT_PACK_SCHEMA_VERSION,
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            name=RESULT_PACK_NAME,
            media_type=RESULT_PACK_MEDIA_TYPE,
            bytes=len(content.encode("utf-8")),
            sha256=sha256_hex(content.encode("utf-8")),
            schema_version=RESULT_PACK_SCHEMA_VERSION,
        )

    async def _document_hook(self, source, body: bytes, kind: str) -> None:
        job_id, attempt_id, lease_token = self._current_job or ("", "", "")
        if kind not in ("html", "pdf") or not body:
            return
        name = f"{source.source_id}.{kind}"
        media_type = "text/html" if kind == "html" else "application/pdf"
        artifact_id = uuid.uuid4().hex
        try:
            await self.client.upload_artifact(
                self.worker_id,
                job_id,
                attempt_id,
                lease_token,
                artifact_id,
                name,
                media_type,
                body,
                sha256_hex(body),
                RESULT_PACK_SCHEMA_VERSION,
            )
        except ManagerAPIError as exc:
            self._hook_errors.append(f"{source.source_id}: {exc.code}")
            return
        self._doc_refs.append(
            ArtifactRef(
                artifact_id=artifact_id,
                name=name,
                media_type=media_type,
                bytes=len(body),
                sha256=sha256_hex(body),
                schema_version=RESULT_PACK_SCHEMA_VERSION,
            )
        )


class WorkerRunner:
    """SIGINT/SIGTERM を worker stop に変換する CLI 用ラッパー．"""

    def __init__(self, service: WorkerService):
        self.service = service

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            handler = getattr(signal, signal_name, None)
            if handler is None:
                continue
            try:
                loop.add_signal_handler(handler, self.service.request_stop)
            except (NotImplementedError, AttributeError):
                pass
        return await self.service.run()


# ----------------------------------------------------------------------- helpers


def _job_usage(raw: dict) -> JobUsage:
    return JobUsage(
        search_requests=int(raw.get("search_requests", 0)),
        search_credits=int(raw.get("search_credits", 0)),
        fetch_requests=int(raw.get("fetch_requests", 0)),
        document_bytes=int(raw.get("document_bytes", 0)),
        model_requests=int(raw.get("model_requests", 0)),
    )


def _stage_status(mode: str, stage):
    if mode == "search":
        if stage.budget_exhausted:
            return "partial", "budget_exhausted"
        if stage.queries and all(entry["status"] != "ok" for entry in stage.queries):
            return "failed", "no_results"
        return "succeeded", None
    if stage.budget_exhausted:
        return "partial", "budget_exhausted"
    if not stage.sources:
        return "partial", "no_sources"
    if all(source.status not in {"ok", "partial"} for source in stage.sources):
        return "failed", "all_fetches_failed"
    return "succeeded", None


def _stage_failures(stage) -> list[Failure]:
    failures: list[Failure] = []
    for entry in list(stage.failures)[:MAX_FAILURES_REPORTED]:
        detail = str(entry.get("detail") or entry.get("url") or "")[:1000] or None
        failures.append(
            Failure(
                stage=entry.get("stage", "worker"),
                query_id=entry.get("query_id"),
                source_id=entry.get("source_id"),
                code=str(entry.get("code", "unknown"))[:128],
                detail=detail,
            )
        )
    return failures


def _diagnostic(raw: dict | None) -> SearchDiagnostic:
    allowed = {"engine", "unresponsive", "reasons", "result_count", "code"}
    return SearchDiagnostic.model_validate({k: v for k, v in (raw or {}).items() if k in allowed})
