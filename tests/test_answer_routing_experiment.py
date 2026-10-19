"""Offline fixture replay and mocked physical model requests．No live services or user data．"""

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from ephy_worker import answer_routing_experiment as module
from ephy_worker.models import ModelRunner
from ephy_worker.schema import Claim, OriginGroup

spec = importlib.util.spec_from_file_location(
    "answer_routing_benchmark", Path(__file__).parents[1] / "scripts" / "benchmark_answer_routing.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def optimistic():
    return module.RouteProposal(route="knowledge", kind="stable_explanation", confidence="high",
                                draft=module.KnowledgeDraft(text="一般的な仕組みを説明します．"))


def test_knowledge_attribution_is_unverified_and_cannot_be_promoted():
    decision = module.choose_route("DNSとは何ですか．", optimistic(), module.EvidenceCache(), now=benchmark.NOW)
    assert decision.route == "knowledge" and decision.origin == "model_knowledge"
    assert not decision.draft.externally_verified and decision.fact_ids == ()
    for updates in ({"origin": "source_verified"}, {"externally_verified": True}, {"reasoning": "hidden"}):
        with pytest.raises(ValidationError):
            module.KnowledgeDraft.model_validate({"text": "公開説明．", **updates})


def test_explicitly_uncertain_proposal_requires_evidence_even_for_general_explanation():
    proposal = optimistic().model_copy(update={"confidence": "low"})
    decision = module.choose_route("DNSとは何ですか．", proposal, module.EvidenceCache(), now=benchmark.NOW)
    assert decision.route == "research" and decision.guard == "proposal_uncertain" and decision.draft is None


@pytest.mark.parametrize("question,guard", [
    ("現在のPythonの最新安定版は何ですか．", "freshness_required"),
    ("What is the latest stable Python version today?", "freshness_required"),
    ("DNSの仕組みを一次資料の出典付きで説明してください．", "sources_required"),
    ("この薬と別の薬を一緒に飲んでも安全ですか．", "high_stakes"),
    ("今日の製品Sの価格はいくらですか．", "freshness_required"),
    ("製品アオミズXの型番AZ-731限定機能は何ですか．", "specific_unverified"),
    ("対象の動作を断定してください．", "uncertain_specific"),
])
def test_deterministic_guards_override_confident_knowledge(question, guard):
    decision = module.choose_route(question, optimistic(), module.EvidenceCache(), now=benchmark.NOW)
    assert decision.route == "research" and decision.guard == guard and decision.draft is None


@pytest.mark.parametrize("text", [
    "<think>internal</think>", "&lt;analysis&gt;internal", "[analysis] internal", "https://example.org/",
    "C:\\private\\data", "../private", "password=private-fixture", "sk-" + "x" * 24, "\ud800", "\x01",
])
def test_unsafe_knowledge_text_is_rejected(text):
    with pytest.raises(ValidationError):
        module.KnowledgeDraft(text=text)


@pytest.mark.parametrize("name", [
    "fresh_verified", "expired", "future", "question_mismatch", "policy_mismatch", "no_verified_facts",
    "snippets_only", "tampered_span", "duplicate_ids", "conflicting", "gaps", "failed_report", "bad_timestamp",
])
def test_fixed_cache_controls_and_exact_question(name):
    cache, question = benchmark.cache_fixture(name)
    before = cache.entries[question]
    decision = module.choose_route(question, optimistic(), cache, now=benchmark.NOW)
    assert decision.route == ("cache" if name == "fresh_verified" else "research")
    assert (decision.cache_code == "hit") == (name == "fresh_verified")
    assert cache.entries[question] == before
    assert cache.lookup(question + " ", now=benchmark.NOW) == ([], "miss")


def test_cache_revalidation_never_mutates_canonical_report_and_snapshot():
    report = benchmark.public_report()
    original = report.model_dump_json()
    cache = module.EvidenceCache()
    cache.put(report, as_of=benchmark.AS_OF, ttl_seconds=3600)
    assert cache.lookup(report.question, now=benchmark.NOW)[1] == "hit"
    assert report.model_dump_json() == original and cache.entries[report.question].report_json == original
    report.claims[0].checked = False
    assert cache.lookup(report.question, now=benchmark.NOW)[1] == "hit"
    entry = cache.entries[report.question]
    cache.entries[report.question] = replace(entry, report_json=entry.report_json + " ")
    assert cache.lookup(report.question, now=benchmark.NOW) == ([], "snapshot_tampered")


@pytest.mark.parametrize("field,value", [
    ("kind", "unknown"), ("http_status", None), ("body_hash", None), ("extractor", None),
    ("bytes_received", 0), ("fetched_at", "2026-10-19T00:00:00+00:00"),
    ("fetched_at", "2020-01-01T00:00:00+00:00"), ("fetched_at", "bad-time"),
    ("requested_url", "http://127.0.0.1/private"),
    ("requested_url", "https://name:password@example.org/"),
    ("final_url", "https://example.org/?password=private-fixture"),
    ("requested_url", "http://intranet/private"),
])
def test_unknown_or_unsafe_source_provenance_is_a_recorded_miss(field, value):
    report = benchmark.public_report()
    setattr(report.sources[0], field, value)
    cache = module.EvidenceCache()
    cache.put(report, as_of=benchmark.AS_OF, ttl_seconds=3600)
    facts, code = cache.lookup(report.question, now=benchmark.NOW)
    assert not facts and code not in {"hit", "miss"}


def test_primary_and_independent_origins_are_recomputed():
    report = benchmark.public_report(2)
    report.claims = [report.claims[0]]
    report.claims[0].evidence_ids = ["e1", "e2"]
    report.claims[0].status = "corroborated"
    for evidence in report.evidence:
        evidence.is_primary = False
    assert module.positive_facts(report)[0] == []
    report.origin_groups = [OriginGroup(group_id=f"o{i}", source_ids=[f"s{i}"],
                                       reasons=["合成独立起点の確認fixture．"], independence="confirmed")
                            for i in (1, 2)]
    original = report.model_dump_json()
    assert [f.claim_id for f in module.positive_facts(report)[0]] == ["c1"]
    assert report.model_dump_json() == original
    report.origin_groups[1].independence = "unknown"
    assert module.positive_facts(report)[0] == []


@pytest.mark.parametrize("change", ["ttl", "as_of", "report_time", "provenance", "conditions", "read", "span"])
def test_additional_cache_negative_controls(change):
    report = benchmark.public_report()
    if change == "report_time":
        report.finished_at = "2026-10-19T00:00:00+00:00"
    elif change == "conditions":
        report.evidence[0].conditions_match = False
    elif change == "read":
        report.sources[0].read_passage_ids = []
    elif change == "span":
        report.sources[0].passages[0].end += 1
    cache = module.EvidenceCache()
    cache.put(report, as_of=benchmark.AS_OF, ttl_seconds=3600)
    entry = cache.entries[report.question]
    changes = {"ttl": {"ttl_seconds": float("nan")}, "as_of": {"as_of": "2026-10-18"},
               "provenance": {"provenance": "unknown"}}
    cache.entries[report.question] = replace(entry, **changes.get(change, {}))
    assert cache.lookup(report.question, now=benchmark.NOW)[0] == []


@pytest.mark.parametrize("name", list(benchmark.COMPLETION_EXPECTED))
def test_completion_fixtures_use_positive_coverage_and_bounded_followups(name):
    questions, rounds, limits = benchmark.completion_fixture(name)
    before = [r.report.model_dump_json() for r in rounds]
    result = module.replay_research(questions, rounds, limits=limits)
    assert (result.state, result.reason, result.rounds) == benchmark.COMPLETION_EXPECTED[name]
    assert [r.report.model_dump_json() for r in rounds] == before
    if name == "gap_then_sufficient_after_three_rounds":
        assert result.followups == 2 and result.covered == 3


def test_early_sufficiency_cancel_error_and_caps_are_distinct():
    questions, rounds, _ = benchmark.completion_fixture("sufficient_early")
    error = benchmark.public_report()
    error.failures = [{"code": "fixture_error"}]
    bad_round = module.ReplayRound(error, {})
    assert module.replay_research(questions, rounds + [bad_round]).rounds == 1
    assert module.replay_research(questions, [bad_round]).reason == "report_error"
    assert module.replay_research(questions, [bad_round], cancelled=True).state == "cancelled"
    exhausted = module.replay_research(questions, [replace(rounds[0], model_calls=7)])
    assert exhausted.state == "partial" and exhausted.reason == "model_limit"
    assert module.replay_research(questions, []).reason == "replay_exhausted"
    with pytest.raises(ValidationError):
        module.ReplayLimits(rounds=9)
    with pytest.raises(ValidationError):
        module.ReplayLimits(model_calls=7)
    with pytest.raises(ValueError):
        module.replay_research(("same", "same"), [])


def test_full_coverage_with_remaining_gap_or_conflict_cannot_be_sufficient():
    questions, rounds, _ = benchmark.completion_fixture("sufficient_early")
    rounds[0].report.gaps = ["未解決の主要論点．"]
    assert module.replay_research(questions, rounds).state == "partial"
    rounds[0].report.gaps = []
    rounds[0].report.claims.append(Claim(claim_id="conflict", text="未解決の対立．", status="conflicting"))
    assert module.replay_research(questions, rounds).state == "partial"


def public_probe_cases():
    return {
        "routing_cases": [
            {"id": "stable_dns", "question": "DNSとは何ですか．", "probe_model": True,
             "expected_route": "knowledge", "answer_contains_any": ["ドメイン", "名前"]},
            {"id": "stable_cache", "question": "キャッシュとは何ですか．", "probe_model": True,
             "expected_route": "knowledge", "answer_contains_any": ["保存", "再利用"]},
            {"id": "unknown_specific", "question": "型番AZ-731限定機能は何ですか．", "probe_model": True,
             "expected_route": "research"},
        ],
        "evidence_probe": {"source_text": "標準版100gは税込1,000円です．業務版は別契約です．",
                           "claims": [{"id": "supported", "text": "標準版100gは税込1,000円です．", "expected": "supported"},
                                      {"id": "wrong_number", "text": "標準版100gは税込500円です．", "expected": "contradicted"},
                                      {"id": "wrong_condition", "text": "業務版100gは税込1,000円です．", "expected": "not_established"}]},
    }


def install_mock_transport(monkeypatch, *, unsafe=False, wrong_model=False):
    observed = {"requests": [], "profiles": [], "active": 0, "maximum": 0}
    answers = ["DNSはドメインの名前を対応付けます．", "キャッシュは保存した情報を再利用します．", "資料が必要です．"]
    async def respond(request):
        observed["active"] += 1
        observed["maximum"] = max(observed["maximum"], observed["active"])
        await asyncio.sleep(0)
        observed["requests"].append(request)
        observed["active"] -= 1
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": module.MODEL_ID, "object": "model"}]})
        index = len(observed["requests"]) - 2
        if index < 3:
            typed = optimistic().model_dump()
            typed["draft"]["text"] = "<think>private model text</think>" if unsafe else answers[index]
        else:
            typed = {"labels": ["supported", "contradicted", "not_established"]}
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "wrong-model" if wrong_model else module.MODEL_ID,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(typed)}}],
        })
    def factory(profile, budget):
        observed["profiles"].append(profile)
        return ModelRunner(profile, budget, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(module, "ModelRunner", factory)
    return observed


async def test_local_probe_reuses_runner_serial_bounds_and_keeps_gold_evaluator_side(monkeypatch):
    observed = install_mock_transport(monkeypatch)
    cases = public_probe_cases()
    result = await module.local_probe(cases, base_url=module.BASE_URL, model_id=module.MODEL_ID)
    assert result["code"] == "finished" and result["model_requests"] == 5
    assert observed["maximum"] == 1 and len(observed["requests"]) == 5
    profile = observed["profiles"][0]
    assert profile.schema_retries == 0 and profile.max_output_tokens == 512 and profile.context_tokens == 16384
    assert profile.timeout_seconds == 45 and profile.max_tokens_parameter == "max_tokens"
    for request in observed["requests"][1:]:
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 512 and payload["model"] == module.MODEL_ID
        assert "max_completion_tokens" not in payload
        prompt = json.dumps(payload["messages"])
        for forbidden in ("expected_route", "answer_contains_any", '"expected"', "wrong_number", "wrong_condition", "stable_dns"):
            assert forbidden not in prompt
    evaluated = benchmark.evaluate_probe(cases, result)
    assert evaluated["execution_ok"] and evaluated["passed"] == 8 and evaluated["failed"] == 0
    result["rows"][-1]["typed"]["labels"][1] = "supported"
    assert benchmark.evaluate_probe(cases, result)["failed"] == 1


@pytest.mark.parametrize("unsafe,wrong_model,code", [(True, False, "typed_probe_failed"),
                                                    (False, True, "response_model_mismatch")])
async def test_probe_failures_never_persist_unsafe_output_and_never_retry(monkeypatch, unsafe, wrong_model, code):
    observed = install_mock_transport(monkeypatch, unsafe=unsafe, wrong_model=wrong_model)
    result = await module.local_probe(public_probe_cases(), base_url=module.BASE_URL, model_id=module.MODEL_ID)
    assert result["rows"][0]["code"] == code and "typed" not in result["rows"][0]
    assert len(observed["requests"]) == 2 and result["model_requests"] == 2
    assert "private model text" not in json.dumps(result)
    evaluated = benchmark.evaluate_probe(public_probe_cases(), result)
    assert not evaluated["execution_ok"] and evaluated["failed"] > 0


async def test_probe_endpoint_and_model_rejected_before_runner(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("runner must not be constructed")
    monkeypatch.setattr(module, "ModelRunner", forbidden)
    for url, model_id in [("https://example.org/v1", module.MODEL_ID),
                          ("http://127.0.0.1:9999/v1", module.MODEL_ID), (module.BASE_URL, "other")]:
        with pytest.raises(ValueError, match="identity_rejected"):
            await module.local_probe(public_probe_cases(), base_url=url, model_id=model_id)


@pytest.mark.parametrize("error", [TimeoutError, RuntimeError, asyncio.CancelledError])
async def test_probe_error_timeout_and_cancellation_always_close_without_raw_details(monkeypatch, error):
    closed = []
    class Runner:
        def __init__(self, profile, budget):
            self.budget = budget
        async def available(self):
            self.budget.take("model_requests")
            return True
        async def run(self, *args, **kwargs):
            self.budget.take("model_requests")
            raise error("private detail must not be saved")
        async def aclose(self):
            closed.append(True)
    monkeypatch.setattr(module, "ModelRunner", Runner)
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await module.local_probe(public_probe_cases(), base_url=module.BASE_URL, model_id=module.MODEL_ID)
    else:
        result = await module.local_probe(public_probe_cases(), base_url=module.BASE_URL, model_id=module.MODEL_ID)
        assert result["code"] == "probe_failed" and result["model_requests"] == 2
        assert "private detail" not in json.dumps(result)
    assert closed == [True]


def test_output_paths_and_case_identity_are_checked_before_any_probe(tmp_path, monkeypatch):
    existing = tmp_path / "existing.json"
    existing.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError):
        benchmark.safe_output_path(str(existing))
    assert existing.read_text("utf-8") == "preserve"
    with pytest.raises(ValueError):
        benchmark.safe_output_path("relative.json")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    with pytest.raises(ValueError):
        benchmark.safe_output_path(str(checkout / "out.json"))
    cases = tmp_path / "cases.json"
    cases.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(benchmark, "local_probe", lambda *a, **k: pytest.fail("no probe before identity"))
    output = tmp_path / "output.json"
    assert benchmark.main(["--cases", str(cases), "--output", str(output), "--live-model"]) == 2
    assert not output.exists()
