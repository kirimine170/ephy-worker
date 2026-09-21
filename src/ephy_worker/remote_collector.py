"""RemoteCollector：research 側の collector として search / extract job を remote worker へ出す．

契約 0.4 で RemoteCollector は 2 種類の job のみ submit する：

- search job：plan queriesの検索．workerはfetch／extractをしない．
- extract job：research側のmodelが選択した候補のfetch．

選択（Selection）と review は research 側で実行するため，
`supplied_urls`／`exclude_urls`は廃止した．
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Sequence

from .collector import ExtractStageResult, SearchStageResult
from .contracts import CONTRACT_VERSION, JOB_KIND, JobQuery, ResultEnvelope
from .schema import Candidate, Limits, Query, Source


class RemoteCollectorError(Exception):
    """remote collector の bounded な失敗（credential を含めない）．"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class RemoteCollector:
    """Manager queue 経由で search / extract job を実行する collector．"""

    mode = "remote"

    def __init__(
        self,
        client,
        *,
        limits: Limits,
        target_worker_id: str,
        poll_interval: float = 0.5,
        max_wait_seconds: float | None = None,
        deadline_seconds: float = 60.0,
        endpoint: str = "",
        submit_key_prefix: str = "",
    ):
        self.client = client
        self.limits = limits
        self.target_worker_id = target_worker_id
        self.poll_interval = poll_interval
        self.max_wait_seconds = max_wait_seconds
        self.deadline_seconds = deadline_seconds
        self.endpoint = endpoint
        self.submit_key_prefix = submit_key_prefix or f"ephv{CONTRACT_VERSION[1]}."
        self._closed = False
        self._parent_job_id: str | None = None
        self._parent_deadline_at: float | None = None
        self._used = {
            "search_requests": 0,
            "fetch_requests": 0,
            "model_requests": 0,
        }

    @property
    def metadata(self) -> dict:
        return {
            "mode": self.mode,
            "contract": CONTRACT_VERSION,
            "endpoint": self.endpoint,
        }

    def _submit_key(self, kind: str, round_index: int) -> str:
        token = secrets.token_urlsafe(12)
        return f"{self.submit_key_prefix}{kind}-{round_index}-{token}"[:200]

    async def _run_job(self, kind: str, payload: dict) -> tuple[dict, dict]:
        """job を submit し，terminal になるまで poll して (job view, result-pack) を返す．"""
        started = time.monotonic()
        job = await self.client.submit_job(payload)
        deadline = started + self.deadline_seconds
        if self.max_wait_seconds is not None:
            deadline = min(deadline, started + self.max_wait_seconds)
        while True:
            state = job.get("state")
            if state in {"completed", "partial"}:
                response = await self.client.job_result(job["job_id"])
                result = response.get("result") if isinstance(response, dict) else None
                if not isinstance(result, dict):
                    raise RemoteCollectorError("invalid_result_envelope")
                envelope = ResultEnvelope.model_validate(result)
                result_value = envelope.model_dump(mode="json")
                self._record_usage(result_value.get("usage", {}))
                if self._parent_job_id is None:
                    self._parent_job_id = job["job_id"]
                    self._parent_deadline_at = float(job["deadline_at"])
                return job, result_value
            if state in {"failed", "cancelled", "lost"}:
                raise RemoteCollectorError(f"job_{state}", payload["submit_key"])
            if time.monotonic() >= deadline:
                raise RemoteCollectorError(f"{kind}_timeout", payload["submit_key"])
            await asyncio.sleep(self.poll_interval)
            job = await self.client.get_job(job["job_id"])

    def _payload_base(self, kind: str, round_index: int) -> dict:
        if kind == "search" and self._used["search_requests"] >= min(
            self.limits.max_queries, self.limits.search_requests
        ):
            raise RemoteCollectorError("parent_search_budget_exhausted")
        if kind == "extract" and self._used["fetch_requests"] >= min(
            self.limits.max_sources, self.limits.fetch_requests
        ):
            raise RemoteCollectorError("parent_fetch_budget_exhausted")
        deadline_seconds = self.deadline_seconds
        if self._parent_deadline_at is not None:
            deadline_seconds = min(
                deadline_seconds, max(0.001, self._parent_deadline_at - time.time() - 0.1)
            )
        return {
            "contract": CONTRACT_VERSION,
            "kind": JOB_KIND,
            "submit_key": self._submit_key(kind, round_index),
            "parent_job_id": self._parent_job_id,
            "target_worker_id": self.target_worker_id,
            "budget": self._remaining_limits().model_dump(),
            "deadline_seconds": deadline_seconds,
        }

    def _remaining_limits(self) -> Limits:
        values = self.limits.model_dump()
        values["max_queries"] = max(1, self.limits.max_queries - self._used["search_requests"])
        values["search_requests"] = max(
            1, self.limits.search_requests - self._used["search_requests"]
        )
        values["max_sources"] = max(1, self.limits.max_sources - self._used["fetch_requests"])
        values["fetch_requests"] = max(
            1, self.limits.fetch_requests - self._used["fetch_requests"]
        )
        values["model_requests"] = max(
            1, self.limits.model_requests - self._used["model_requests"]
        )
        return Limits.model_validate(values)

    def _record_usage(self, usage: dict) -> None:
        for key in self._used:
            self._used[key] += max(0, int(usage.get(key, 0) or 0))

    def _search_payload(self, queries: Sequence[tuple[str, Query]], round_index: int, topic: str) -> dict:
        payload = self._payload_base("search", round_index)
        payload["input"] = {
            "mode": "search",
            "round_index": round_index,
            "topic": topic,
            "queries": [
                JobQuery.from_query(query, query_id=query_id).model_dump()
                for query_id, query in queries
            ],
        }
        return payload

    @staticmethod
    def _candidate_input(candidate: Candidate) -> dict:
        """契約の CandidateInput 字段だけ抽出する（extra は禁止）．"""
        return {
            "url": candidate.url,
            "title": candidate.title,
            "snippet": candidate.snippet,
            "query_id": candidate.query_id,
            "engine": candidate.engine,
        }

    def _extract_payload(
        self,
        items: Sequence[tuple[str, Candidate]],
        round_index: int,
        topic: str,
    ) -> dict:
        payload = self._payload_base("extract", round_index)
        # source_id は worker 側（s1..）が採番し，research 側が位置付きで S 連番へ戻すため，
        # 候補の順序だけが契約になる．
        payload["input"] = {
            "mode": "extract",
            "round_index": round_index,
            "topic": topic,
            "candidates": [self._candidate_input(candidate) for _, candidate in items],
        }
        return payload

    @staticmethod
    def _stage_result(kind: str, job: dict, result: dict):
        """result-pack（JobResult の dict）を stage 結果へ変換する．"""
        exhausted = result.get("stop_reason") if result.get("status") == "partial" else None
        payload = result.get("payload", {})
        common = {
            "failures": list(payload.get("failures", [])),
            "budget_exhausted": exhausted,
            "usage": dict(result.get("usage", {})),
            "elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
            "worker_id": job.get("worker_id"),
        }
        if kind == "search":
            candidates = [Candidate.model_validate(candidate) for candidate in payload.get("candidates", [])]
            return SearchStageResult(queries=list(payload.get("queries", [])), candidates=candidates, **common)
        sources = [Source.model_validate(source) for source in payload.get("sources", [])]
        return ExtractStageResult(sources=sources, **common)

    async def search(
        self,
        items: Sequence[tuple[str, Query]],
        *,
        round_index: int = 0,
        topic: str = "",
    ) -> SearchStageResult:
        """search job を submit し，候補を含む結果を返す（fetch はしない）．"""
        if self._closed:
            raise RemoteCollectorError("collector_closed")
        if not items:
            return SearchStageResult(usage={"search_requests": 0, "search_credits": 0, "model_requests": 0})
        payload = self._search_payload(items, round_index, topic)
        job, result = await self._run_job("search", payload)
        return self._stage_result("search", job, result)

    async def extract(
        self,
        items: Sequence[tuple[str, Candidate]],
        *,
        round_index: int = 0,
        topic: str = "",
    ) -> ExtractStageResult:
        """research 側が選択済み候補の fetch（extract job）を submit し，source を返す．"""
        if self._closed:
            raise RemoteCollectorError("collector_closed")
        if not items:
            return ExtractStageResult(usage={"fetch_requests": 0, "document_bytes": 0, "model_requests": 0})
        payload = self._extract_payload(items, round_index, topic)
        job, result = await self._run_job("extract", payload)
        return self._stage_result("extract", job, result)

    async def aclose(self) -> None:
        self._closed = True
        await self.client.aclose()
