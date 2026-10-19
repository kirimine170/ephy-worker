"""Canonical provenance，namespace，time and filesystem rejection controls．"""

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ephy_worker.answer_cache import AnswerCache, positive_facts, validate_snapshot
from ephy_worker.config import AnswerPolicy
from ephy_worker.schema import Claim, Evidence, Passage, Report, Source

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def canonical():
    quote = "Lists are mutable sequences. Tuples are immutable sequences."
    created, fetched, finished = [(NOW - timedelta(seconds=s)).isoformat() for s in (20, 15, 10)]
    return Report(job_id="research-original", question="公開資料の出典付き比較", profile_id="fixture",
        created_at=created, finished_at=finished, state="completed", answerability="sufficient",
        stop_reason="verified_coverage_complete", subquestions=["変更可能性"],
        sources=[Source(source_id="S1", requested_url="https://docs.python.org/3/library/stdtypes.html",
            final_url="https://docs.python.org/3/library/stdtypes.html", fetched_at=fetched, kind="html",
            status="ok", http_status=200, body_hash=hashlib.sha256(quote.encode()).hexdigest(),
            extractor="fixture-extractor", bytes_received=3000, limitations=["引用箇所だけを読取"],
            read_passage_ids=["S1-P1"], passages=[Passage(passage_id="S1-P1", source_id="S1",
                text=quote, start=10, end=10 + len(quote))])],
        evidence=[Evidence(evidence_id="R0-e001", source_id="S1", passage_id="S1-P1", quote=quote,
            quote_start=10, quote_end=10 + len(quote), relation="supports", checked=True,
            conditions_match=True, is_primary=True, check_reason="公式の仕様．", conditions="標準型")],
        claims=[Claim(claim_id="R0-c001", text="listは変更可能でtupleは変更できません．",
            evidence_ids=["R0-e001"], checked=True, status="supported_primary", reason="公式本文．",
            conditions="標準型", uncertainty=["派生クラスは対象外"] )],
        metrics={"adaptive_coverage": {"0": ["R0-c001"]}},
        rounds=[{"round": 0, "coverage": {"0": ["R0-c001"]}}])


def test_restart_hit_preserves_original_and_conditions(tmp_path):
    policy = AnswerPolicy()
    report = canonical()
    original = report.model_dump_json()
    cache = AnswerCache(tmp_path, policy)
    assert cache.put(report, "scope1", now=NOW.isoformat()) == "stored"
    loaded, code = AnswerCache(tmp_path, policy).lookup(report.question, "scope1", now=NOW.isoformat())
    assert code == "hit" and loaded.job_id == report.job_id
    assert loaded.sources[0].fetched_at == report.sources[0].fetched_at
    facts, reason = positive_facts(loaded)
    assert reason == "ok" and facts[0].conditions == "標準型" and facts[0].uncertainty == ("派生クラスは対象外",)
    assert report.model_dump_json() == original
    assert cache.lookup(report.question, "scope2", now=NOW.isoformat())[1] == "miss"


@pytest.mark.parametrize("fault", ["flag", "span", "quote", "read", "owner", "primary", "http", "hash", "extractor",
    "bytes", "url", "duplicate", "page", "conditions", "gap", "partial", "failure", "coverage", "future", "oldsource"])
def test_flags_and_rehashed_snapshots_cannot_bypass_provenance(tmp_path, fault):
    report = canonical()
    source, evidence = report.sources[0], report.evidence[0]
    if fault == "flag": evidence.checked = False
    elif fault == "span": evidence.quote_start += 1
    elif fault == "quote": evidence.quote = "forged"
    elif fault == "read": source.read_passage_ids = []
    elif fault == "owner": source.passages[0].source_id = "other"
    elif fault == "primary": evidence.is_primary = False
    elif fault == "http": source.http_status = 500
    elif fault == "hash": source.body_hash = "forged"
    elif fault == "extractor": source.extractor = None
    elif fault == "bytes": source.bytes_received = 0
    elif fault == "url": source.requested_url = "http://127.0.0.1/private"
    elif fault == "duplicate": report.claims.append(report.claims[0].model_copy())
    elif fault == "page": source.passages[0].page = 1
    elif fault == "conditions": evidence.conditions_match = False
    elif fault == "gap": report.gaps = ["未解決"]
    elif fault == "partial": report.state = "partial"
    elif fault == "failure": report.failures = [{"code": "failure"}]
    elif fault == "coverage": report.metrics["adaptive_coverage"] = {"0": ["unknown"]}
    elif fault == "future": source.fetched_at = (NOW + timedelta(seconds=5)).isoformat()
    elif fault == "oldsource": source.fetched_at = (NOW - timedelta(days=2)).isoformat()
    cache = AnswerCache(tmp_path, AnswerPolicy())
    valid = canonical()
    assert cache.put(valid, "scope", now=NOW.isoformat()) == "stored"
    snapshot = report.model_dump_json()
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE answers SET snapshot=?,digest=?", (snapshot, hashlib.sha256(snapshot.encode()).hexdigest()))
    assert cache.lookup(report.question, "scope", now=NOW.isoformat())[0] is None


def test_stale_future_and_short_freshness_ttl(tmp_path):
    cache = AnswerCache(tmp_path, AnswerPolicy(freshness_ttl_seconds=30))
    report = canonical()
    assert cache.put(report, "scope", now=NOW.isoformat()) == "stored"
    assert cache.lookup(report.question, "scope", now=(NOW + timedelta(seconds=25)).isoformat(), freshness=True)[0] is None
    assert cache.lookup(report.question, "scope", now=(NOW - timedelta(seconds=1)).isoformat())[1] == "invalid_report_time"
    assert cache.lookup(report.question, "scope", now=(NOW + timedelta(days=2)).isoformat())[1] == "expired"


def test_tamper_schema_hardlink_path_and_size(tmp_path):
    cache = AnswerCache(tmp_path / "state", AnswerPolicy(cache_max_bytes=65536))
    report = canonical()
    assert cache.put(report, "scope", now=NOW.isoformat()) == "stored"
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE answers SET snapshot='{}'")
    assert cache.lookup(report.question, "scope", now=NOW.isoformat())[1] == "snapshot_tampered"
    with sqlite3.connect(cache.path) as db:
        db.execute("CREATE TABLE unexpected (x TEXT)")
    with pytest.raises(ValueError, match="schema"):
        cache.lookup(report.question, "scope")
    os.link(cache.path, tmp_path / "hardlink")
    with pytest.raises(ValueError, match="unsafe"):
        cache.lookup(report.question, "scope")
    with pytest.raises(ValueError, match="Git"):
        AnswerCache(__import__("pathlib").Path(__file__).parent, AnswerPolicy())
    oversized = tmp_path / "oversize"
    oversized.mkdir()
    (oversized / cache.path.name).write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="unsafe"):
        AnswerCache(oversized, AnswerPolicy(cache_max_bytes=65536))


def test_positive_cache_is_bounded_and_no_knowledge(tmp_path):
    cache = AnswerCache(tmp_path, AnswerPolicy(cache_max_entries=2))
    for i in range(4):
        report = canonical()
        report.question += str(i)
        assert cache.put(report, "scope", now=NOW.isoformat()) == "stored"
    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT count(*) FROM answers").fetchone()[0] == 2
    report = canonical()
    report.claims = []
    assert cache.put(report, "scope", now=NOW.isoformat()) != "stored"
    assert validate_snapshot(canonical(), now=NOW.isoformat(), as_of=NOW.isoformat(), ttl=10) != "ok"
    assert json.loads(canonical().model_dump_json())["sources"][0]["fetched_at"]


def test_snapshot_bytecap_secret_and_pdf_page_conditions(tmp_path):
    report = canonical()
    report.sources[0].limitations.append("公開資料の範囲" * 4000)
    cache = AnswerCache(tmp_path / "bytes", AnswerPolicy(snapshot_max_bytes=4096))
    assert cache.put(report, "scope", now=NOW.isoformat()) == "snapshot_size_limit"
    report = canonical()
    report.sources[0].limitations.append("fixture-known-private-value")
    cache = AnswerCache(tmp_path / "secret", AnswerPolicy(), known_secrets=("fixture-known-private-value",))
    assert cache.put(report, "scope", now=NOW.isoformat()) == "secret_snapshot"
    report = canonical()
    report.sources[0].kind = "pdf"
    report.sources[0].page_count = 3
    report.sources[0].passages[0].page = report.evidence[0].page = 2
    assert positive_facts(report)[1] == "ok"
    report.sources[0].page_count = 1
    assert positive_facts(report)[0] == []


def unused_acquisition_report():
    report = canonical()
    report.sources.append(Source(source_id="S2", requested_url="https://example.org/unavailable",
        final_url="https://example.org/unavailable", fetched_at=(NOW - timedelta(seconds=12)).isoformat(),
        kind="html", status="extraction_failed", error="html_body_unavailable", http_status=200,
        extractor="fixture-extractor", bytes_received=0))
    report.failures = [{"stage": "fetch", "code": "S2:extraction_failed"}]
    report.rounds[-1].update(claim_ids=[c.claim_id for c in report.claims], remaining_gaps=[],
        conflicting_claim_ids=[], failure_count=1)
    return report


def resolve_unused(report):
    from ephy_worker.answer_cache import build_acquisition_resolutions

    report.acquisition_resolutions = build_acquisition_resolutions(
        report, resolved_at=(NOW - timedelta(seconds=11)).isoformat())
    return report


def test_unused_html_failure_keeps_positive_facts_but_needs_code_owned_resolution(tmp_path):
    report = unused_acquisition_report()
    original = report.model_dump_json()
    facts, reason = positive_facts(report)
    assert reason == "ok" and [f.claim_id for f in facts] == ["R0-c001"]
    assert report.model_dump_json() == original
    cache = AnswerCache(tmp_path, AnswerPolicy())
    assert cache.put(report, "scope", now=NOW.isoformat()) == "unresolved_failure"
    resolve_unused(report)
    assert len(report.acquisition_resolutions) == 1
    record = report.acquisition_resolutions[0]
    assert record.failure_index == 0 and record.source_id == "S2"
    assert record.stage == "fetch" and record.code == "S2:extraction_failed"
    assert record.supporting_claim_ids == ["R0-c001"] and record.round_number == 0
    assert record.source_sha256 == hashlib.sha256(report.sources[1].model_dump_json().encode()).hexdigest()
    assert cache.put(report, "scope", now=NOW.isoformat()) == "stored"
    loaded, code = AnswerCache(tmp_path, AnswerPolicy()).lookup(report.question, "scope", now=NOW.isoformat())
    assert code == "hit" and loaded.failures == report.failures
    assert loaded.sources[1].model_dump() == report.sources[1].model_dump()
    assert loaded.rounds == report.rounds and loaded.acquisition_resolutions == report.acquisition_resolutions
    assert loaded.metrics["answer_cache_projection"] == {"version": 2, "retained_failure_sources": ["S2"]}
    assert json.loads(original)["failures"] == report.failures
    assert loaded.sources[0].fetched_at == report.sources[0].fetched_at


@pytest.mark.parametrize("fault", ["failure_index", "code", "source_id", "source_sha256", "supporting_claim_ids",
    "round_number", "duplicate", "missing", "future", "before_fetch", "missing_history", "history_coverage",
    "history_failure_count", "history_claim_ids", "changed_source", "added_failure", "removed_failure", "orphan"])
def test_rehashed_resolution_forgery_is_rejected_on_restart(tmp_path, fault):
    report = resolve_unused(unused_acquisition_report())
    cache = AnswerCache(tmp_path, AnswerPolicy())
    assert cache.put(report, "scope", now=NOW.isoformat()) == "stored"
    record = report.acquisition_resolutions[0]
    if fault == "failure_index": record.failure_index = 1
    elif fault == "code": record.code = "S1:extraction_failed"
    elif fault == "source_id": record.source_id = "S1"
    elif fault == "source_sha256": record.source_sha256 = "0" * 64
    elif fault == "supporting_claim_ids": record.supporting_claim_ids = ["forged"]
    elif fault == "round_number": record.round_number = 1
    elif fault == "duplicate": report.acquisition_resolutions.append(record.model_copy())
    elif fault == "missing": report.acquisition_resolutions = []
    elif fault == "future": record.resolved_at = (NOW + timedelta(seconds=1)).isoformat()
    elif fault == "before_fetch": record.resolved_at = report.created_at
    elif fault == "missing_history": report.rounds = []
    elif fault == "history_coverage": report.rounds[-1]["coverage"] = {"0": ["forged"]}
    elif fault == "history_failure_count": report.rounds[-1]["failure_count"] = 0
    elif fault == "history_claim_ids": report.rounds[-1]["claim_ids"] = []
    elif fault == "changed_source": report.sources[1].limitations = ["changed after decision"]
    elif fault == "added_failure": report.failures.append({"stage": "cleanup", "code": "RuntimeError"})
    elif fault == "removed_failure": report.failures = []
    elif fault == "orphan":
        report.evidence.append(report.evidence[0].model_copy(update={"evidence_id": "orphan-evidence"}))
    snapshot = report.model_dump_json()
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE answers SET snapshot=?,digest=?", (snapshot, hashlib.sha256(snapshot.encode()).hexdigest()))
    assert AnswerCache(tmp_path, AnswerPolicy()).lookup(report.question, "scope", now=NOW.isoformat())[0] is None


@pytest.mark.parametrize("fault", ["supplied", "read", "passages", "cited", "parser_error", "parser_exit",
    "parser_monitor", "blocked", "http", "network", "timeout", "budget", "search", "model", "schema",
    "review", "evidence", "coverage", "cleanup", "storage", "duplicate_failure", "missing_failure",
    "unsafe_url", "wrong_code", "extra_failure_field"])
def test_hard_or_required_acquisition_never_gets_resolved(tmp_path, fault):
    from ephy_worker.answer_cache import build_acquisition_resolutions

    report = unused_acquisition_report()
    source = report.sources[1]
    if fault == "supplied": source.supplied = True
    elif fault == "read": source.read_passage_ids = ["unread-forged"]
    elif fault == "passages": source.passages = [report.sources[0].passages[0].model_copy()]
    elif fault == "cited":
        report.evidence.append(report.evidence[0].model_copy(update={"evidence_id": "unused-e", "source_id": "S2"}))
    elif fault == "parser_error": source.error = "parser_exception"
    elif fault == "parser_exit": source.error = "parser_exit:1"
    elif fault == "parser_monitor": source.error = "parser_monitor_failed"
    elif fault == "blocked": source.status, source.error = "blocked", "unsafe_url"
    elif fault == "http": source.http_status = 503
    elif fault == "network": source.status, source.error = "network_error", "network_error"
    elif fault == "timeout": source.status, source.error = "timeout", "parser_timeout"
    elif fault == "budget": report.failures.append({"stage": "fetch", "code": "job_timeout"})
    elif fault in {"search", "model", "schema", "review", "evidence", "coverage", "cleanup", "storage"}:
        report.failures.append({"stage": fault, "code": "hard_failure"})
    elif fault == "duplicate_failure": report.failures.append(dict(report.failures[0]))
    elif fault == "missing_failure": report.failures = []
    elif fault == "unsafe_url": source.requested_url = "http://127.0.0.1/private"
    elif fault == "wrong_code": report.failures[0]["code"] = "S9:extraction_failed"
    elif fault == "extra_failure_field": report.failures[0]["resolved"] = True
    assert build_acquisition_resolutions(report) == []
    assert AnswerCache(tmp_path, AnswerPolicy()).put(report, "scope", now=NOW.isoformat()) != "stored"
    if fault not in {"passages", "cited"}:
        assert [f.claim_id for f in positive_facts(report)[0]] == ["R0-c001"]


def test_deep_partial_facts_validate_each_actual_cited_dependency(tmp_path):
    report = unused_acquisition_report()
    broken = report.claims[0].model_copy(deep=True, update={"claim_id": "R1-c001", "evidence_ids": ["R1-e001"]})
    report.claims.append(broken)
    report.evidence.append(report.evidence[0].model_copy(update={"evidence_id": "R1-e001", "source_id": "S2"}))
    report.state, report.answerability = "partial", "limited"
    original = report.model_dump_json()
    facts, reason = positive_facts(report)
    assert reason == "partial_positive_facts" and [f.claim_id for f in facts] == ["R0-c001"]
    assert facts[0].conditions == "標準型" and facts[0].uncertainty == ("派生クラスは対象外",)
    assert report.model_dump_json() == original
    assert AnswerCache(tmp_path, AnswerPolicy()).put(report, "scope", now=NOW.isoformat()) == "incomplete_report"
    # One real quote cannot conceal another broken dependency on the same claim．
    report.claims[0].evidence_ids.append("extra-e")
    report.evidence.append(report.evidence[0].model_copy(update={"evidence_id": "extra-e", "quote_start": 11}))
    assert positive_facts(report)[0] == []


@pytest.mark.parametrize("fault", ["gap", "claim", "coverage", "extra_coverage", "contradiction", "cancelled"])
def test_resolution_requires_all_claims_every_main_question_and_no_gap(tmp_path, fault):
    from ephy_worker.answer_cache import build_acquisition_resolutions

    report = unused_acquisition_report()
    if fault == "gap": report.gaps = ["unresolved"]
    elif fault == "claim": report.claims.append(Claim(claim_id="unchecked", text="未照合"))
    elif fault == "coverage": report.metrics["adaptive_coverage"] = {"0": ["forged"]}
    elif fault == "extra_coverage": report.metrics["adaptive_coverage"]["1"] = ["R0-c001"]
    elif fault == "contradiction": report.evidence[0].relation = "contradicts"
    elif fault == "cancelled": report.state = "cancelled"
    assert build_acquisition_resolutions(report) == []
    assert AnswerCache(tmp_path, AnswerPolicy()).put(report, "scope", now=NOW.isoformat()) != "stored"


def test_acquisition_resolution_is_not_a_model_review_field():
    from pydantic import ValidationError

    from ephy_worker.schema import AdaptiveReview

    report = resolve_unused(unused_acquisition_report())
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AdaptiveReview.model_validate({"checks": [], "acquisition_resolutions":
                                       [report.acquisition_resolutions[0].model_dump()]})
