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
