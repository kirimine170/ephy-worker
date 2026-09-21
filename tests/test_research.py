"""Offline workflow tests assert provenance and state，not model competence."""

import asyncio
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.research import ResearchExecutor, normalize_url
from ephy_worker.schema import (
    Candidate,
    ClaimCheck,
    ClaimProposal,
    EvidenceCheck,
    EvidenceProposal,
    Extraction,
    Limits,
    Passage,
    Plan,
    Query,
    Review,
    Selection,
    Source,
)
from ephy_worker.store import JobStore, output_root


class FakeModel:
    metadata: ClassVar[dict] = {"model_id": "offline-fixture", "real_model": False}

    def __init__(self, *, additional=False, bad_quote=False, review_timeout=False):
        self.calls = []
        self.additional = additional
        self.bad_quote = bad_quote
        self.review_timeout = review_timeout
        self.closed = False

    async def run(self, output_type, prompt, *, stage):
        self.calls.append(stage)
        data = json.loads(prompt.split("INPUT_DATA_JSON:\n", 1)[1])
        if stage == "plan":
            return Plan(
                subquestions=["Version 2 limits"],
                queries=[
                    Query(text=f"version 2 {role}", role=role, reason="fixture")
                    for role in ["overview", "primary", "limitations"]
                ],
            )
        if stage == "select":
            return Selection(urls=[c["url"] for c in data["candidates"][:5]], reason="fixture")
        if stage in {"extract", "repair_quotes"}:
            p = data["passages"][-1]
            return Extraction(
                claims=[
                    ClaimProposal(
                        text="version 2 は10件まで対応します．",
                        evidence=[
                            EvidenceProposal(
                                source_id=p["source_id"],
                                passage_id=p["passage_id"],
                                quote="invented" if self.bad_quote else p["text"],
                                relation="supports",
                            )
                        ],
                    )
                ]
            )
        if self.review_timeout:
            raise TimeoutError()
        return Review(
            checks=[
                ClaimCheck(
                    claim_id=c["claim_id"],
                    status="supported_primary",
                    reason="version 2の仕様本文．",
                    evidence_checks=[
                        EvidenceCheck(
                            evidence_id=e["evidence_id"],
                            relation="supports",
                            conditions_match=True,
                            is_primary=True,
                            reason="対象版が一致．",
                        )
                        for e in data["evidence"]
                        if e["evidence_id"] in c["evidence_ids"]
                    ],
                )
                for c in data["claims"]
            ],
            additional_queries=[Query(text="version 2 extra conditions", role="gap", reason="制約の確認．")]
            if self.additional
            else [],
        )

    async def aclose(self):
        self.closed = True


class FakeSearch:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure

    async def search(self, query, query_id):
        self.calls.append(query)
        if self.failure:
            raise RuntimeError("provider failed")
        return [
            Candidate(
                url=f"https://example.org/{query_id}/{i}",
                title="Version 2 spec",
                query_id=query_id,
                engine="fixture",
                rank=i,
            )
            for i in range(5)
        ]

    async def aclose(self):
        pass


class FakeFetcher:
    def __init__(self, fail=False):
        self.fail = fail

    async def fetch(self, candidate, source_id):
        return Source(
            source_id=source_id,
            requested_url=candidate.url,
            final_url=candidate.url,
            kind="pdf" if source_id == "S5" else "html",
            status="http_error" if self.fail else "ok",
            http_status=403 if self.fail else 200,
            title="Version 2 spec",
            body_hash="same",
            passages=[]
            if self.fail
            else [
                Passage(
                    source_id=source_id,
                    passage_id=f"{source_id}-P1",
                    text="Version 2 supports 10 records.",
                    page=8 if source_id == "S5" else None,
                    end=30,
                )
            ],
        )

    async def aclose(self):
        pass


def worker(tmp_path, **kwargs):
    config = SimpleNamespace(limits=Limits(), search=SimpleNamespace(engine="fixture"))
    store = JobStore(tmp_path)
    model = kwargs.pop("model", FakeModel())
    if "collector" in kwargs:
        # collector モードでは local search/fetcher は構築しない（None のまま）．
        return ResearchExecutor(config, "fixture", store, model=model, collector=kwargs.pop("collector"))
    return ResearchExecutor(
        config,
        "fixture",
        store,
        model=model,
        search=kwargs.pop("search", FakeSearch()),
        fetcher=kwargs.pop("fetcher", FakeFetcher()),
    )


class FakeCollector:
    """collector の二阶段インターフェースを worker 側採番（s1..）で模した fake．"""

    mode = "remote"

    def __init__(self, *, fail_search=False):
        from ephy_worker.collector import ExtractStageResult, SearchStageResult

        self.stage_results = (SearchStageResult, ExtractStageResult)
        self.search_calls = []
        self.extract_calls = []
        self.fail_search = fail_search
        self.closed = False

    @property
    def metadata(self):
        return {"mode": self.mode, "contract": "0.4", "endpoint": "fixture"}

    async def search(self, items, *, round_index=0, topic=""):
        from ephy_worker.remote_collector import RemoteCollectorError

        if self.fail_search:
            raise RemoteCollectorError("search_timeout")
        self.search_calls.append({"round_index": round_index, "items": list(items)})
        queries = []
        candidates = []
        for query_id, query in items:
            queries.append(
                {
                    "query_id": query_id, "text": query.text, "role": query.role, "reason": query.reason,
                    "status": "ok", "result_count": 5, "diagnostic": {},
                }
            )
            candidates.extend(
                Candidate(
                    url=f"https://example.org/{query_id}/{i}",
                    title="Version 2 spec",
                    query_id=query_id,
                    engine="fixture",
                    rank=i,
                )
                for i in range(5)
            )
        return self.stage_results[0](
            queries=queries,
            candidates=candidates,
            usage={"search_requests": len(items), "search_credits": 0, "model_requests": 0},
        )

    async def extract(self, items, *, round_index=0, topic=""):
        self.extract_calls.append({"round_index": round_index, "items": list(items)})
        sources = []
        for index, (_preassigned_id, candidate) in enumerate(items, start=1):
            # worker は job 内で s1.. を採番し，先渡しの S 連番は無視する．
            source_id = f"s{index}"
            body = "Version 2 は10件まで対応します．"
            sources.append(
                Source(
                    source_id=source_id,
                    requested_url=candidate.url,
                    final_url=candidate.url,
                    title="Version 2 spec",
                    kind="html",
                    status="ok",
                    http_status=200,
                    passages=[
                        Passage(
                            source_id=source_id,
                            passage_id=f"{source_id}-html-0",
                            text=body,
                            start=0,
                            end=len(body),
                        )
                    ],
                )
            )
        return self.stage_results[1](
            sources=sources,
            usage={"fetch_requests": len(items), "document_bytes": 0, "model_requests": 0},
        )

    async def aclose(self):
        self.closed = True


async def test_collector_mode_two_stage_flow(tmp_path):
    collector = FakeCollector()
    w = worker(tmp_path, collector=collector)
    report = await w.run("Version 2 の上限を確認してください")
    assert report.state == "completed"
    # local provider は構築されず，collector だけが収集に使う．
    assert w.search is None and w.fetcher is None and collector.closed is True
    # search job 1 回（plan の 3 query），extract job 1 回（選択した 5 候補）．
    assert [c["round_index"] for c in collector.search_calls] == [0]
    assert [c["round_index"] for c in collector.extract_calls] == [0]
    assert [q[0] for q in collector.search_calls[0]["items"]] == ["Q1", "Q2", "Q3"]
    assert [s[0] for s in collector.extract_calls[0]["items"]] == [f"S{i}" for i in range(1, 6)]
    # report の queries には Q 連番，sources には worker の s 連番が S 連番へ付け直される．
    assert [q["query_id"] for q in report.queries] == ["Q1", "Q2", "Q3"]
    assert [s.source_id for s in report.sources] == [f"S{i}" for i in range(1, 6)]
    for source in report.sources:
        for passage in source.passages:
            assert passage.source_id == source.source_id
    assert report.metrics["collection"]["mode"] == "remote"


async def test_collector_mode_followup_round_passage_ids_unique(tmp_path):
    collector = FakeCollector()
    report = await worker(tmp_path, model=FakeModel(additional=True), collector=collector).run(
        "Version 2 の上限を確認してください"
    )
    assert report.state == "completed"
    assert report.stop_reason == "additional_round_limit"
    assert [c["round_index"] for c in collector.search_calls] == [0, 1]
    assert [c["round_index"] for c in collector.extract_calls] == [0, 1]
    # round 1 でも worker は s1.. を採番するため，付け直し後は report 全体で passage_id が一意．
    passage_ids = [p.passage_id for s in report.sources for p in s.passages]
    assert len(passage_ids) == len(set(passage_ids))
    # フォローアップ round は追加 3 件まで（select_candidates の target）．
    assert [s.source_id for s in report.sources] == [f"S{i}" for i in range(1, 9)]
    assert [s[0] for s in collector.extract_calls[1]["items"]] == ["S6", "S7", "S8"]


async def test_collector_failure_marks_job_failed(tmp_path):
    collector = FakeCollector(fail_search=True)
    report = await worker(tmp_path, collector=collector).run("Version 2 の上限を確認してください")
    assert report.state == "failed"
    assert report.stop_reason == "stage_failed"
    assert {"stage": "search", "code": "search_timeout"} in report.failures
    assert collector.closed is True


async def test_complete_report_provenance_and_fresh_job(tmp_path):
    w = worker(tmp_path)
    report = await w.run("version 2 の制約")
    assert report.state == "completed"
    assert set(w.model.calls) == {"plan", "select", "extract", "review"}
    assert len(w.search.calls) == 3 and len(report.sources) == 5
    assert report.claims[0].checked
    assert report.evidence[0].page == 8
    saved = json.loads((w.store.directory / "report.json").read_text())
    assert saved["state"] == report.state
    assert (w.store.directory / "report.md").exists()
    assert w.model.closed
    assert JobStore(tmp_path).job_id != w.store.job_id
    with pytest.raises(FileExistsError):
        JobStore(tmp_path, job_id=w.store.job_id)


async def test_additional_round_at_most_one(tmp_path):
    w = worker(tmp_path, model=FakeModel(additional=True))
    report = await w.run("version 2 の制約")
    assert len(report.rounds) == 2
    assert w.model.calls.count("review") == 2
    assert len(w.search.calls) == 4
    assert report.stop_reason == "additional_round_limit"


async def test_fabricated_citation_not_success(tmp_path):
    report = await worker(tmp_path, model=FakeModel(bad_quote=True)).run("version 2 の制約")
    assert report.state == "failed"
    assert not report.evidence
    assert all(c.status == "insufficient" for c in report.claims)


async def test_fetch_and_search_failure_not_zero_success(tmp_path):
    w = worker(tmp_path, search=FakeSearch(failure=True))
    report = await w.run("version 2 の制約")
    assert report.state == "failed"
    assert all(q["status"] == "failed" for q in report.queries)
    w = worker(tmp_path, fetcher=FakeFetcher(fail=True))
    report = await w.run("version 2 の制約")
    assert report.state == "failed" and len(report.failures) == 5
    assert not report.evidence


async def test_review_timeout_never_promotes_candidates(tmp_path):
    w = worker(tmp_path, model=FakeModel(review_timeout=True))
    report = await w.run("version 2 の制約")
    assert report.state == "failed"
    assert not any(c.checked for c in report.claims)
    assert report.stop_reason == "job_timeout"


async def test_cancel_saves_non_success_and_closes_clients(tmp_path):
    w = worker(tmp_path)
    started = asyncio.Event()

    async def wait_forever(urls):
        started.set()
        await asyncio.Future()

    w._execute = wait_forever
    task = asyncio.create_task(w.run("公開の質問"))
    await started.wait()
    task.cancel()
    report = await task
    assert report.state == "cancelled"
    assert w.model.closed
    assert json.loads((w.store.directory / "status.json").read_text())["state"] == "cancelled"


async def test_wall_timeout(tmp_path):
    w = worker(tmp_path)
    w.budget.limits.job_seconds = 0.02

    async def wait_forever(urls):
        await asyncio.Future()

    w._execute = wait_forever
    report = await w.run("公開の質問")
    assert report.stop_reason == "job_timeout"
    assert report.state == "failed"


def test_budget_query_dedup_retry_and_url_normalization():
    budget = Budget(Limits(model_requests=2))
    assert budget.query("Qwen 3") and not budget.query("QWEN  3")
    budget.take("model_requests")
    budget.take("model_requests")
    with pytest.raises(BudgetExceeded):
        budget.take("model_requests")
    assert (
        normalize_url("https://example.org/a?version=2&utm_source=x#one") == "https://example.org/a?version=2"
    )


def test_git_output_rejected_unicode_path_accepted(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError):
        output_root(checkout / "output")
    store = JobStore(tmp_path / "日本語 空白")
    assert store.directory.is_dir()


async def test_quote_correction_is_finite_and_retains_valid_subset_on_failure(tmp_path):
    from ephy_worker.models import ModelError

    class CorrectionFails(FakeModel):
        async def run(self, output_type, prompt, *, stage):
            if stage == "repair_quotes":
                self.calls.append(stage)
                raise ModelError("model_timeout")
            result = await super().run(output_type, prompt, stage=stage)
            if stage == "extract":
                valid = result.claims[0].evidence[0]
                result.claims[0].evidence.append(valid.model_copy(update={"quote": "invented quotation"}))
            return result

    w = worker(tmp_path, model=CorrectionFails())
    report = await w.run("version 2 の制約")
    assert report.state == "partial"
    assert w.model.calls.count("repair_quotes") == 1
    assert report.claims[0].status == "supported_primary"
    assert len(report.evidence) == 1
    assert report.evidence[0].checked


def test_japanese_prose_normalization_preserves_original_evidence_and_passages():
    quote = "原資料の記述、句読点は変更しない。"
    evidence = EvidenceProposal(
        source_id="S1", passage_id="P1", quote=quote, relation="supports", conditions="日本語、条件。"
    )
    claim = ClaimProposal(text="主張、本文。", evidence=[evidence])
    assert claim.text == "主張，本文．"
    assert claim.evidence[0].conditions == "日本語，条件．"
    assert claim.evidence[0].quote == quote
    assert Passage(source_id="S1", passage_id="P1", text=quote).text == quote
