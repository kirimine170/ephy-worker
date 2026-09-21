"""WorkerService の test：manager からの claim・実行・artifact upload・finalize の一連の流れ．

検索と取得は fixture provider（FixtureSearch / FixtureFetcher）を使い，network なしで
次の事項を検証する．

1. search / extract job の end-to-end 実行（completed, result-pack の hash・contract 整合）
2. 暇な worker の idle 挙動（online の維持，job の作成なし）
3. 長時間 job の lease 更新（更新なしだと lease が切れて finalize 不能になる）
4. requester 側の cancel が worker へ伝わり cancelled として finalize される
5. 別 worker 宛の job を無視する（target_worker_id の routing）
6. query 失敗・全 fetch 失敗・全 query 失敗の worker 側 status 分類
7. LocalCollector と RemoteCollector の構造的同一性（Phase 2 の受け入れ基準）
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from ephy_worker.budget import Budget
from ephy_worker.collector import run_extract_stage, run_search_stage
from ephy_worker.config import SearchConfig
from ephy_worker.contracts import CONTRACT_VERSION, JOB_KIND, ResultEnvelope, SubmitJob, result_pack_hash
from ephy_worker.fixture_backend import FixtureBackend, InProcessManagerClient
from ephy_worker.remote_collector import RemoteCollector, RemoteCollectorError
from ephy_worker.schema import Candidate, Limits, Passage, Query, Source
from ephy_worker.search import SearchError
from ephy_worker.worker_service import WorkerService

TERMINAL_STATES = {"completed", "partial", "failed", "cancelled", "lost"}

SPEC_URLS = [
    "https://example.org/spec-1",
    "https://example.org/spec-2",
    "https://example.org/spec-3",
]
DATA_URLS = [
    "https://example.org/data-1",
    "https://example.org/data-2",
    "https://example.org/data-3",
]
FIXTURE_RESULTS = {"version 2 spec": SPEC_URLS, "version 2 primary data": DATA_URLS}
DEFAULT_URLS = ["https://example.org/gen-1", "https://example.org/gen-2"]
DEFAULT_QUERIES = ("version 2 spec", "version 2 primary data")
TOPIC = "Version 2 の制約条件を確認する"


class FixtureSearch:
    """決定的な検索：同じ query には同じ候補リストを同じ順で返す．"""

    def __init__(
        self,
        *,
        results: dict[str, list[str]] = FIXTURE_RESULTS,
        default: list[str] = DEFAULT_URLS,
        error_for: dict[str, Exception] | None = None,
    ):
        self.results = dict(results)
        self.default = list(default)
        self.error_for = dict(error_for or {})
        self.requests = 0
        self.last_diagnostic: dict = {}

    async def search(self, text: str, query_id: str) -> list[Candidate]:
        self.requests += 1
        if text in self.error_for:
            raise self.error_for[text]
        urls = self.results.get(text, self.default)
        self.last_diagnostic = {"engine": "fixture", "query": text}
        return [
            Candidate(url=url, title=f"fixture {index}", rank=index, query_id=query_id, engine="fixture")
            for index, url in enumerate(urls)
        ]

    async def aclose(self) -> None:
        return None


class FixtureFetcher:
    """決定的な取得：URL ごとに html source＋2 passages を返す（fail_all のときは失敗）．"""

    def __init__(self, *, delay_seconds: float = 0.0, fail_all: bool = False):
        self.delay_seconds = delay_seconds
        self.fail_all = fail_all
        self.calls: list[str] = []
        self.cancelled = False

    async def fetch(self, candidate: Candidate, source_id: str) -> Source:
        self.calls.append(candidate.url)
        if self.delay_seconds:
            try:
                await asyncio.sleep(self.delay_seconds)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.fail_all:
            return Source(
                source_id=source_id,
                requested_url=candidate.url,
                kind="unknown",
                status="network_error",
                error="fixture fetch failure",
            )
        body = f"{candidate.url} の本文です．Version 2 の仕様と制約条件を説明します．" * 20
        return Source(
            source_id=source_id,
            requested_url=candidate.url,
            final_url=candidate.url,
            title=candidate.title,
            kind="html",
            media_type="text/html; charset=utf-8",
            http_status=200,
            status="ok",
            bytes_received=len(body.encode("utf-8")),
            passages=[
                Passage(passage_id=f"{source_id}-p1", source_id=source_id, text=body[:80], start=0, end=80),
                Passage(passage_id=f"{source_id}-p2", source_id=source_id, text=body[80:160], start=80, end=160),
            ],
        )

    async def aclose(self) -> None:
        return None


async def wait_for(predicate, *, timeout: float = 15.0, interval: float = 0.02) -> bool:
    """同じ event loop 内で条件成立を待つ．成立したら True，タイムアウトで False．"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def make_backend(tmp_path, search, fetcher, *, lease_ttl_seconds: float = 30.0) -> FixtureBackend:
    return FixtureBackend(
        tmp_path,
        search_factory=(lambda: search) if search is not None else None,
        fetcher_factory=(lambda: fetcher) if fetcher is not None else None,
        lease_ttl_seconds=lease_ttl_seconds,
    )


def make_search_submit(
    key: str,
    *,
    worker_id: str = "worker-a",
    query_texts: tuple[str, ...] = DEFAULT_QUERIES,
) -> SubmitJob:
    return SubmitJob(
        contract=CONTRACT_VERSION,
        submit_key=key,
        kind=JOB_KIND,
        input={
            "mode": "search",
            "topic": TOPIC,
            "round_index": 0,
            "queries": [
                {
                    "query_id": f"Q{index}",
                    "text": text,
                    "role": "overview",
                    "reason": "fixture 検証用クエリ",
                }
                for index, text in enumerate(query_texts, 1)
            ],
        },
        target_worker_id=worker_id,
        budget=Limits(),
        deadline_seconds=600,
    )


def make_extract_submit(
    key: str,
    *,
    worker_id: str = "worker-a",
    urls: list[str] | tuple[str, ...] = DEFAULT_URLS,
) -> SubmitJob:
    return SubmitJob(
        contract=CONTRACT_VERSION,
        submit_key=key,
        kind=JOB_KIND,
        input={
            "mode": "extract",
            "topic": TOPIC,
            "round_index": 0,
            "candidates": [
                {"url": url, "title": f"fixture {index}", "query_id": "q1", "engine": "fixture"}
                for index, url in enumerate(urls)
            ],
        },
        target_worker_id=worker_id,
        budget=Limits(),
        deadline_seconds=600,
    )


async def wait_terminal(backend, job_id: str) -> bool:
    return await wait_for(lambda: backend.manager.job_view(job_id)["state"] in TERMINAL_STATES)


def flat_result(manager, job_id: str) -> dict:
    envelope = manager.job_result(job_id)
    flattened = {**envelope, **envelope["payload"]}
    flattened.setdefault("queries", [])
    flattened.setdefault("candidates", [])
    flattened.setdefault("sources", [])
    return flattened


# -- 1. end-to-end ----------------------------------------------------------


async def test_worker_executes_search_and_extract_jobs_end_to_end(tmp_path):
    """search job（候補のみ）→ extract job（research 側の選択を模した候補の fetch）の一連の流れ．"""
    search, fetcher = FixtureSearch(), FixtureFetcher()
    backend = make_backend(tmp_path, search, fetcher)
    try:
        await backend.start_worker("worker-a")

        # 1) search job：候補のみを返し，fetch はしない．
        view, _ = backend.manager.submit(make_search_submit("key-e2e-s001"))
        search_job_id = view["job_id"]
        assert await wait_terminal(backend, search_job_id)

        view = backend.manager.job_view(search_job_id)
        assert view["state"] == "completed"
        assert view["worker_id"] == "worker-a"

        result = flat_result(backend.manager, search_job_id)
        assert result["status"] == "succeeded"
        assert result["stop_reason"] is None
        assert [entry["status"] for entry in result["queries"]] == ["ok", "ok"]
        assert [entry["result_count"] for entry in result["queries"]] == [3, 3]
        assert len(result["candidates"]) == 6
        assert result["sources"] == []
        assert result["failures"] == []

        # 2) extract job：research 側が候補を選んだものとして fetch を実行する．
        selected = result["candidates"][: Limits().initial_sources]
        view, _ = backend.manager.submit(
            make_extract_submit("key-e2e-x001", urls=[candidate["url"] for candidate in selected])
        )
        extract_job_id = view["job_id"]
        assert await wait_terminal(backend, extract_job_id)

        view = backend.manager.job_view(extract_job_id)
        assert view["state"] == "completed"
        assert view["worker_id"] == "worker-a"

        result = flat_result(backend.manager, extract_job_id)
        assert result["status"] == "succeeded"
        assert len(result["sources"]) == Limits().initial_sources
        assert [source["source_id"] for source in result["sources"]] == [
            f"s{index}" for index in range(1, Limits().initial_sources + 1)
        ]
        assert all(source["status"] == "ok" for source in result["sources"])
        assert all(len(source["passages"]) == 2 for source in result["sources"])
        assert result["usage"]["model_requests"] == 0
        assert result["usage"]["document_bytes"] > 0
        assert result["failures"] == []

        # result-pack artifact の hash・contract 整合
        files = view["result"]["manifest"]
        pack_ref = next(ref for ref in files if ref["name"] == "result-pack.json")
        stored_ref, path = backend.manager.get_artifact(extract_job_id, pack_ref["artifact_id"])
        body = path.read_bytes()
        assert stored_ref.media_type == "application/json"
        assert stored_ref.bytes == len(body)
        assert hashlib.sha256(body).hexdigest() == pack_ref["sha256"]
        pack = ResultEnvelope.model_validate(json.loads(body.decode("utf-8")))
        assert pack.contract == CONTRACT_VERSION
        assert pack.status == "succeeded"
        assert result_pack_hash(pack) == pack_ref["sha256"]
    finally:
        await backend.stop()


# -- 2. idle ----------------------------------------------------------------


async def test_worker_without_job_stays_idle(tmp_path):
    backend = make_backend(tmp_path, FixtureSearch(), FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        await asyncio.sleep(0.25)
        job_count = backend.manager._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert job_count == 0
        workers = backend.manager.list_workers()
        assert workers[0]["worker_id"] == "worker-a"
        assert workers[0]["state"] == "online"
    finally:
        await backend.stop()


# -- 3. lease renewal --------------------------------------------------------


async def test_worker_renews_lease_for_long_jobs(tmp_path):
    """ttl 2.0s の lease を 2.5s かけて fetch 中に更新し，completed に到達する．

    更新が起きなければ finalize の時点で lease が切れて 410 になり，
    job は terminal state に到達しない（wait_for が False を返す）．
    """
    # fetch は 5 source 直列：0.5s×5 = 2.5s で初期 lease（2.0s）を超える．
    fetcher = FixtureFetcher(delay_seconds=0.5)
    backend = make_backend(tmp_path, FixtureSearch(), fetcher, lease_ttl_seconds=2.0)
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_extract_submit("key-lease-0001", urls=SPEC_URLS + DATA_URLS[:2]))
        job_id = view["job_id"]
        assert await wait_terminal(backend, job_id)

        assert backend.manager.job_view(job_id)["state"] == "completed"
        result = flat_result(backend.manager, job_id)
        assert result["status"] == "succeeded"
        assert result["elapsed_seconds"] >= 2.0

        row = backend.manager._conn.execute(
            "SELECT created_at, lease_expires_at FROM attempts WHERE job_id=? ORDER BY attempt_number",
            (job_id,),
        ).fetchone()
        # 初期 lease は claimed_at + 2.0s．renew が起きなければここまで伸びない．
        assert row["lease_expires_at"] - row["created_at"] > 2.0
    finally:
        await backend.stop()


# -- 4. cancellation ----------------------------------------------------------


async def test_cancelled_job_finalizes_as_cancelled(tmp_path):
    # 直列 fetch 0.4s×5 ≒ 2.0s：cancel 検出（t≒1.0 の renew）より後に，
    # その renew が延長した lease（t≒3.0 まで）が切れる前に finalize される．
    fetcher = FixtureFetcher(delay_seconds=0.4)
    backend = make_backend(tmp_path, FixtureSearch(), fetcher, lease_ttl_seconds=2.0)
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_extract_submit("key-cancel-0001", urls=SPEC_URLS + DATA_URLS[:2]))
        job_id = view["job_id"]
        assert await wait_for(lambda: backend.manager.job_view(job_id)["state"] == "running")

        backend.manager.cancel(job_id)
        assert await wait_terminal(backend, job_id)

        view = backend.manager.job_view(job_id)
        assert view["state"] == "cancelled"
        assert view["stop_reason"] == "cancelled_by_requester"
        result = flat_result(backend.manager, job_id)
        assert result["status"] == "cancelled"
        assert result["stop_reason"] == "cancelled_by_requester"
        assert fetcher.cancelled is True
    finally:
        await backend.stop()


# -- 5. target routing ---------------------------------------------------------


async def test_worker_ignores_jobs_for_other_worker(tmp_path):
    backend = make_backend(tmp_path, FixtureSearch(), FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_search_submit("key-target-0001", worker_id="worker-b"))
        job_id = view["job_id"]

        # worker-a は自分宛でない job を claim しない．
        await asyncio.sleep(0.4)
        view = backend.manager.job_view(job_id)
        assert view["state"] == "queued"
        assert view["worker_id"] is None

        # worker-b を手動で起動すると，その worker-b が完了させる．
        client_b = InProcessManagerClient(backend.manager, "worker-b-secret")
        service_b = WorkerService(
            client_b,
            "worker-b",
            search_config=SearchConfig(),
            search=FixtureSearch(),
            fetcher=FixtureFetcher(),
            poll_interval=0.02,
        )
        task_b = asyncio.create_task(service_b.run())
        try:
            assert await wait_terminal(backend, job_id)
            view = backend.manager.job_view(job_id)
            assert view["state"] == "completed"
            assert view["worker_id"] == "worker-b"
        finally:
            service_b.request_stop()
            await task_b
    finally:
        await backend.stop()


# -- 6. worker-side status classification ---------------------------------------


async def test_worker_continues_after_query_failure(tmp_path):
    search = FixtureSearch(error_for={"version 2 spec": SearchError("searxng_engine_unresponsive")})
    backend = make_backend(tmp_path, search, FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_search_submit("key-qfail-0001"))
        job_id = view["job_id"]
        assert await wait_terminal(backend, job_id)

        # 片方の query が失敗しても，もう片方の候補だけで search job は completed になる．
        assert backend.manager.job_view(job_id)["state"] == "completed"
        result = flat_result(backend.manager, job_id)
        assert result["status"] == "succeeded"
        assert [entry["status"] for entry in result["queries"]] == ["failed", "ok"]
        assert len(result["candidates"]) == len(DATA_URLS)
        assert result["sources"] == []
        failure = next(item for item in result["failures"] if item.get("stage") == "search")
        assert failure["query_id"] == "Q1"
        assert failure["code"] == "searxng_engine_unresponsive"
    finally:
        await backend.stop()


async def test_worker_reports_all_fetches_failed(tmp_path):
    backend = make_backend(tmp_path, FixtureSearch(), FixtureFetcher(fail_all=True))
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_extract_submit("key-ffail-0001", urls=SPEC_URLS))
        job_id = view["job_id"]
        assert await wait_terminal(backend, job_id)

        assert backend.manager.job_view(job_id)["state"] == "failed"
        result = flat_result(backend.manager, job_id)
        assert result["status"] == "failed"
        assert result["stop_reason"] == "all_fetches_failed"
        assert result["sources"], "fetch は試行されているはず"
        assert all(source["status"] == "network_error" for source in result["sources"])
    finally:
        await backend.stop()


async def test_worker_reports_failed_when_all_queries_fail(tmp_path):
    search = FixtureSearch(
        error_for={"version 2 spec": SearchError("boom"), "version 2 primary data": SearchError("boom")}
    )
    backend = make_backend(tmp_path, search, FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_search_submit("key-nores-0001"))
        job_id = view["job_id"]
        assert await wait_terminal(backend, job_id)

        assert backend.manager.job_view(job_id)["state"] == "failed"
        result = flat_result(backend.manager, job_id)
        assert result["status"] == "failed"
        assert result["stop_reason"] == "no_results"
        assert result["candidates"] == []
        assert result["sources"] == []
    finally:
        await backend.stop()


async def test_search_job_with_zero_results_succeeds_empty(tmp_path):
    """0.4 の search job は0候補でも正当な結果（research側が選択できないだけ）．"""
    search = FixtureSearch(results={}, default=[])
    backend = make_backend(tmp_path, search, FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        view, _ = backend.manager.submit(make_search_submit("key-empty-0001"))
        job_id = view["job_id"]
        assert await wait_terminal(backend, job_id)

        assert backend.manager.job_view(job_id)["state"] == "completed"
        result = flat_result(backend.manager, job_id)
        assert result["status"] == "succeeded"
        assert result["candidates"] == []
        assert result["sources"] == []
    finally:
        await backend.stop()


# -- 7. local / remote parity ----------------------------------------------------


def _shape_queries(queries: list[dict]) -> list[tuple]:
    return [(entry["query_id"], entry["text"], entry["status"], entry["result_count"]) for entry in queries]


def _shape_candidates(candidates) -> list[tuple]:
    """Candidate（local）／dict（remote result-pack）を同一形状へ正規化して比較する．"""
    normalized = [c if isinstance(c, Candidate) else Candidate.model_validate(c) for c in candidates]
    return [(candidate.url, candidate.query_id, candidate.rank) for candidate in normalized]


def _shape_sources(sources) -> list[tuple]:
    normalized = [s if isinstance(s, Source) else Source.model_validate(s) for s in sources]
    return [
        (
            source.source_id,
            source.requested_url,
            source.final_url,
            source.status,
            source.kind,
            source.http_status,
            source.media_type,
            source.bytes_received,
            tuple((p.passage_id, p.source_id, p.text, p.start, p.end) for p in source.passages),
        )
        for source in normalized
    ]


def make_remote(backend, *, poll_interval: float = 0.05) -> RemoteCollector:
    return RemoteCollector(
        backend.client,
        limits=Limits(),
        target_worker_id="worker-a",
        poll_interval=poll_interval,
        max_wait_seconds=60,
        deadline_seconds=600,
    )


async def test_remote_search_matches_local_stage(tmp_path):
    """同一 fixture で local な run_search_stage と remote な search job が構造的に一致する．"""
    items = [
        ("q1", Query(text="version 2 spec", role="overview", reason="全体の確認")),
        ("q2", Query(text="version 2 primary data", role="primary", reason="一次資料の確認")),
    ]

    local = await run_search_stage(FixtureSearch(), Budget(Limits()), items)

    backend = make_backend(tmp_path, FixtureSearch(), FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        remote = await make_remote(backend).search(items, round_index=0, topic=TOPIC)
    finally:
        await backend.stop()

    assert remote.worker_id == "worker-a"
    assert _shape_queries(local.queries) == _shape_queries(remote.queries)
    assert _shape_candidates(local.candidates) == _shape_candidates(remote.candidates)
    assert local.failures == remote.failures
    assert local.usage["search_requests"] == remote.usage["search_requests"]
    assert local.usage["search_credits"] == remote.usage["search_credits"]


async def test_remote_extract_matches_local_stage(tmp_path):
    """同一 fixture で local な run_extract_stage と remote な extract job が構造的に一致する．"""
    urls = SPEC_URLS + DATA_URLS
    candidates = [
        Candidate(url=url, title=f"fixture {index}", query_id="q1", engine="fixture", rank=index)
        for index, url in enumerate(urls)
    ]
    items = [(f"s{index}", candidate) for index, candidate in enumerate(candidates, start=1)]

    local = await run_extract_stage(FixtureFetcher(), Budget(Limits()), items)

    backend = make_backend(tmp_path, FixtureSearch(), FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        remote = await make_remote(backend).extract(items, round_index=0, topic=TOPIC)
    finally:
        await backend.stop()

    assert remote.worker_id == "worker-a"
    assert _shape_sources(local.sources) == _shape_sources(remote.sources)
    assert local.failures == remote.failures
    assert local.usage["fetch_requests"] == remote.usage["fetch_requests"]
    assert local.usage["document_bytes"] == remote.usage["document_bytes"]
    # 決定的 fetch：5 件すべて成功し，source_id は worker 側 s1.. がそのまま返る．
    assert len(local.sources) == len(urls)
    assert all(source.status == "ok" for source in local.sources)


async def test_remote_collector_raises_on_failed_job(tmp_path):
    search = FixtureSearch(
        error_for={"version 2 spec": SearchError("boom"), "version 2 primary data": SearchError("boom")}
    )
    backend = make_backend(tmp_path, search, FixtureFetcher())
    try:
        await backend.start_worker("worker-a")
        collector = make_remote(backend)
        with pytest.raises(RemoteCollectorError) as excinfo:
            await collector.search(
                [("q1", Query(text="version 2 spec", role="overview", reason="全体の確認"))],
                round_index=0,
                topic=TOPIC,
            )
        assert excinfo.value.code == "job_failed"
    finally:
        await backend.stop()


async def test_remote_collector_times_out(tmp_path):
    """max_wait_seconds 以内に terminal にならない job は collector 側で timeout になる．"""
    fetcher = FixtureFetcher(delay_seconds=0.3)
    backend = make_backend(tmp_path, FixtureSearch(), fetcher)
    try:
        await backend.start_worker("worker-a")
        collector = RemoteCollector(
            backend.client,
            limits=Limits(),
            target_worker_id="worker-a",
            poll_interval=0.05,
            max_wait_seconds=0.2,
            deadline_seconds=600,
        )
        items = [
            ("s1", Candidate(url="https://example.org/slow-1", title="t1", query_id="q1", engine="fixture")),
            ("s2", Candidate(url="https://example.org/slow-2", title="t2", query_id="q1", engine="fixture")),
        ]
        with pytest.raises(RemoteCollectorError) as excinfo:
            await collector.extract(items, round_index=0, topic=TOPIC)
        assert excinfo.value.code == "extract_timeout"
    finally:
        await backend.stop()
