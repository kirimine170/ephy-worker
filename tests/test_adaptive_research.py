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
