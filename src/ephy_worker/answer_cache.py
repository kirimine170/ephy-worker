"""Bounded canonical evidence snapshots，separate from search previews．"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from pydantic import ValidationError

from .discord_adapter import redact_secrets
from .discord_answer import verified_facts
from .evidence import apply_review, group_origins
from .fetch import validate_public_url
from .report import _evidence_is_real
from .schema import (
    AcquisitionResolution,
    ClaimCheck,
    CoverageAssessment,
    EvidenceCheck,
    Report,
    Review,
    utc_now,
)
from .store import output_root
from .tavily_keyless import _result_url

VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed


def _safe_source_urls(source, known_secrets):
    for url in [source.requested_url, source.final_url or source.requested_url, *source.origin_urls]:
        try:
            validate_public_url(url)
            if (not _result_url(url) or redact_secrets(url, known_secrets) != url
                    or redact_secrets(unquote(unquote(url)), known_secrets) != unquote(unquote(url))):
                return "unsafe_source_url"
        except ValueError:
            return "unsafe_source_url"
    return "ok"


def _source_problem(source, known_secrets):
    if (source.kind not in {"html", "pdf"} or source.status != "ok"
                or type(source.http_status) is not int or not 200 <= source.http_status < 300
                or not source.body_hash or not re.fullmatch(r"[a-f0-9]{64}", source.body_hash)
                or not source.extractor or source.bytes_received <= 0 or source.error
                or len(source.read_passage_ids) != len(set(source.read_passage_ids))):
        return "invalid_source_provenance"
    passage_ids = {p.passage_id for p in source.passages}
    if not set(source.read_passage_ids) <= passage_ids:
        return "invalid_read_range"
    for passage in source.passages:
        if (passage.source_id != source.source_id or passage.start < 0 or passage.end <= passage.start
                    or passage.end - passage.start != len(passage.text)
                    or source.kind == "html" and passage.page is not None
                    or source.kind == "pdf" and (type(passage.page) is not int or passage.page < 1
                        or type(source.page_count) is not int or passage.page > source.page_count)):
            return "invalid_passage"
    return _safe_source_urls(source, known_secrets)


def positive_facts(report: Report, known_secrets: tuple[str, ...] = ()):
    """Deeply recheck each cited dependency；retain independently valid partial facts．"""
    copied = report.model_copy(deep=True)
    if len(copied.claims) > 10:
        return [], "canonical_claim_count_limit"
    collections = [[s.source_id for s in copied.sources],
                   [p.passage_id for s in copied.sources for p in s.passages],
                   [e.evidence_id for e in copied.evidence], [c.claim_id for c in copied.claims]]
    if any(len(ids) != len(set(ids)) or any(not _ID.fullmatch(key) for key in ids) for ids in collections):
        return [], "duplicate_or_invalid_ids"
    sources = {s.source_id: s for s in copied.sources}
    passage_counts = Counter(p.passage_id for s in copied.sources for p in s.passages)
    by_id = {e.evidence_id: e for e in copied.evidence}
    owners = Counter(key for c in copied.claims for key in c.evidence_ids)
    checks, valid_claims, valid_evidence, source_ids = [], [], [], set()
    rejection = None
    for claim in copied.claims:
        items = [by_id[key] for key in claim.evidence_ids if key in by_id]
        if (not claim.checked or claim.status not in {"supported_primary", "corroborated"}
                or len(items) != len(claim.evidence_ids) or not items
                or any(owners[e.evidence_id] != 1 or not e.checked or e.conditions_match is not True
                       or type(e.is_primary) is not bool or e.relation != "supports" for e in items)):
            rejection = rejection or "not_positive"
            continue
        problem = next(("invalid_source_provenance" if e.source_id not in sources
                        else _source_problem(sources[e.source_id], known_secrets)
                        for e in items if e.source_id not in sources
                        or _source_problem(sources[e.source_id], known_secrets) != "ok"), None)
        if problem:
            rejection = rejection or problem
            continue
        if any(not _evidence_is_real(e, sources, passage_counts) for e in items):
            rejection = rejection or "unverified_report"
            continue
        valid_claims.append(claim)
        valid_evidence.extend(items)
        source_ids.update(e.source_id for e in items)
        checks.append(ClaimCheck(claim_id=claim.claim_id, status=claim.status, reason=claim.reason,
            conditions=claim.conditions, uncertainty=list(claim.uncertainty), evidence_checks=[
                EvidenceCheck(evidence_id=e.evidence_id, relation=e.relation,
                    conditions_match=e.conditions_match, is_primary=e.is_primary,
                    reason=e.check_reason or "記録済みの照合結果．") for e in items]))
    # Recompute origin merging；a serialized 'confirmed' flag cannot establish independence．
    copied.sources = [s for s in copied.sources if s.source_id in source_ids]
    copied.evidence = valid_evidence
    copied.origin_groups = group_origins(copied.sources)
    copied.claims = apply_review(valid_claims, copied.evidence, Review(checks=checks), copied.origin_groups)
    facts, omitted = verified_facts(copied, known_secrets)
    if not facts:
        return [], rejection or "unverified_report"
    complete = not omitted and len(facts) == len(report.claims)
    return facts, "ok" if complete else "partial_positive_facts"


def _coverage_assessments_resolved(report: Report, positive: set[str]) -> bool:
    """Revalidate explicit unsupported history instead of trusting completion flags．"""
    uncovered, supported = {}, set()
    for record in report.rounds:
        if "coverage_assessments" not in record:
            continue  # Legacy canonical reports did not record assessment rows．
        rows = record["coverage_assessments"]
        if not isinstance(rows, list) or len(rows) > 8:
            return False
        try:
            assessments = [CoverageAssessment.model_validate(row, strict=True) for row in rows]
        except ValidationError:
            return False
        indexes = [row.subquestion for row in assessments]
        if len(indexes) != len(set(indexes)):
            return False
        for row in assessments:
            refs, index = row.claim_ids, row.subquestion
            if index >= len(report.subquestions) or len(refs) != len(set(refs)) or not set(refs) <= positive:
                return False
            if not refs:
                uncovered[index] = f"主確認項目{index + 1}は今回の読取範囲で未確認です：{report.subquestions[index]}"
                supported.discard(index)
            else:
                supported.add(index)
        for index in list(uncovered.keys() & supported):
            new_ids, resolutions = record.get("new_positive_claim_ids", []), record.get("resolutions", [])
            if (not isinstance(new_ids, list) or not all(isinstance(key, str) for key in new_ids)
                    or not isinstance(resolutions, list)):
                return False
            for resolution in resolutions:
                if not isinstance(resolution, dict) or resolution.get("gap") != uncovered[index]:
                    continue
                cited = resolution.get("claim_ids")
                if (isinstance(cited, list) and cited and all(isinstance(key, str) for key in cited)
                        and len(cited) == len(set(cited)) and set(cited) <= set(new_ids) & positive):
                    del uncovered[index]
                    break
    return not uncovered


def verified_main_coverage(report: Report, facts) -> bool:
    positive = {f.claim_id for f in facts}
    coverage = report.metrics.get("adaptive_coverage", {})
    return (bool(positive) and bool(report.subquestions) and isinstance(coverage, dict)
        and set(coverage) == {str(i) for i in range(len(report.subquestions))}
        and all(isinstance(refs, list) and refs and len(refs) == len(set(refs))
                and all(ref in positive for ref in refs) for refs in coverage.values())
        and _coverage_assessments_resolved(report, positive))


def complete_support(report, facts) -> bool:
    positive = {f.claim_id for f in facts}
    owners = Counter(key for c in report.claims for key in c.evidence_ids)
    return (bool(positive) and len(facts) == len(report.claims)
        and positive == {c.claim_id for c in report.claims} and not report.gaps
        and all(c.checked and c.status in {"supported_primary", "corroborated"} for c in report.claims)
        and set(owners) == {e.evidence_id for e in report.evidence}
        and all(count == 1 for count in owners.values())
        and not any(e.relation == "contradicts" for e in report.evidence)
        and verified_main_coverage(report, facts))


def _unused_html_failures(report, known_secrets):
    sources = {s.source_id: s for s in report.sources}
    if len(sources) != len(report.sources):
        return None
    eligible, seen = [], set()
    for index, failure in enumerate(report.failures):
        if set(failure) != {"stage", "code"} or failure.get("stage") != "fetch":
            return None
        code = failure.get("code")
        if not isinstance(code, str) or not code.endswith(":extraction_failed"):
            return None
        source_id = code.removesuffix(":extraction_failed")
        source = sources.get(source_id)
        if (source is None or source_id in seen or not _ID.fullmatch(source_id)
                or source.status != "extraction_failed" or source.error != "html_body_unavailable"
                or source.kind != "html" or source.supplied is not False
                or type(source.http_status) is not int or not 200 <= source.http_status < 300
                or not source.extractor or source.body_hash is not None or source.bytes_received != 0
                or source.passages or source.read_passage_ids
                or any(e.source_id == source_id for e in report.evidence)
                or _safe_source_urls(source, known_secrets) != "ok"):
            return None
        seen.add(source_id)
        eligible.append((index, code, source))
    # An unrecorded bad source cannot disappear during the cache projection．
    if any(s.source_id not in seen and _source_problem(s, known_secrets) != "ok" for s in report.sources):
        return None
    return eligible


def _resolution_round(report):
    if not report.rounds:
        return None
    record = report.rounds[-1]
    number = record.get("round")
    if (type(number) is not int or not 0 <= number <= 5
            or record.get("claim_ids") != [c.claim_id for c in report.claims]
            or record.get("coverage") != report.metrics.get("adaptive_coverage")
            or record.get("remaining_gaps") != [] or record.get("conflicting_claim_ids") != []
            or record.get("failure_count") != len(report.failures)):
        return None
    return number


def build_acquisition_resolutions(report: Report, *, resolved_at: str | None = None):
    """Issue records only after independent facts cover every immutable main question．"""
    facts, _ = positive_facts(report)
    eligible, number = _unused_html_failures(report, ()), _resolution_round(report)
    if (not report.failures or report.acquisition_resolutions or not complete_support(report, facts)
            or eligible is None or number is None or report.state == "cancelled"):
        return []
    return [AcquisitionResolution(failure_index=index, code=code, source_id=source.source_id,
        source_sha256=hashlib.sha256(source.model_dump_json().encode()).hexdigest(),
        supporting_claim_ids=sorted(f.claim_id for f in facts), round_number=number,
        resolved_at=resolved_at or utc_now()) for index, code, source in eligible]


def unresolved_failure_code(report: Report, facts, known_secrets: tuple[str, ...] = ()) -> str:
    """Recompute dependency decisions；neither serialized success nor model labels count．"""
    if not report.failures:
        if report.acquisition_resolutions:
            return "invalid_acquisition_resolution"
        return "unresolved_source_failure" if any(
            _source_problem(s, known_secrets) != "ok" for s in report.sources) else "ok"
    eligible, number = _unused_html_failures(report, known_secrets), _resolution_round(report)
    if (eligible is None or number is None or not complete_support(report, facts)
            or len(report.acquisition_resolutions) != len(eligible) or report.state == "cancelled"):
        return "unresolved_failure"
    for record, (index, code, source) in zip(report.acquisition_resolutions, eligible, strict=True):
        if (record.version != "unused-html-v1" or record.failure_index != index or record.stage != "fetch"
                or record.code != code or record.source_id != source.source_id
                or record.source_sha256 != hashlib.sha256(source.model_dump_json().encode()).hexdigest()
                or record.supporting_claim_ids != sorted(f.claim_id for f in facts)
                or record.round_number != number):
            return "invalid_acquisition_resolution"
        try:
            if not (timestamp(report.created_at) <= timestamp(source.fetched_at)
                    <= timestamp(record.resolved_at) <= timestamp(report.finished_at or utc_now())):
                return "invalid_acquisition_resolution_time"
        except (ValueError, TypeError, OverflowError):
            return "invalid_acquisition_resolution_time"
    return "ok"


def validate_snapshot(report: Report, *, now: str, as_of: str, ttl: float,
                      known_secrets: tuple[str, ...] = ()) -> str:
    if (report.state != "completed" or report.answerability != "sufficient"
            or report.gaps or not report.finished_at or not report.subquestions):
        return "incomplete_report"
    try:
        current, saved = timestamp(now), timestamp(as_of)
        created, finished = timestamp(report.created_at), timestamp(report.finished_at)
        if not created <= finished <= saved <= current:
            return "invalid_report_time"
        if not 0 < ttl <= 86400 or (current - saved).total_seconds() >= ttl:
            return "expired"
        if (current - finished).total_seconds() >= ttl:
            return "expired_report"
        for source in report.sources:
            fetched = timestamp(source.fetched_at)
            if not created <= fetched <= finished:
                return "invalid_source_time"
            if (current - fetched).total_seconds() >= ttl:
                return "expired_source"
    except (ValueError, TypeError, OverflowError):
        return "invalid_time"
    facts, reason = positive_facts(report, known_secrets)
    if reason != "ok" or len(facts) != len(report.claims):
        return reason
    if not complete_support(report, facts):
        return "missing_verified_coverage"
    reason = unresolved_failure_code(report, facts, known_secrets)
    if reason != "ok":
        return reason
    resolved_sources = {r.source_id for r in report.acquisition_resolutions}
    if any(s.source_id not in resolved_sources and _source_problem(s, known_secrets) != "ok" for s in report.sources):
        return "invalid_source_provenance"
    return "ok"


def projection(report: Report) -> Report:
    copied = report.model_copy(deep=True)
    used = {e.passage_id for e in copied.evidence}
    sources = {e.source_id for e in copied.evidence}
    retained_failures = {r.source_id for r in copied.acquisition_resolutions}
    copied.sources = [s for s in copied.sources if s.source_id in sources | retained_failures]
    for source in copied.sources:
        source.passages = [p for p in source.passages if p.passage_id in used]
        source.read_passage_ids = [p for p in source.read_passage_ids if p in used]
    copied.candidates = []
    copied.metrics["answer_cache_projection"] = {"version": 2, "retained_failure_sources": sorted(retained_failures)}
    return copied


class AnswerCache:
    """SQLite stores only validated，versioned canonical projections under private state．"""

    def __init__(self, state_dir: Path, policy, *, known_secrets: tuple[str, ...] = ()):
        unresolved = Path(state_dir).absolute()
        for component in (unresolved, *unresolved.parents):
            if component.is_symlink() or component.is_junction():
                raise ValueError("unsafe_answer_cache_path")
        self.root = output_root(unresolved)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "verified-answers-v1.sqlite3"
        self.policy, self.known_secrets = policy, known_secrets
        self._safe_paths()

    def _safe_paths(self):
        for parent in (self.root, *self.root.parents):
            marker = parent / ".git"
            if (parent.is_symlink() or parent.is_junction() or marker.exists()
                    or marker.is_symlink() or marker.is_junction()):
                raise ValueError("unsafe_answer_cache_path")
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.is_symlink() or path.is_junction():
                raise ValueError("unsafe_answer_cache_path")
            if path.exists():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size > self.policy.cache_max_bytes):
                    raise ValueError("unsafe_answer_cache_file")

    def _connect(self):
        self._safe_paths()
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            db.execute("PRAGMA busy_timeout=200")
            db.execute("PRAGMA trusted_schema=OFF")
            tables = db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if not tables:
                db.execute("CREATE TABLE answers (key TEXT PRIMARY KEY, as_of TEXT NOT NULL, ttl REAL NOT NULL, snapshot TEXT NOT NULL, digest TEXT NOT NULL, version INTEGER NOT NULL)")
                db.execute("PRAGMA user_version=1")
                db.commit()
            columns = [(r[1], r[2], r[3], r[5]) for r in db.execute("PRAGMA table_info(answers)")]
            expected = [("key", "TEXT", 0, 1), ("as_of", "TEXT", 1, 0), ("ttl", "REAL", 1, 0),
                        ("snapshot", "TEXT", 1, 0), ("digest", "TEXT", 1, 0), ("version", "INTEGER", 1, 0)]
            if (db.execute("PRAGMA user_version").fetchone()[0] != VERSION or columns != expected
                    or db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                    != [("answers", "table")]):
                raise ValueError("answer_cache_schema_mismatch")
            db.execute("PRAGMA journal_mode=DELETE")
            if db.execute("PRAGMA page_size").fetchone()[0] != 4096:
                raise ValueError("answer_cache_page_size_mismatch")
            db.execute(f"PRAGMA max_page_count={self.policy.cache_max_bytes // 4096}")
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def key(question: str, namespace: str) -> str:
        return hashlib.sha256(json.dumps([namespace, question], ensure_ascii=False).encode()).hexdigest()

    def lookup(self, question: str, namespace: str, *, now: str | None = None, freshness=False):
        db = self._connect()
        try:
            row = db.execute("SELECT as_of,ttl,snapshot,digest,version FROM answers WHERE key=?",
                             (self.key(question, namespace),)).fetchone()
        finally:
            db.close()
        if row is None:
            return None, "miss"
        as_of, ttl, snapshot, digest, version = row
        if (version != VERSION or not isinstance(snapshot, str)
                or len(snapshot.encode()) > self.policy.snapshot_max_bytes
                or hashlib.sha256(snapshot.encode()).hexdigest() != digest):
            return None, "snapshot_tampered"
        if redact_secrets(snapshot, self.known_secrets) != snapshot:
            return None, "secret_snapshot"
        try:
            report = Report.model_validate_json(snapshot)
            if report.question != question:
                return None, "identity_mismatch"
            effective = min(ttl, self.policy.cache_ttl_seconds,
                            self.policy.freshness_ttl_seconds if freshness else self.policy.cache_ttl_seconds)
            reason = validate_snapshot(report, now=now or utc_now(), as_of=as_of, ttl=effective,
                                       known_secrets=self.known_secrets)
            return (report, "hit") if reason == "ok" else (None, reason)
        except (ValueError, TypeError, OverflowError):
            return None, "invalid_snapshot"

    def put(self, report: Report, namespace: str, *, now: str | None = None, freshness=False) -> str:
        as_of = now or utc_now()
        ttl = min(self.policy.cache_ttl_seconds, self.policy.freshness_ttl_seconds) if freshness else self.policy.cache_ttl_seconds
        reason = validate_snapshot(report, now=as_of, as_of=as_of, ttl=ttl, known_secrets=self.known_secrets)
        if reason != "ok":
            return reason
        snapshot = projection(report).model_dump_json()
        if redact_secrets(snapshot, self.known_secrets) != snapshot:
            return "secret_snapshot"
        size = len(snapshot.encode())
        if size > self.policy.snapshot_max_bytes or size + 16384 > self.policy.cache_max_bytes:
            return "snapshot_size_limit"
        db = self._connect()
        try:
            with db:
                # Prune by timestamp plus a strict payload envelope；physical pages have their own cap．
                db.execute("DELETE FROM answers WHERE julianday(?) - julianday(as_of) >= ttl / 86400.0", (as_of,))
                db.execute("DELETE FROM answers WHERE key=?", (self.key(report.question, namespace),))
                while True:
                    count, total = db.execute("SELECT count(*),coalesce(sum(length(CAST(snapshot AS BLOB))),0) FROM answers").fetchone()
                    if count < self.policy.cache_max_entries and total + size <= self.policy.cache_max_bytes - 16384:
                        break
                    db.execute("DELETE FROM answers WHERE key=(SELECT key FROM answers ORDER BY as_of,key LIMIT 1)")
                db.execute("INSERT INTO answers VALUES (?,?,?,?,?,?)", (self.key(report.question, namespace), as_of,
                    ttl, snapshot, hashlib.sha256(snapshot.encode()).hexdigest(), VERSION))
        finally:
            db.close()
        self._safe_paths()
        return "stored"

    def discard(self, question: str, namespace: str) -> None:
        db = self._connect()
        try:
            with db:
                db.execute("DELETE FROM answers WHERE key=?", (self.key(question, namespace),))
        finally:
            db.close()
