"""Local 参照 collector と共有 search/extract stage 実装．

`run_search_stage` / `run_extract_stage` が収集ロジックの唯一の本体であり，
LocalCollector（local research）と WorkerService（remote worker）の両方が
同じ関数を呼ぶことで挙動が一致する．

ソース選択（どの候補を取得するか）はこの層に含まない．選択は research 側の
model（Selection）または呼び出し側が行い，extract stage は渡された候補
（source_id 付き）を fetch するだけ．
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .budget import Budget, BudgetExceeded
from .fetch import PublicFetcher
from .research import normalize_url
from .schema import Candidate, Query, Source
from .search import QueryPrivacyError, validate_query


@dataclass
class SearchStageResult:
    """search job / LocalCollector.search の結果（research report 取り込み単位）．"""

    queries: list[dict] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    budget_exhausted: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    worker_id: str | None = None


@dataclass
class ExtractStageResult:
    """extract job / LocalCollector.extract の結果（research report 取り込み単位）．"""

    sources: list[Source] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    budget_exhausted: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    worker_id: str | None = None


def _query_entry(query_id: str, query: Query, *, status: str, result_count: int, diagnostic: dict) -> dict:
    return {
        "query_id": query_id,
        "text": query.text,
        "role": query.role,
        "reason": query.reason,
        "status": status,
        "result_count": result_count,
        "diagnostic": diagnostic,
    }


def _search_usage(budget: Budget) -> dict[str, int]:
    return {
        "search_requests": budget.counts.get("search_requests", 0),
        "search_credits": budget.counts.get("search_requests", 0),
        "model_requests": 0,
    }


def _extract_usage(budget: Budget, sources: Sequence[Source]) -> dict[str, int]:
    return {
        "fetch_requests": budget.counts.get("fetch_requests", 0),
        "document_bytes": sum(source.bytes_received or 0 for source in sources),
        "model_requests": 0,
    }


async def run_search_stage(
    search,
    budget: Budget,
    items: Sequence[tuple[str, Query]],
    *,
    cancel_check: callable | None = None,
    result: SearchStageResult | None = None,
) -> SearchStageResult:
    """plan queries を budget 内で検索し，候補のみ返す（fetch はしない）．"""
    started = time.monotonic()
    result = result or SearchStageResult()
    try:
        for query_id, query in items:
            if cancel_check is not None and cancel_check():
                break
            try:
                validate_query(query.text)
            except QueryPrivacyError as exc:
                entry = _query_entry(query_id, query, status="failed", result_count=0, diagnostic={"code": "query_rejected_privacy"})
                result.queries.append(entry)
                result.failures.append({"stage": "search", "query_id": query_id, "code": "query_rejected_privacy", "detail": str(exc)[:200]})
                continue
            if not budget.query(query.text):
                entry = _query_entry(query_id, query, status="limit", result_count=0, diagnostic={"code": "max_queries"})
                result.queries.append(entry)
                result.failures.append({"stage": "search", "query_id": query_id, "code": "max_queries"})
                result.budget_exhausted = "max_queries"
                break
            try:
                found = await search.search(query.text, query_id)
            except BudgetExceeded:
                entry = _query_entry(query_id, query, status="limit", result_count=0, diagnostic={"code": "budget_exhausted"})
                result.queries.append(entry)
                result.failures.append({"stage": "search", "query_id": query_id, "code": "budget_exhausted"})
                result.budget_exhausted = "max_queries"
                break
            except Exception as exc:  # noqa: BLE001 - 個別 query 失敗で stage を止めない
                code = getattr(exc, "code", type(exc).__name__)
                entry = _query_entry(query_id, query, status="failed", result_count=0, diagnostic={"code": code})
                result.queries.append(entry)
                result.failures.append({"stage": "search", "query_id": query_id, "code": code})
                continue
            entry = _query_entry(query_id, query, status="ok", result_count=len(found), diagnostic=dict(getattr(search, "last_diagnostic", {})))
            result.queries.append(entry)
            result.candidates.extend(found)
    finally:
        result.usage = _search_usage(budget)
        result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


async def run_extract_stage(
    fetcher: PublicFetcher,
    budget: Budget,
    items: Sequence[tuple[str, Candidate]],
    *,
    cancel_check: callable | None = None,
    result: ExtractStageResult | None = None,
) -> ExtractStageResult:
    """渡された候補（research 側が選択済み）を budget 内で fetch する．"""
    started = time.monotonic()
    result = result or ExtractStageResult()
    try:
        for source_id, candidate in items:
            if cancel_check is not None and cancel_check():
                break
            key = normalize_url(candidate.url)
            try:
                accepted = budget.source(key)
            except BudgetExceeded:
                result.failures.append({"stage": "fetch", "source_id": source_id, "url": candidate.url, "code": "max_sources"})
                result.budget_exhausted = "max_sources"
                break
            if not accepted:
                # job 内の重複は research 側で除去済み．念のためスキップ．
                continue
            try:
                source = await fetcher.fetch(candidate, source_id=source_id)
            except BudgetExceeded as exc:
                reason = str(exc) or "budget_exhausted"
                result.sources.append(Source(source_id=source_id, requested_url=candidate.url, status="limit", error=reason))
                result.failures.append({"stage": "fetch", "source_id": source_id, "url": candidate.url, "code": reason})
                result.budget_exhausted = reason
                break
            except Exception as exc:  # noqa: BLE001 - 個別 fetch 失敗で stage を止めない
                code = getattr(exc, "code", type(exc).__name__)
                result.sources.append(
                    Source(source_id=source_id, requested_url=candidate.url, status="network_error", error=str(exc)[:300] or code)
                )
                result.failures.append({"stage": "fetch", "source_id": source_id, "url": candidate.url, "code": code})
                continue
            result.sources.append(source)
            if source.status not in {"ok", "partial"}:
                result.failures.append(
                    {"stage": "fetch", "source_id": source.source_id, "url": candidate.url, "code": source.status}
                )
    finally:
        result.usage = _extract_usage(budget, result.sources)
        result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


@runtime_checkable
class Collector(Protocol):
    """research executor が呼ぶ収集接口（契約 0.4：search / extract）．"""

    mode: str

    @property
    def metadata(self) -> dict:
        """report.metrics に記録する収集経路の識別情報（credential は含めない）．"""
        ...

    async def search(
        self,
        items: Sequence[tuple[str, Query]],
        *,
        round_index: int = 0,
        topic: str = "",
    ) -> SearchStageResult:
        ...

    async def extract(
        self,
        items: Sequence[tuple[str, Candidate]],
        *,
        round_index: int = 0,
        topic: str = "",
    ) -> ExtractStageResult:
        ...

    async def aclose(self) -> None:
        ...


class LocalCollector:
    """同じ host 内で検索・取得・抽出を直接実行する参照実装（検証用に使用）．"""

    mode = "local"

    def __init__(self, search, fetcher: PublicFetcher, limits, *, metadata: dict | None = None):
        self.search = search
        self.fetcher = fetcher
        self.budget = limits if isinstance(limits, Budget) else Budget(limits)
        self.limits = self.budget.limits
        self._metadata = metadata or {"provider": getattr(search, "metadata", {}).get("provider", "searxng")}

    @property
    def metadata(self) -> dict:
        return {**self._metadata, "mode": self.mode}

    async def search(self, items, *, round_index: int = 0, topic: str = "") -> SearchStageResult:
        return await run_search_stage(self.search, self.budget, items)

    async def extract(self, items, *, round_index: int = 0, topic: str = "") -> ExtractStageResult:
        return await run_extract_stage(self.fetcher, self.budget, items)

    async def aclose(self) -> None:
        await self.fetcher.aclose()
        aclose = getattr(self.search, "aclose", None)
        if aclose is not None:
            await aclose()
