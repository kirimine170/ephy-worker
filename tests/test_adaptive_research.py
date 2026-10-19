"""Real multi-round executor uses positive coverage，history and original budgets．"""

import asyncio
import hashlib
import json
from typing import ClassVar

import pytest
from test_answer_routing import profile

from ephy_worker.budget import Budget
from ephy_worker.config import AnswerPolicy, SearchConfig, WorkerConfig
from ephy_worker.research import ResearchExecutor
from ephy_worker.schema import (
    AdaptiveReview,
    Candidate,
    Claim,
    ClaimCheck,
    ClaimProposal,
    Coverage,
    EvidenceCheck,
    EvidenceProposal,
    Extraction,
    GapResolution,
    Limits,
    Passage,
    Plan,
    Query,
    Selection,
    Source,
)
from ephy_worker.store import JobStore


class ResearchModel:
    metadata: ClassVar[dict] = {"model_id": "offline", "real_model": False}
    def __init__(self, budget, mode="complete"):
        self.budget, self.mode, self.round, self.calls, self.closed = budget, mode, 0, [], False
    async def run(self, output, prompt, *, stage, **bounds):
        self.budget.take("model_requests")
        self.calls.append((stage, bounds))
        data = json.loads(prompt.split("INPUT_DATA_JSON:\n")[1])
        if stage == "plan":
            return Plan(subquestions=["first", "second", "third"], queries=[
                Query(text=f"public fixture {role}", role=role, reason="確認")
                for role in ("overview", "primary", "limitations")])
        if stage == "select":
            return Selection(urls=[c["url"] for c in data["candidates"][:4]], reason="公式本文")
        if stage == "extract":
            passage = data["passages"][-1]
            item_number = 0 if self.mode == "repeat" else self.round
            return Extraction(claims=[ClaimProposal(text=f"項目{item_number}は利用できます．", evidence=[
                EvidenceProposal(source_id=passage["source_id"], passage_id=passage["passage_id"],
                                 quote=passage["text"], relation="supports")])],
                gaps=["second", "third"] if self.round == 0 else ["third"] if self.round == 1 else [])
        assert stage == "review" and output is AdaptiveReview
        claim = data["claims"][0]
        positive = self.mode != "stall" or self.round == 0
        checks = [ClaimCheck(claim_id=claim["claim_id"], status="supported_primary" if positive else "insufficient",
            reason="対象項目の公式本文", evidence_checks=[EvidenceCheck(evidence_id=e["evidence_id"],
                relation="supports" if positive else "context_only", conditions_match=True,
                is_primary=True, reason="条件一致") for e in data["evidence"]])]
        coverage = [Coverage(subquestion=self.round, claim_ids=[claim["claim_id"]])] if positive else []
        if self.mode == "unknown": coverage = [Coverage(subquestion=0, claim_ids=["forged-positive"])]
        resolutions = []
        if self.round and self.mode != "unresolved" and positive:
            resolutions = [GapResolution(gap="second" if self.round == 1 else "third", claim_ids=[claim["claim_id"]])]
        if self.mode == "oldref" and self.round == 1:
            resolutions = [GapResolution(gap="second", claim_ids=["R0-c001"])]
        if self.mode == "conflict" and self.round == 1:
            checks[0].status = "conflicting"
            checks[0].evidence_checks[0].relation = "contradicts"
        additional = [] if self.mode == "noquery" or self.round >= 2 else [
            Query(text=f"new public fixture round {self.round + 1}", role="gap", reason="未確認項目")]
        result = AdaptiveReview(checks=checks, coverage=coverage, resolutions=resolutions,
            additional_queries=additional, gaps=["contradiction"] if self.mode == "conflict" and self.round == 1 else [])
        self.round += 1
        return result
    async def aclose(self): self.closed = True


class Search:
    metadata: ClassVar[dict] = {"provider": "offline"}
    def __init__(self, budget): self.budget, self.closed = budget, False
    async def search(self, query, query_id):
        self.budget.take("search_requests")
        return [Candidate(url=f"https://example.org/{query_id}", query_id=query_id, engine="fixture", rank=1)]
    async def aclose(self): self.closed = True


class Fetcher:
    def __init__(self, budget): self.budget, self.closed = budget, False
    async def fetch(self, candidate, source_id):
        self.budget.take("fetch_requests")
        text = "The specified item is available under the documented standard conditions."
        return Source(source_id=source_id, requested_url=candidate.url, final_url=candidate.url,
            status="ok", kind="html", http_status=200, bytes_received=2000, extractor="fixture",
            body_hash=hashlib.sha256((text + source_id).encode()).hexdigest(),
            passages=[Passage(passage_id=source_id + "-P1", source_id=source_id, text=text, end=len(text))])
    async def aclose(self): self.closed = True


def executor(tmp_path, mode="complete", limit=40, max_rounds=4):
    config = WorkerConfig(model_profiles={"p": profile()}, search=SearchConfig(base_url="http://127.0.0.1:9"),
        answer_policy=AnswerPolicy(enabled=True, max_rounds=max_rounds), limits=Limits(model_requests=limit, job_seconds=300))
    budget = Budget(config.limits)
    model, search, fetcher = ResearchModel(budget, mode), Search(budget), Fetcher(budget)
    return ResearchExecutor(config, "p", JobStore(tmp_path), model=model, search=search, fetcher=fetcher,
                            budget=budget, adaptive=True, final_reserve_seconds=25)


@pytest.mark.asyncio
async def test_three_round_completion_keeps_gap_resolution_and_evidence(tmp_path):
    runner = executor(tmp_path)
    started = runner.budget.started
    runner.budget.take("model_requests")  # Original preflight request，not a reset research budget．
    report = await runner.run("公開資料による比較")
    assert report.state == "completed" and report.answerability == "sufficient"
    assert report.stop_reason == "verified_coverage_complete" and len(report.rounds) == 3
    assert report.rounds[0]["remaining_gaps"] == ["second", "third"]
    assert report.rounds[1]["previous_gaps"] == ["second", "third"]
    assert report.rounds[1]["resolutions"][0]["claim_ids"] == ["R1-c001"]
    assert len(report.evidence) == 3 and len(report.claims) == 3 and not report.gaps
    assert runner.budget.started == started and runner.budget.counts["model_requests"] == len(runner.model.calls) + 1
    assert runner.model.closed and runner.search.closed and runner.fetcher.closed
    assert all(bounds["schema_retries"] == 0 and bounds["max_output_tokens"] <= 1536 for _, bounds in runner.model.calls)


@pytest.mark.parametrize("mode", ["unknown", "oldref", "unresolved", "noquery", "conflict", "stall"])
@pytest.mark.asyncio
async def test_gaps_conflict_stall_and_unknown_ids_never_sufficient(tmp_path, mode):
    runner = executor(tmp_path, mode)
    report = await runner.run("公開資料による比較")
    assert report.state != "completed" and report.answerability != "sufficient"
    assert len(report.rounds) <= 4
    if mode == "noquery": assert report.stop_reason == "no_actionable_fresh_query"
    if mode == "stall": assert report.stop_reason == "two_stalled_rounds"
    if mode in {"unknown", "oldref"}: assert report.failures
    if mode == "conflict": assert "contradiction" in report.gaps and len(report.claims) >= 2


@pytest.mark.asyncio
async def test_round_and_original_physical_and_time_limits(tmp_path):
    runner = executor(tmp_path / "roundcap", max_rounds=1)
    report = await runner.run("公開資料による比較")
    assert report.stop_reason == "adaptive_round_limit" and report.state == "partial"
    runner = executor(tmp_path / "budget", limit=4)
    runner.budget.take("model_requests")
    report = await runner.run("公開資料による比較")
    assert runner.budget.counts["model_requests"] <= 3
    assert report.stop_reason == "model_requests_reserved_for_answer" and report.answerability != "sufficient"
    runner = executor(tmp_path / "time")
    runner.budget.started -= 280
    report = await runner.run("公開資料による比較")
    assert report.state == "failed" and not runner.model.calls
    assert report.answerability != "sufficient"


@pytest.mark.asyncio
async def test_cancellation_closes_all_clients_and_saves(tmp_path):
    runner = executor(tmp_path)
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    runner.model.run = blocked
    task = asyncio.create_task(runner.run("公開資料による比較"))
    await entered.wait()
    task.cancel()
    report = await task
    assert report.state == "cancelled" and report.stop_reason == "cancelled"
    assert runner.model.closed and runner.search.closed and runner.fetcher.closed
    assert (runner.store.directory / "report.json").exists()


@pytest.mark.asyncio
async def test_new_ids_for_old_material_do_not_resolve_gaps_or_unstall(tmp_path):
    runner = executor(tmp_path, "repeat")
    fetch = runner.fetcher.fetch
    async def repeated_hash(candidate, source_id):
        source = await fetch(candidate, source_id)
        source.body_hash = hashlib.sha256(source.passages[0].text.encode()).hexdigest()
        return source
    runner.fetcher.fetch = repeated_hash
    report = await runner.run("公開資料による比較")
    assert report.stop_reason == "two_stalled_rounds" and report.state == "partial"
    assert report.rounds[1]["new_verified_material_count"] == report.rounds[2]["new_verified_material_count"] == 0
    assert report.rounds[1]["new_positive_claim_ids"] == []
    assert report.rounds[1]["resolutions"] == [] and "second" in report.gaps
    assert len(report.claims) == 3 and len(report.evidence) == 3


@pytest.mark.asyncio
async def test_prior_unresolved_claim_keeps_completed_state_partial(tmp_path):
    from test_answer_cache import canonical

    runner = executor(tmp_path)
    original = canonical()
    async def retained_old_claims(urls):
        runner.report.subquestions = original.subquestions
        runner.report.sources = original.sources
        runner.report.claims = [*original.claims, Claim(claim_id="R1-unresolved", text="未確認の旧主張")]
        runner.report.evidence = original.evidence
        runner.coverage = {"0": [original.claims[0].claim_id]}
        runner.report.stop_reason = "no_actionable_fresh_query"
    runner._execute = retained_old_claims
    report = await runner.run("公開資料による比較")
    assert report.state == "partial" and report.answerability == "limited"
    assert report.claims[-1].claim_id == "R1-unresolved" and not report.claims[-1].checked


class PassageBoundModel(ResearchModel):
    def __init__(self, budget, unseen=None):
        super().__init__(budget)
        self.inputs, self.prompts, self.unseen = {}, {}, unseen
        self.metadata = dict(type(self).metadata)

    async def run(self, output, prompt, *, stage, **bounds):
        self.budget.take("model_requests")
        self.calls.append((stage, bounds))
        data = json.loads(prompt.split("INPUT_DATA_JSON:\n")[1])
        self.inputs[stage], self.prompts[stage] = data, prompt
        if stage == "plan":
            return Plan(subquestions=[f"確認項目{index}" for index in range(5)], queries=[
                Query(text=f"public lookup {role}", role=role, reason="確認項目の本文")
                for role in ("overview", "primary", "limitations")])
        if stage == "select":
            return Selection(urls=[item["url"] for item in data["candidates"][:4]], reason="公開本文")
        if stage in {"extract", "repair_quotes"}:
            passage = self.unseen or data["passages"][0]
            return Extraction(claims=[ClaimProposal(text="lookupの公開条件が記載されています．", evidence=[
                EvidenceProposal(source_id=passage["source_id"], passage_id=passage["passage_id"],
                    quote=passage["text"][10:90], relation="supports")])])
        assert stage == "review"
        checks = [ClaimCheck(claim_id=claim["claim_id"], status="supported_primary", reason="公式の条件本文",
            evidence_checks=[EvidenceCheck(evidence_id=item["evidence_id"], relation="supports",
                conditions_match=True, is_primary=True, reason="対象の条件と一致")
                for item in data["evidence"] if item["evidence_id"] in claim["evidence_ids"]])
            for claim in data["claims"]]
        extra = {"coverage": [Coverage(subquestion=0, claim_ids=[data["claims"][0]["claim_id"]])]} if output is AdaptiveReview else {}
        return output(checks=checks, **extra)


def passage_sources(count=4, per_source=2):
    sources = []
    for index in range(count):
        source_id = f"s{index + 1:03d}"
        texts = ["General background without the relevant term. " * 12]
        texts.extend([f"Lookup documented standard conditions for source {index}. " * 12] * (per_source - 1))
        passages = [Passage(passage_id=f"{source_id}-p{number + 1}", source_id=source_id,
            text=text, start=number * 2000, end=number * 2000 + len(text)) for number, text in enumerate(texts)]
        sources.append(Source(source_id=source_id, requested_url=f"https://example.org/{index}",
            status="ok", kind="html", http_status=200, bytes_received=4000, extractor="fixture",
            body_hash=hashlib.sha256((str(index) + "".join(texts)).encode()).hexdigest(), passages=passages))
    return sources


def passage_executor(tmp_path, *, count=4, per_source=2, configured=24, adaptive=True, unseen=None):
    from ephy_worker.schema import Report

    runner = executor(tmp_path)
    runner.adaptive = adaptive
    runner.config.limits.selected_passages = configured
    runner.model = PassageBoundModel(runner.budget, unseen)
    runner.report = Report(job_id=runner.store.job_id, question="lookup", profile_id="p", subquestions=["lookup"],
        sources=passage_sources(count, per_source))
    return runner


def passage_events(runner):
    return [json.loads(line) for line in (runner.store.directory / "events.jsonl").read_text("utf-8").splitlines()]


@pytest.mark.parametrize(("count", "per_source", "configured"), [(4, 2, 24), (4, 3, 4), (2, 1, 8), (6, 2, 8)])
@pytest.mark.asyncio
async def test_adaptive_extract_bounds_whole_ranked_passages_and_preserves_sources(tmp_path, count, per_source, configured):
    from ephy_worker.evidence import select_passages

    runner = passage_executor(tmp_path, count=count, per_source=per_source, configured=configured)
    original = [source.model_dump(exclude={"read_passage_ids"}) for source in runner.report.sources]
    ranked = select_passages(runner.report.sources, "lookup lookup", configured)
    await runner.examine(0)
    actual = runner.model.inputs["extract"]["passages"]
    assert actual == [passage.model_dump() for passage in ranked[:4]]
    assert len(actual) <= min(4, configured)
    assert len({passage["source_id"] for passage in actual}) == len(actual)
    if per_source > 1:
        assert all(passage["passage_id"].endswith("p2") for passage in actual)
    assert original == [source.model_dump(exclude={"read_passage_ids"}) for source in runner.report.sources]
    assert {key for source in runner.report.sources for key in source.read_passage_ids} == {p["passage_id"] for p in actual}
    bounded = [event for event in passage_events(runner) if event["type"] == "input_bounded" and event["stage"] == "extract"]
    assert len(bounded) == 1
    assert (bounded[0]["original_count"], bounded[0]["retained_count"], bounded[0]["limit"]) == (len(ranked), len(actual), min(4, configured))
    assert bounded[0]["reason"] == "adaptive_complete_passage_limit"
    assert "最大3件抽出" in runner.model.prompts["extract"] and "最大5件" not in runner.model.prompts["extract"]
    assert runner.model.inputs["extract"]["subquestions"] == ["lookup"]
    review = runner.model.inputs["review"]
    assert {p["passage_id"] for p in review["passages"]} <= {p["passage_id"] for p in actual}
    assert len(runner.report.evidence) == 1 and runner.report.claims[0].checked
    evidence = runner.report.evidence[0]
    passage = next(p for source in runner.report.sources for p in source.passages if p.passage_id == evidence.passage_id)
    assert evidence.quote == passage.text[evidence.quote_start - passage.start:evidence.quote_end - passage.start]


@pytest.mark.asyncio
async def test_adaptive_passage_bound_keeps_context_gate_and_actual_read_range(tmp_path):
    from ephy_worker.models import estimate_tokens
    from ephy_worker.research import prompt

    runner = passage_executor(tmp_path)
    original_call = runner.call

    async def context_limited(output, task, data, stage):
        if stage == "extract":
            assert len(data["passages"]) == 4
            one = {**data, "passages": data["passages"][:1]}
            reserve = 4096 + estimate_tokens(json.dumps(output.model_json_schema(), ensure_ascii=False)) + 3072
            runner.model.metadata["context_tokens"] = estimate_tokens(prompt(task, one)) + reserve + 1
        return await original_call(output, task, data, stage)

    runner.call = context_limited
    await runner.examine(0)
    actual = runner.model.inputs["extract"]["passages"]
    assert len(actual) == 1
    assert runner.extraction_passage_ids == {actual[0]["passage_id"]}
    assert [key for source in runner.report.sources for key in source.read_passage_ids] == [actual[0]["passage_id"]]
    assert sum(len(source.passages) for source in runner.report.sources) == 8
    reduced = [event for event in passage_events(runner) if event["type"] == "context_reduced"]
    assert len(reduced) == 1 and reduced[0]["original_count"] == 4 and reduced[0]["retained_count"] == 1


@pytest.mark.asyncio
async def test_unsubmitted_complete_passage_cannot_supply_a_quote(tmp_path):
    unseen = passage_sources()[0].passages[0].model_dump()
    runner = passage_executor(tmp_path, unseen=unseen)
    await runner.examine(0)
    assert all(unseen["passage_id"] not in {p["passage_id"] for p in data["passages"]}
        for stage, data in runner.model.inputs.items() if stage in {"extract", "repair_quotes"})
    assert runner.report.evidence == [] and not runner.report.claims[0].checked
    assert runner.report.claims[0].status == "insufficient"
    assert any("モデルへ送信した読取範囲に含まれません" in item["code"] for item in runner.report.failures)
    assert unseen["passage_id"] not in runner.report.sources[0].read_passage_ids
    assert runner.report.sources[0].passages[0].model_dump() == unseen
    assert not runner._adaptive_complete()


@pytest.mark.asyncio
async def test_compact_extract_does_not_drop_initial_subquestions_or_complete_partial_coverage(tmp_path):
    runner = executor(tmp_path, max_rounds=1)
    runner.model = PassageBoundModel(runner.budget)
    report = await runner.run("公開資料のlookup条件")
    expected = [f"確認項目{index}" for index in range(5)]
    assert report.subquestions == runner.model.inputs["extract"]["subquestions"] == expected
    assert report.state == "partial" and report.answerability == "limited"
    assert report.stop_reason == "adaptive_round_limit" and not report.gaps and not report.failures
    assert report.metrics["adaptive_coverage"] == {"0": ["R0-c001"]}
    assert report.claims[0].checked and len(report.claims) == 1


@pytest.mark.asyncio
async def test_legacy_examine_keeps_configured_passages_prompt_and_unbounded_call_api(tmp_path):
    from ephy_worker.evidence import select_passages

    runner = passage_executor(tmp_path, adaptive=False, configured=8)
    expected = select_passages(runner.report.sources, "lookup lookup", 8)
    await runner.examine(0)
    assert runner.model.inputs["extract"]["passages"] == [passage.model_dump() for passage in expected]
    assert len(expected) == 8
    assert "最大5件抽出" in runner.model.prompts["extract"] and "最大3件" not in runner.model.prompts["extract"]
    assert "各確認項目へ直接答える根拠を優先" not in runner.model.prompts["extract"]
    assert all(bounds == {} for _, bounds in runner.model.calls)
    assert not any(event["type"] == "input_bounded" for event in passage_events(runner))


class RecoveryModel(ResearchModel):
    def __init__(self, budget, *, omit_evidence=False):
        super().__init__(budget)
        self.inputs, self.prompts, self.omit_evidence = {}, {}, omit_evidence

    async def run(self, output, prompt, *, stage, **bounds):
        self.budget.take("model_requests")
        self.calls.append((stage, bounds))
        data = json.loads(prompt.split("INPUT_DATA_JSON:\n")[1])
        self.inputs[stage], self.prompts[stage] = data, prompt
        if stage == "plan":
            return Plan(subquestions=["利用条件の主な違い"], queries=[
                Query(text=f"public conditions {role}", role=role, reason="依頼の公開条件")
                for role in ("overview", "primary", "limitations")])
        if stage == "select":
            return Selection(urls=[c["url"] for c in data["candidates"][:4]], reason="直接の原資料")
        if stage == "extract":
            passage = data["passages"][0]
            return Extraction(claims=[ClaimProposal(text="記載の条件で対象を利用できます．", conditions="文書の条件",
                uncertainty=["別版は未確認"], evidence=[EvidenceProposal(source_id=passage["source_id"],
                    passage_id=passage["passage_id"], quote=passage["text"], relation="supports")])])
        assert stage == "review"
        checks = [ClaimCheck(claim_id=c["claim_id"], status="supported_primary", reason="対象の条件を原資料本文が直接支持",
            evidence_checks=[EvidenceCheck(evidence_id=e["evidence_id"], relation="supports", conditions_match=True,
                is_primary=True, reason="同じ対象と適用条件を本文で確認") for e in data["evidence"]
                if e["evidence_id"] in c["evidence_ids"] and not self.omit_evidence]) for c in data["claims"]]
        return AdaptiveReview(checks=checks, coverage=[Coverage(subquestion=0,
            claim_ids=[data["claims"][0]["claim_id"]])])


def recovery_executor(tmp_path, *, failure="html_body_unavailable", omit_evidence=False):
    runner = executor(tmp_path)
    runner.model = RecoveryModel(runner.budget, omit_evidence=omit_evidence)
    fetch = runner.fetcher.fetch

    async def fetch_with_unused_failure(candidate, source_id):
        if source_id != "S2":
            return await fetch(candidate, source_id)
        runner.budget.take("fetch_requests")
        return Source(source_id=source_id, requested_url=candidate.url, final_url=candidate.url,
            status="extraction_failed", kind="html", http_status=200, extractor="fixture", error=failure)

    runner.fetcher.fetch = fetch_with_unused_failure
    return runner


@pytest.mark.asyncio
async def test_real_adaptive_completion_resolves_only_unused_html_and_cache_rechecks(tmp_path):
    from ephy_worker.answer_cache import AnswerCache, positive_facts, unresolved_failure_code

    runner = recovery_executor(tmp_path / "jobs")
    started = runner.budget.started
    report = await runner.run("公開資料で利用条件の主な違いを短く説明")
    assert report.state == "completed" and report.answerability == "sufficient"
    assert report.stop_reason == "verified_coverage_complete" and len(report.rounds) == 1
    assert report.failures == [{"stage": "fetch", "code": "S2:extraction_failed"}]
    assert report.sources[1].status == "extraction_failed" and report.sources[1].error == "html_body_unavailable"
    assert not report.sources[1].read_passage_ids and not report.sources[1].passages
    assert len(report.acquisition_resolutions) == 1
    assert report.acquisition_resolutions[0].supporting_claim_ids == ["R0-c001"]
    assert report.rounds[0]["failure_count"] == 1 and report.rounds[0]["claim_ids"] == ["R0-c001"]
    facts, reason = positive_facts(report)
    assert reason == "ok" and facts[0].conditions == "文書の条件" and facts[0].uncertainty == ("別版は未確認",)
    assert unresolved_failure_code(report, facts) == "ok"
    assert runner.budget.started == started and runner.budget.counts["model_requests"] == 4
    assert runner.budget.counts["search_requests"] == 3 and runner.budget.counts["fetch_requests"] == 3
    original = report.model_dump_json()
    cache = AnswerCache(tmp_path / "state", AnswerPolicy())
    assert cache.put(report, "authenticated-scope") == "stored"
    loaded, code = AnswerCache(tmp_path / "state", AnswerPolicy()).lookup(report.question, "authenticated-scope")
    assert code == "hit" and loaded.failures == report.failures and loaded.rounds == report.rounds
    assert loaded.acquisition_resolutions == report.acquisition_resolutions
    assert next(s for s in loaded.sources if s.source_id == "S2").model_dump() == report.sources[1].model_dump()
    assert report.model_dump_json() == original
    assert runner.model.closed and runner.search.closed and runner.fetcher.closed


@pytest.mark.parametrize("failure", ["parser_exception", "parser_exit:1", "parser_monitor_failed", "parser_timeout"])
@pytest.mark.asyncio
async def test_hard_acquisition_failures_keep_real_positive_progress_partial(tmp_path, failure):
    from ephy_worker.answer_cache import AnswerCache, positive_facts

    runner = recovery_executor(tmp_path / "jobs", failure=failure)
    report = await runner.run("公開資料で利用条件を短く説明")
    assert report.state == "partial" and report.answerability == "limited"
    assert report.stop_reason == "no_actionable_fresh_query"
    assert report.acquisition_resolutions == [] and report.failures[0]["code"] == "S2:extraction_failed"
    assert positive_facts(report)[0][0].claim_id == "R0-c001"
    assert report.sources[1].error == failure
    assert AnswerCache(tmp_path / "state", AnswerPolicy()).put(report, "scope") == "incomplete_report"


@pytest.mark.asyncio
async def test_omitted_evidence_check_cannot_be_saved_by_concise_prompt_or_resolution(tmp_path):
    runner = recovery_executor(tmp_path, omit_evidence=True)
    report = await runner.run("公開資料で利用条件を短く説明")
    assert report.state == "failed" and report.answerability == "unresolved"
    assert not report.claims[0].checked and report.claims[0].status == "insufficient"
    assert report.acquisition_resolutions == []
    assert any(f["stage"] == "coverage" for f in report.failures)
    assert report.sources[1].error == "html_body_unavailable"


@pytest.mark.asyncio
async def test_cleanup_failure_after_resolution_is_retained_and_prevents_cache(tmp_path):
    from ephy_worker.answer_cache import AnswerCache, positive_facts, unresolved_failure_code

    runner = recovery_executor(tmp_path / "jobs")

    async def failed_cleanup():
        raise RuntimeError("offline cleanup failure")

    runner.fetcher.aclose = failed_cleanup
    report = await runner.run("公開資料で利用条件を短く説明")
    assert report.state == "partial" and report.answerability == "limited"
    assert len(report.acquisition_resolutions) == 1
    assert report.failures == [{"stage": "fetch", "code": "S2:extraction_failed"},
                               {"stage": "cleanup", "code": "RuntimeError"}]
    assert unresolved_failure_code(report, positive_facts(report)[0]) == "unresolved_failure"
    assert AnswerCache(tmp_path / "state", AnswerPolicy()).put(report, "scope") == "incomplete_report"


@pytest.mark.asyncio
async def test_cancellation_after_positive_review_does_not_resolve_or_cache(tmp_path):
    from ephy_worker.answer_cache import AnswerCache, positive_facts

    runner = recovery_executor(tmp_path / "jobs")
    examine = runner.examine

    async def cancel_after_review(round_number):
        await examine(round_number)
        raise asyncio.CancelledError

    runner.examine = cancel_after_review
    report = await runner.run("公開資料で利用条件を短く説明")
    assert report.state == "cancelled" and report.stop_reason == "cancelled"
    assert positive_facts(report)[0] and report.acquisition_resolutions == []
    assert runner.model.closed and runner.search.closed and runner.fetcher.closed
    assert AnswerCache(tmp_path / "state", AnswerPolicy()).put(report, "scope") == "incomplete_report"


@pytest.mark.asyncio
async def test_adaptive_prompt_bounds_keep_every_emitted_dependency_and_explicit_scope(tmp_path):
    runner = recovery_executor(tmp_path)
    question = "公開条件と明示した内部実装・定量性能の比較を出典付きで説明"
    report = await runner.run(question)
    prompts, inputs = runner.model.prompts, runner.model.inputs
    assert "利用者が明示した依頼" in prompts["plan"] and "回答に不可欠な前提だけ" in prompts["plan"]
    assert "求められていない内部実装・性能測定" in prompts["plan"]
    assert "明示された場合はその依頼を保持" in prompts["plan"]
    assert inputs["plan"]["question"] == question
    assert report.subquestions == inputs["extract"]["subquestions"] == ["利用条件の主な違い"]
    assert "最も強い一次資料" in prompts["extract"] and "同じ内容の引用を重ねません" in prompts["extract"]
    assert "条件差・反証の保持に必要な引用は省きません" in prompts["extract"]
    assert "15〜25字" in prompts["review"] and "全claimと全evidence_idを個別に検査" in prompts["review"]
    assert "実在する条件・不確実性・矛盾や必要な追加queryは省略しません" in prompts["review"]
    assert {e["evidence_id"] for e in inputs["review"]["evidence"]} == {e.evidence_id for e in report.evidence}
    assert inputs["review"]["claims"][0]["conditions"] == "文書の条件"
    assert inputs["review"]["claims"][0]["uncertainty"] == ["別版は未確認"]
    bounds = dict(runner.model.calls)["review"]
    assert bounds["max_output_tokens"] == 1536 and bounds["schema_retries"] == 0
    assert 0 < bounds["timeout_seconds"] <= runner.config.model_profiles["p"].timeout_seconds
    assert runner.budget.limits.job_seconds == 300 and runner.final_reserve_seconds == 25


@pytest.mark.parametrize(("supplemental_url", "failed_url", "expected_supplied", "source_count"), [
    ("https://example.org/Q2", "https://example.org/Q2", True, 3),
    ("https://EXAMPLE.ORG/Q2?utm_source=requested&gclid=tracking#details", "https://example.org/Q2", True, 3),
    ("https://example.org/required", "https://example.org/required", True, 4),
    (None, "https://example.org/Q2", False, 3),
], ids=["exact-search-collision", "normalized-search-collision", "new-supplied-source", "unused-search-source"])
@pytest.mark.asyncio
async def test_supplied_html_failure_keeps_real_fetch_provenance_and_cannot_resolve_or_cache(
    tmp_path, monkeypatch, supplemental_url, failed_url, expected_supplied, source_count,
):
    from contextlib import asynccontextmanager

    from test_fetch import HTML, Response, public_dns

    from ephy_worker import fetch
    from ephy_worker.answer_cache import (
        AnswerCache,
        build_acquisition_resolutions,
        complete_support,
        positive_facts,
        unresolved_failure_code,
    )
    from ephy_worker.research import normalize_url
    from ephy_worker.schema import Report

    class URLTransport:
        def __init__(self):
            self.calls, self.closed = [], False

        @asynccontextmanager
        async def request(self, url, addresses, timeout):
            self.calls.append(url)
            yield Response(body=b"" if normalize_url(url) == failed_url else HTML)

        async def aclose(self):
            self.closed = True

    extract_document = fetch.extract_document

    async def empty_html_failure(data, *, url, **kwargs):
        if normalize_url(url) == failed_url:
            assert data == b""
            return {"status": "extraction_failed", "error": "html_body_unavailable", "extractor": "offline-empty-html"}
        return await extract_document(data, url=url, **kwargs)

    monkeypatch.setattr(fetch, "extract_document", empty_html_failure)
    runner = executor(tmp_path / "jobs")
    runner.model = RecoveryModel(runner.budget)
    transport = URLTransport()
    runner.fetcher = fetch.PublicFetcher(runner.budget, transport=transport, resolver=public_dns)
    report = await runner.run("公開資料で利用条件の主な違いを短く説明", [supplemental_url] if supplemental_url else [])
    failed = next(source for source in report.sources if normalize_url(source.requested_url) == failed_url)
    candidate = runner.candidates[failed_url]
    assert candidate.supplied is expected_supplied and failed.supplied is expected_supplied
    assert len(report.sources) == len(transport.calls) == len(set(transport.calls)) == source_count
    assert failed.status == "extraction_failed" and failed.error == "html_body_unavailable"
    assert failed.kind == "html" and failed.http_status == 200 and failed.extractor == "offline-empty-html"
    assert failed.bytes_received == 0 and failed.body_hash is None and not failed.passages and not failed.read_passage_ids
    assert report.failures == [{"stage": "fetch", "code": f"{failed.source_id}:extraction_failed"}]
    assert len(report.rounds) == 1 and report.rounds[0]["failure_count"] == 1
    assert all(source.fetched_at <= report.finished_at for source in report.sources)
    if failed_url.endswith("Q2"):
        assert candidate.url == failed.requested_url == "https://example.org/Q2"
        assert failed.query_ids == ["Q2"] and failed.engines == ["fixture"]
        assert next(item for item in report.candidates if item.url == candidate.url).supplied is expected_supplied
    facts, reason = positive_facts(report)
    assert reason == "ok" and [fact.claim_id for fact in facts] == ["R0-c001"]
    assert complete_support(report, facts) and not report.gaps
    assert all(item.source_id != failed.source_id for item in report.evidence)
    original = report.model_dump_json()
    cache = AnswerCache(tmp_path / "state", AnswerPolicy())
    if expected_supplied:
        assert report.state == "partial" and report.answerability == "limited"
        assert report.stop_reason == "no_actionable_fresh_query"
        assert report.acquisition_resolutions == build_acquisition_resolutions(report) == []
        assert unresolved_failure_code(report, facts) == "unresolved_failure" and not runner._adaptive_complete()
        assert not any(event["type"] == "acquisition_failure_resolved" for event in passage_events(runner))
        assert cache.put(report, "scope") == "incomplete_report"
        assert AnswerCache(tmp_path / "state", AnswerPolicy()).lookup(report.question, "scope") == (None, "miss")
    else:
        assert report.state == "completed" and report.answerability == "sufficient"
        assert report.stop_reason == "verified_coverage_complete" and runner._adaptive_complete()
        assert len(report.acquisition_resolutions) == 1 and unresolved_failure_code(report, facts) == "ok"
        assert report.acquisition_resolutions[0].source_id == failed.source_id
        assert cache.put(report, "scope") == "stored"
        cached, code = AnswerCache(tmp_path / "state", AnswerPolicy()).lookup(report.question, "scope")
        assert code == "hit" and cached.failures == report.failures and cached.rounds == report.rounds
        assert next(source for source in cached.sources if source.source_id == failed.source_id).model_dump() == failed.model_dump()
    saved = Report.model_validate_json((runner.store.directory / "report.json").read_bytes())
    assert saved.model_dump_json() == original == report.model_dump_json()
    assert transport.closed and runner.model.closed and runner.search.closed


def flat_review_fixture():
    from ephy_worker.schema import Evidence

    sources = passage_sources(2, 2)
    claims = [Claim(claim_id="c1", text="対象の利用条件", evidence_ids=["e1", "e2"],
                    conditions="特定の版", uncertainty=["他の版は未確認"]),
              Claim(claim_id="c2", text="対象の別の条件", evidence_ids=["e3"])]
    evidence = [Evidence(evidence_id=key, source_id=source.source_id,
        passage_id=source.passages[0].passage_id, quote=source.passages[0].text[:80],
        quote_start=0, quote_end=80, relation="supports", conditions="特定の版")
        for key, source in zip(("e1", "e2", "e3"), (sources[0], sources[1], sources[0]), strict=True)]
    data = {"question": "公開条件を比較", "subquestions": ["条件"], "previous_gaps": ["未確認項目"],
        "claims": [c.model_dump() for c in claims], "evidence": [e.model_dump() for e in evidence],
        "sources": [s.model_dump() for s in sources],
        "passages": [p.model_dump() for s in sources for p in s.passages],
        "previous_claims_reference": [Claim(claim_id="old", text="過去の条件").model_dump()],
        "previous_evidence_reference": [], "origin_groups": []}
    wire = {"claim_checks": [
        {"claim_id": "c1", "status": "conflicting", "reason": "条件が異なる反証を保持",
         "conditions": "特定の版", "uncertainty": ["他の版は未確認"], "inference_from": ["old"]},
        {"claim_id": "c2", "status": "insufficient", "reason": "今回の読取範囲で未確認"}],
        "evidence_checks": [
            {"claim_id": owner, "evidence_id": key, "relation": relation,
             "conditions_match": matches, "is_primary": primary, "reason": "対象の版の条件を照合"}
            for owner, key, relation, matches, primary in [
                ("c1", "e1", "supports", True, True), ("c1", "e2", "contradicts", True, False),
                ("c2", "e3", "context_only", False, False)]],
        "coverage": [{"subquestion": 0, "claim_ids": ["old"]}],
        "resolutions": [{"gap": "未確認項目", "claim_ids": ["c2"]}], "gaps": ["条件の矛盾"],
        "additional_queries": [{"text": "public alternate version terms", "role": "gap", "reason": "版の差を確認"}]}
    return data, wire


def flat_completion(content, *, mode="prompted_json", finish="stop"):
    import httpx

    message = {"role": "assistant", "content": content}
    if mode == "tool":
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function",
            "function": {"name": "final_result", "arguments": content}}]}
    return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
        "model": "offline", "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 67, "total_tokens": 190}})


@pytest.mark.parametrize("mode", ["prompted_json", "native_json", "tool"])
@pytest.mark.asyncio
async def test_real_model_flat_review_transport_returns_full_canonical_result(mode):
    import httpx

    from ephy_worker.evidence import apply_review
    from ephy_worker.models import ModelRunner
    from ephy_worker.research import prompt
    from ephy_worker.schema import AdaptiveReviewWire, Evidence

    data, wire = flat_review_fixture()
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        schema_text = json.dumps(payload)
        assert "claim_checks" in schema_text and "AdaptiveReviewWire" in schema_text
        assert "ClaimCheck" not in schema_text.replace("AdaptiveClaimCheck", "")
        assert payload["max_completion_tokens"] == 1024
        return flat_completion(json.dumps(wire, ensure_ascii=False), mode=mode,
                               finish="tool_calls" if mode == "tool" else "stop")
    budget = Budget(Limits(job_seconds=300))
    runner = ModelRunner(profile().model_copy(update={"output_mode": mode}), budget,
                         transport=httpx.MockTransport(handler))
    started = budget.started
    try:
        result = await runner.run(AdaptiveReview, prompt("照合", data), stage="review", max_output_tokens=1024,
                                  timeout_seconds=5, schema_retries=0)
        assert type(result) is AdaptiveReview and "checks" in result.model_dump()
        assert "claim_checks" not in result.model_dump() and AdaptiveReview.model_wire_type() is AdaptiveReviewWire
        assert result.checks[0].conditions == "特定の版" and result.checks[0].uncertainty == ["他の版は未確認"]
        assert result.checks[0].inference_from == ["old"]
        assert [e.model_dump() for c in result.checks for e in c.evidence_checks] == [
            {k: v for k, v in row.items() if k != "claim_id"} for row in wire["evidence_checks"]]
        assert result.coverage[0].claim_ids == ["old"] and result.resolutions[0].claim_ids == ["c2"]
        assert result.gaps == wire["gaps"] and result.additional_queries[0].role == "gap"
        claims = [Claim.model_validate(c) for c in data["claims"]]
        evidence = [Evidence.model_validate(e) for e in data["evidence"]]
        checked = apply_review(claims, evidence, result, [])
        assert checked[0].status == "conflicting" and checked[1].status == "insufficient"
        assert evidence[1].relation == "contradicts" and evidence[1].conditions_match is True
        assert evidence[1].is_primary is False and evidence[2].relation == "context_only"
        assert evidence[2].conditions_match is False and checked[0].uncertainty == ["他の版は未確認"]
        assert len(requests) == budget.counts["model_requests"] == 1 and budget.started == started
        assert budget.input_tokens == 123 and budget.output_tokens == 67
        assert runner.metadata["call_diagnostics"] == [{"finish_reason": "tool_calls" if mode == "tool" else "stop",
                                                       "input_tokens": 123, "output_tokens": 67}]
    finally:
        await runner.aclose()
    assert runner.transport.call_timeout_seconds is None and runner.transport.call_max_output_tokens is None


@pytest.mark.parametrize("fault", ["missing-claim", "duplicate-claim", "unknown-claim", "missing-evidence",
    "duplicate-evidence", "unknown-evidence", "wrong-owner", "unknown-owner", "nested-mixed", "canonical-shape",
    "unknown-field-secret", "bad-bool", "unknown-coverage", "duplicate-coverage", "unknown-gap", "duplicate-resolution",
    "unknown-inference", "wrong-source-input", "duplicate-input-owner"])
@pytest.mark.asyncio
async def test_real_model_flat_review_rejects_invalid_rows_without_retry_or_secret_leak(fault):
    import httpx

    from ephy_worker.models import ModelError, ModelRunner
    from ephy_worker.research import prompt

    data, wire = flat_review_fixture()
    secret = "sk-private-conversation-should-never-appear"
    if fault == "missing-claim": wire["claim_checks"].pop()
    elif fault == "duplicate-claim": wire["claim_checks"].append(wire["claim_checks"][0])
    elif fault == "unknown-claim": wire["claim_checks"][0]["claim_id"] = secret
    elif fault == "missing-evidence": wire["evidence_checks"].pop()
    elif fault == "duplicate-evidence": wire["evidence_checks"].append(wire["evidence_checks"][0])
    elif fault == "unknown-evidence": wire["evidence_checks"][0]["evidence_id"] = secret
    elif fault == "wrong-owner": wire["evidence_checks"][0]["claim_id"] = "c2"
    elif fault == "unknown-owner": wire["evidence_checks"][0]["claim_id"] = secret
    elif fault == "nested-mixed": wire["claim_checks"][0]["evidence_checks"] = [secret]
    elif fault == "canonical-shape": wire = {"checks": [{"claim_id": secret, "status": "supported_primary"}]}
    elif fault == "unknown-field-secret": wire["claim_checks"][0][secret] = secret
    elif fault == "bad-bool": wire["evidence_checks"][0]["conditions_match"] = secret
    elif fault == "unknown-coverage": wire["coverage"][0]["claim_ids"] = [secret]
    elif fault == "duplicate-coverage": wire["coverage"].append(wire["coverage"][0])
    elif fault == "unknown-gap": wire["resolutions"][0]["gap"] = secret
    elif fault == "duplicate-resolution": wire["resolutions"].append(wire["resolutions"][0])
    elif fault == "unknown-inference": wire["claim_checks"][0]["inference_from"] = [secret]
    elif fault == "wrong-source-input": data["evidence"][0]["source_id"] = data["sources"][1]["source_id"]
    elif fault == "duplicate-input-owner": data["claims"][1]["evidence_ids"].append("e1")
    physical = []
    async def handler(request):
        physical.append(request)
        return flat_completion(json.dumps(wire))
    budget = Budget(Limits())
    runner = ModelRunner(profile(), budget, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError) as error:
            await runner.run(AdaptiveReview, prompt("照合", data), stage="review", schema_retries=0)
        assert error.value.code == "model_schema_invalid"
        diagnostic = runner.metadata["call_diagnostics"][0]
        assert secret not in json.dumps(diagnostic) and secret not in str(error.value)
        assert len(physical) == budget.counts["model_requests"] == 1
        assert diagnostic["input_tokens"] == 123 and diagnostic["output_tokens"] == 67
        if fault in {"unknown-field-secret", "nested-mixed", "bad-bool", "canonical-shape"}:
            assert "UnexpectedModelBehavior" in diagnostic["error_classes"]
            assert "ToolRetryError" in diagnostic["error_classes"] and "ValidationError" in diagnostic["error_classes"]
            assert diagnostic["validation"] and all("input" not in row for row in diagnostic["validation"])
        if fault == "bad-bool":
            assert {"type": "bool_type", "path": ["evidence_checks", "[]", "conditions_match"]} in diagnostic["validation"]
        if fault == "unknown-field-secret":
            assert diagnostic["finish_reason"] == "stop"
            assert {"type": "extra_forbidden", "path": ["claim_checks", "[]"]} in diagnostic["validation"]
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_real_model_wire_protocol_is_generic_and_legacy_review_is_unchanged():
    import httpx
    from pydantic import BaseModel

    from ephy_worker.models import ModelRunner, model_wire_type
    from ephy_worker.schema import Review

    class Flat(BaseModel):
        text: str
    class Public(BaseModel):
        value: str
        @classmethod
        def model_wire_type(cls): return Flat
        @classmethod
        def model_from_wire(cls, value, *, prompt): return cls(value=value.text + prompt)
    responses = [flat_completion('{"text":"public"}'), flat_completion('{"checks":[]}')]
    runner = ModelRunner(profile(), Budget(Limits()), transport=httpx.MockTransport(lambda request: responses.pop(0)))
    try:
        assert model_wire_type(Review) is Review
        result = await runner.run(Public, "-context", stage="fixture", schema_retries=0)
        assert type(result) is Public and result.value == "public-context"
        legacy = await runner.run(Review, "legacy", stage="review", schema_retries=0)
        assert type(legacy) is Review and legacy.checks == []
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_adaptive_review_projection_retains_full_semantics_and_previous_context(tmp_path):
    runner = recovery_executor(tmp_path)
    await runner.run("公開条件の比較")
    data = runner.model.inputs["review"]
    projection = data["projection"]
    assert projection["version"] == "adaptive-review-semantic-v1"
    assert set(projection["omitted_fields"]) == {"claims", "evidence", "passages"}
    assert data["claims"][0]["conditions"] == "文書の条件"
    assert data["claims"][0]["uncertainty"] == ["別版は未確認"]
    assert {"checked", "status", "reason"}.isdisjoint(data["claims"][0])
    assert {"checked", "quote_start", "quote_end", "is_primary", "conditions_match"}.isdisjoint(data["evidence"][0])
    evidence = runner.report.evidence[0]
    actual = next(p for s in runner.report.sources for p in s.passages if p.passage_id == evidence.passage_id)
    assert data["passages"][0]["text"] == actual.text and data["evidence"][0]["quote"] == evidence.quote
    assert data["passages"][0]["page"] == actual.page and "start" not in data["passages"][0]
    assert "previous_claims_reference" in data and "previous_evidence_reference" in data
    assert all({"source_id", "requested_url", "final_url", "publisher", "published_at", "limitations", "origin_urls"} <= s.keys()
               for s in data["sources"])
    assert "origin_groups" in data and "claim_checks" in runner.model.prompts["review"]
    assert "周辺的な導入説明は追加しません" in runner.model.prompts["extract"]
    events = [e for e in passage_events(runner) if e["type"] == "input_projection"]
    assert len(events) == 1 and events[0]["omitted_fields"] == projection["omitted_fields"]


def duplicate_body_runner(tmp_path):
    runner = passage_executor(tmp_path, count=4, per_source=3)
    sources = runner.report.sources
    for source in sources:
        source.query_ids = ["q-overview"]
    sources[2].query_ids = ["q-primary"]
    sources[3] = sources[2].model_copy(deep=True, update={"source_id": "duplicate", "supplied": True,
                                                       "query_ids": ["q-limit"]})
    sources[3].passages = [p.model_copy(update={"source_id": "duplicate", "passage_id": f"duplicate-p{i}"})
                           for i, p in enumerate(sources[2].passages)]
    sources[2].passages[2].text = "Distinct prerequisite zephyr spectrum official conditions. " * 8
    sources[2].passages[1].text = "Lookup prerequisite zephyr spectrum documented standard conditions. " * 12
    sources[3].passages[2].text = sources[2].passages[2].text
    sources[3].passages[1].text = sources[2].passages[1].text
    sources[2].body_hash = sources[3].body_hash = hashlib.sha256(
        "".join(p.text for p in sources[2].passages).encode()).hexdigest()
    runner.report.queries = [
        {"query_id": "q-overview", "text": "lookup", "role": "overview"},
        {"query_id": "q-primary", "text": "zephyr spectrum", "role": "primary"},
        {"query_id": "q-limit", "text": "prerequisite", "role": "limitations"}]
    return runner


@pytest.mark.asyncio
async def test_identical_bodies_free_slot_for_actual_primary_query_passage_without_mutation(tmp_path):
    runner = duplicate_body_runner(tmp_path)
    original = [s.model_dump(exclude={"read_passage_ids"}) for s in runner.report.sources]
    await runner.examine(0)
    actual = runner.model.inputs["extract"]["passages"]
    assert len(actual) == 4 and len({p["source_id"] for p in actual}) == 3
    assert actual[-1]["passage_id"] == runner.report.sources[2].passages[2].passage_id
    assert all(p["source_id"] != "duplicate" for p in actual)
    assert [s.model_dump(exclude={"read_passage_ids"}) for s in runner.report.sources] == original
    assert runner.report.sources[3].supplied and runner.report.sources[3].read_passage_ids == []
    assert set(runner.extraction_passage_ids) == {p["passage_id"] for p in actual}
    assert {p for s in runner.report.sources for p in s.read_passage_ids} == set(runner.extraction_passage_ids)
    for item in actual:
        assert item == next(p.model_dump() for s in runner.report.sources for p in s.passages if p.passage_id == item["passage_id"])
    event = next(e for e in passage_events(runner) if e["type"] == "passage_selection")
    assert event["representative_groups"][-1] == {"representative_id": "s003", "source_ids": ["s003", "duplicate"]}
    assert event["selection"][-1] == {"passage_id": actual[-1]["passage_id"], "reason": "query_role_relevance",
                                      "query_id": "q-primary", "role": "primary"}
    submitted = next(e for e in passage_events(runner) if e["type"] == "passages_submitted")
    assert set(submitted["passage_ids"]) == set(runner.extraction_passage_ids) and submitted["retained_count"] == 4


@pytest.mark.parametrize("difference", ["hash", "kind", "extractor", "text", "status", "bytes", "http"])
def test_body_representatives_require_real_matching_usable_acquisition(tmp_path, difference):
    from ephy_worker.evidence import select_passages

    runner = duplicate_body_runner(tmp_path)
    duplicate = runner.report.sources[3]
    if difference == "hash": duplicate.body_hash = "b" * 64
    elif difference == "kind": duplicate.kind = "pdf"
    elif difference == "extractor": duplicate.extractor = "other-extractor"
    elif difference == "text": duplicate.passages[0].text += "different"
    elif difference == "status": duplicate.status = "partial"
    elif difference == "bytes": duplicate.bytes_received = 0
    elif difference == "http": duplicate.http_status = 503
    original = [s.model_dump() for s in runner.report.sources]
    ranked = select_passages(runner.report.sources, "lookup", 24)
    assert runner.adaptive_passages(ranked, "lookup") == ranked[:4]
    assert [s.model_dump() for s in runner.report.sources] == original
    event = next(e for e in passage_events(runner) if e["type"] == "passage_selection")
    assert all(len(g["source_ids"]) == 1 for g in event["representative_groups"])


@pytest.mark.parametrize(("matches", "status"), [(True, "conflicting"), (False, "insufficient")])
@pytest.mark.asyncio
async def test_wire_conversion_keeps_matching_and_mismatching_negative_evidence_gates(matches, status):
    import httpx

    from ephy_worker.evidence import apply_review
    from ephy_worker.models import ModelRunner
    from ephy_worker.research import prompt
    from ephy_worker.schema import Evidence

    data, wire = flat_review_fixture()
    wire["evidence_checks"][1]["conditions_match"] = matches
    runner = ModelRunner(profile(), Budget(Limits()),
        transport=httpx.MockTransport(lambda request: flat_completion(json.dumps(wire))))
    try:
        review = await runner.run(AdaptiveReview, prompt("照合", data), stage="review", schema_retries=0)
        evidence = [Evidence.model_validate(e) for e in data["evidence"]]
        checked = apply_review([Claim.model_validate(c) for c in data["claims"]], evidence, review, [])
        assert checked[0].status == status and checked[0].checked
        assert evidence[1].checked and evidence[1].relation == "contradicts" and evidence[1].conditions_match is matches
        assert "他の版は未確認" in checked[0].uncertainty
        if not matches: assert any("条件・日時・version等が不一致" in value for value in checked[0].uncertainty)
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_real_wire_context_estimates_provider_schema_and_tightened_actual_output_bound():
    import httpx
    from pydantic import BaseModel, Field, create_model

    from ephy_worker.models import ModelRunner, estimate_tokens

    class SmallWire(BaseModel):
        text: str
    class Adapter(BaseModel):
        @classmethod
        def model_wire_type(cls): return SmallWire
        @classmethod
        def model_from_wire(cls, value, *, prompt):
            return cls(**{field: value.text for field in cls.model_fields})
    public = create_model("LargePublicResult", __base__=Adapter, **{
        f"field_{number}": (str, Field(description="canonical public material " * 40)) for number in range(30)})
    active = profile().model_copy(update={"context_tokens": 4096, "max_output_tokens": 2048})
    assert estimate_tokens(json.dumps(public.model_json_schema())) + 1024 + 256 > active.context_tokens
    physical = []
    async def handler(request):
        physical.append(json.loads(request.content))
        return flat_completion('{"text":"retained"}')
    budget = Budget(Limits())
    runner = ModelRunner(active, budget, transport=httpx.MockTransport(handler))
    try:
        result = await runner.run(public, "small request", stage="fixture", max_output_tokens=256,
                                  timeout_seconds=5, schema_retries=0)
        assert len(type(result).model_fields) == 30 and set(result.model_dump().values()) == {"retained"}
        assert len(physical) == budget.counts["model_requests"] == 1
        assert physical[0]["max_completion_tokens"] == 256
        assert "SmallWire" in json.dumps(physical[0]) and "LargePublicResult" not in json.dumps(physical[0])
    finally:
        await runner.aclose()


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
@pytest.mark.asyncio
async def test_real_wire_timeout_and_cancellation_keep_physical_accounting_and_cleanup(ending):
    import httpx

    from ephy_worker.models import ModelError, ModelRunner
    from ephy_worker.research import prompt

    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.sleep(10)
        raise AssertionError("bounded call must not finish")
    data, _ = flat_review_fixture()
    budget = Budget(Limits())
    runner = ModelRunner(profile(), budget, transport=httpx.MockTransport(handler))
    try:
        task = asyncio.create_task(runner.run(AdaptiveReview, prompt("照合", data), stage="review",
            max_output_tokens=256, timeout_seconds=0.02 if ending == "timeout" else 5, schema_retries=0))
        await entered.wait()
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            with pytest.raises(ModelError) as error: await task
            assert error.value.code == "model_timeout"
        assert budget.counts["model_requests"] == 1
        assert runner.transport.call_timeout_seconds is None and runner.transport.call_max_output_tokens is None
        assert runner.metadata["call_diagnostics"] == [{}]
    finally:
        await runner.aclose()
    assert runner.client.is_closed()


@pytest.mark.asyncio
async def test_schema_diagnostics_coarse_finish_and_bounded_cause_chain_hide_unknown_values():
    import httpx

    from ephy_worker.models import ModelError, ModelRunner, _schema_diagnostic
    from ephy_worker.research import prompt
    from ephy_worker.schema import AdaptiveReviewWire

    secret = "private-conversation-secret-field"
    data, wire = flat_review_fixture()
    runner = ModelRunner(profile(), Budget(Limits()), transport=httpx.MockTransport(
        lambda request: flat_completion(json.dumps(wire), finish=secret)))
    try:
        with pytest.raises(ModelError):
            await runner.run(AdaptiveReview, prompt("照合", data), stage="review", schema_retries=0)
        diagnostic = runner.metadata["call_diagnostics"][0]
        assert diagnostic["finish_reason"] == "unknown" and secret not in json.dumps(diagnostic)
        assert diagnostic["input_tokens"] == 123 and diagnostic["output_tokens"] == 67
    finally:
        await runner.aclose()
    unknown = type(secret, (RuntimeError,), {})(secret)
    error = ValueError(secret)
    error.__cause__, unknown.__cause__ = unknown, error
    diagnostic = _schema_diagnostic(error, AdaptiveReviewWire.model_json_schema())
    assert diagnostic == {"error_classes": ["ValueError", "other"], "validation": []}
    assert secret not in json.dumps(diagnostic)


@pytest.mark.parametrize("role", ["limitations", "fallback"])
def test_duplicate_refill_uses_actual_query_provenance_then_general_relevance(tmp_path, role):
    from ephy_worker.evidence import select_passages

    runner = duplicate_body_runner(tmp_path)
    if role == "limitations":
        runner.report.queries = [{"query_id": "q-limit", "text": "zephyr spectrum", "role": "limitations"}]
    else:
        runner.report.queries = []
    original = [s.model_dump() for s in runner.report.sources]
    ranked = select_passages(runner.report.sources, "lookup", 24)
    selected = runner.adaptive_passages(ranked, "lookup")
    event = next(e for e in passage_events(runner) if e["type"] == "passage_selection")
    assert len(selected) == 4 and len({p.source_id for p in selected}) == 3
    assert event["selection"][-1]["role"] == ("limitations" if role == "limitations" else None)
    assert event["selection"][-1]["reason"] == ("query_role_relevance" if role == "limitations" else "general_relevance")
    assert [s.model_dump() for s in runner.report.sources] == original


@pytest.mark.asyncio
async def test_compact_review_keeps_previous_checked_claim_and_evidence_semantics(tmp_path):
    runner = executor(tmp_path)
    reviews, original = [], runner.model.run
    async def record(output, prompt, *, stage, **bounds):
        if stage == "review": reviews.append(json.loads(prompt.split("INPUT_DATA_JSON:\n")[1]))
        return await original(output, prompt, stage=stage, **bounds)
    runner.model.run = record
    report = await runner.run("公開条件の比較")
    assert len(reviews) == 3
    assert reviews[1]["previous_claims_reference"] == [report.claims[0].model_dump()]
    assert reviews[1]["previous_evidence_reference"] == [report.evidence[0].model_dump()]
    assert reviews[1]["previous_claims_reference"][0]["checked"]
    assert reviews[1]["previous_evidence_reference"][0]["is_primary"]
    assert reviews[1]["previous_gaps"] and reviews[1]["origin_groups"]
