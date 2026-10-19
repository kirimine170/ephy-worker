#!/usr/bin/env python3
"""Public synthetic routing/cache/completion replay．Default mode never uses a model．"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys

from ephy_worker.answer_routing_experiment import (
    BASE_URL, MODEL_ID, Completion, EvidenceCache, KnowledgeDraft, ReplayLimits, ReplayRound,
    RouteProposal, choose_route, local_probe, replay_research,
)
from ephy_worker.schema import Candidate, Claim, Evidence, Passage, Report, Source
from ephy_worker.store import output_root

EVALUATION_SHA256 = "5da28e17fd12fbd91a50c8edbb1056e8634361fb554a3ee26a3c4aba556f68e6"
NOW = "2026-10-18T00:03:00+00:00"
AS_OF = "2026-10-18T00:02:00+00:00"
CREATED = "2026-10-18T00:00:00+00:00"
QUESTION = "公開合成資料の機能を出典付きで説明してください．"


def public_report(count: int = 1) -> Report:
    """Canonical synthetic snapshots；no actual fetches or actual semantic reviews．"""
    sources, evidence, claims = [], [], []
    for number in range(1, count + 1):
        suffix = str(number)
        text = f"公開合成資料の標準版には機能{suffix}があります．"
        source = Source(
            source_id="s" + suffix, requested_url="https://example.org/fixture/" + suffix,
            kind="html", status="ok", fetched_at=CREATED, http_status=200,
            body_hash=hashlib.sha256(text.encode()).hexdigest(), bytes_received=len(text.encode()),
            extractor="synthetic-fixture", read_passage_ids=["p" + suffix],
            passages=[Passage(passage_id="p" + suffix, source_id="s" + suffix,
                              text=text, start=0, end=len(text))],
        )
        sources.append(source)
        evidence.append(Evidence(
            evidence_id="e" + suffix, source_id=source.source_id, passage_id="p" + suffix,
            quote=text, quote_start=0, quote_end=len(text), relation="supports", checked=True,
            conditions_match=True, is_primary=True,
        ))
        claims.append(Claim(
            claim_id="c" + suffix, text=text, evidence_ids=["e" + suffix], checked=True,
            status="supported_primary", conditions="標準版",
        ))
    return Report(
        job_id="public-synthetic", profile_id="public-synthetic", question=QUESTION,
        state="completed", answerability="sufficient", stop_reason="no_actionable_gaps",
        created_at=CREATED, finished_at="2026-10-18T00:01:00+00:00",
        sources=sources, evidence=evidence, claims=claims,
    )


def cache_fixture(name: str) -> tuple[EvidenceCache, str]:
    report = public_report()
    if name == "no_verified_facts":
        report.claims[0].checked = False
    elif name == "snippets_only":
        report.candidates = [Candidate(url="https://example.org/preview", snippet="紹介文だけです．")]
        report.sources, report.evidence, report.claims = [], [], []
    elif name == "tampered_span":
        report.evidence[0].quote_start += 1
    elif name == "duplicate_ids":
        report.claims.append(report.claims[0].model_copy(deep=True))
    elif name == "conflicting":
        report.claims[0].status = "conflicting"
        report.evidence[0].relation = "contradicts"
    elif name == "gaps":
        report.gaps = ["未確認の主要論点．"]
    elif name == "failed_report":
        report.failures = [{"code": "synthetic_failure"}]
    cache = EvidenceCache()
    cache.put(report, as_of=AS_OF, ttl_seconds=3600)
    entry = cache.entries[QUESTION]
    changes = {
        "expired": {"ttl_seconds": 30},
        "future": {"as_of": "2026-10-19T00:00:00+00:00"},
        "question_mismatch": {"question": "別の質問．"},
        "policy_mismatch": {"policy_id": "other-policy"},
        "bad_timestamp": {"as_of": "not-a-time"},
    }
    cache.entries[QUESTION] = replace(entry, **changes.get(name, {}))
    return cache, QUESTION


def completion_fixture(name: str) -> tuple[tuple[str, ...], list[ReplayRound], ReplayLimits]:
    subquestions = ("仕組み", "適用条件", "制限")
    rounds = [ReplayRound(public_report(3), {key: (f"c{i}",) for i, key in enumerate(subquestions, 1)})]
    limits = ReplayLimits()
    if name == "gap_then_sufficient_after_three_rounds":
        rounds = []
        for count in (1, 2, 3):
            report = public_report(count)
            report.gaps = ["残る主要論点．"] if count < 3 else []
            rounds.append(ReplayRound(report, {key: (f"c{i}",) for i, key in enumerate(subquestions[:count], 1)}))
    elif name == "unresolved_conflict":
        report = public_report(3)
        report.claims[2].status = "conflicting"
        report.evidence[2].relation = "contradicts"
        rounds = [ReplayRound(report, {"仕組み": ("c1",), "適用条件": ("c2",)})] * 3
    elif name == "condition_mismatch":
        rounds[0].report.evidence[0].conditions_match = False
    elif name == "unknown_claim_coverage":
        rounds[0].coverage["仕組み"] = ("unknown",)
    elif name == "unverified_claim_coverage":
        rounds[0].report.claims[0].checked = False
    elif name in {"duplicate_stall", "model_limit_partial", "round_limit_partial", "time_limit_partial"}:
        report = public_report()
        report.gaps = ["主要論点が未確認．"]
        rounds = [ReplayRound(report, {"仕組み": ("c1",)})] * 6
        if name == "model_limit_partial":
            limits = ReplayLimits(model_calls=1)
        elif name == "round_limit_partial":
            limits = ReplayLimits(rounds=1)
        elif name == "time_limit_partial":
            rounds = [replace(rounds[0], elapsed_seconds=300)]
    elif name == "cancelled":
        rounds = [replace(rounds[0], cancelled=True, elapsed_seconds=300, model_calls=7)]
    return subquestions, rounds, limits


COMPLETION_EXPECTED = {
    "sufficient_early": ("sufficient", "coverage_complete", 1),
    "gap_then_sufficient_after_three_rounds": ("sufficient", "coverage_complete", 3),
    "unresolved_conflict": ("partial", "duplicate_stall", 3),
    "condition_mismatch": ("partial", "coverage_invalid", 1),
    "unknown_claim_coverage": ("partial", "coverage_invalid", 1),
    "unverified_claim_coverage": ("partial", "coverage_invalid", 1),
    "duplicate_stall": ("partial", "duplicate_stall", 3),
    "time_limit_partial": ("partial", "time_limit", 1),
    "model_limit_partial": ("partial", "model_limit", 1),
    "round_limit_partial": ("partial", "round_limit", 1),
    "cancelled": ("cancelled", "cancelled", 1),
}


def offline_evaluation(cases: dict) -> dict:
    rows = []
    # Deliberately identical optimistic proposal for every case．The guard sees only the question．
    proposal = RouteProposal(route="knowledge", kind="stable_explanation", confidence="high",
                             draft=KnowledgeDraft(text="一般的な仕組みの説明です．"))
    for case in cases["routing_cases"]:
        decision = choose_route(case["question"], proposal, EvidenceCache(), now=NOW)
        rows.append(dict(id=case["id"], category="routing", expected=case["expected_route"],
                         actual=decision.route, passed=decision.route == case["expected_route"],
                         guard=decision.guard, origin=decision.origin, cache_code=decision.cache_code))
    for name in cases["cache_cases"]:
        cache, question = cache_fixture(name)
        decision = choose_route(question, proposal, cache, now=NOW)
        expected = "cache" if name == "fresh_verified" else "research"
        rows.append(dict(id=name, category="cache", expected=expected, actual=decision.route,
                         passed=decision.route == expected, cache_code=decision.cache_code))
    for name in cases["completion_cases"]:
        questions, rounds, limits = completion_fixture(name)
        result: Completion = replay_research(questions, rounds, limits=limits)
        actual = (result.state, result.reason, result.rounds)
        expected = COMPLETION_EXPECTED[name]
        rows.append(dict(id=name, category="completion", expected=expected, actual=actual,
                         passed=actual == expected, metrics=asdict(result)))
    return dict(mode="offline", passed=sum(row["passed"] for row in rows),
                failed=sum(not row["passed"] for row in rows), model_requests=0, rows=rows,
                limitation="Synthetic replay tests finite policy rules, not knowledge quality or live Web correctness.")


def evaluate_probe(cases: dict, observed: dict) -> dict:
    routing = {c["id"]: c for c in cases["routing_cases"]}
    rows = []
    for row in observed["rows"]:
        result = dict(row)
        if row["id"] in routing:
            case = routing[row["id"]]
            if row["code"] == "ok":
                proposal = RouteProposal.model_validate(row["typed"])
                decision = choose_route(case["question"], proposal, EvidenceCache(), now=NOW)
                result.update(actual=decision.route, expected=case["expected_route"], guard=decision.guard,
                              origin=decision.origin, passed=decision.route == case["expected_route"])
                if "answer_contains_any" in case:
                    result["answer_heuristic_passed"] = bool(proposal.draft) and any(
                        value in proposal.draft.text for value in case["answer_contains_any"]
                    )
            else:
                result["passed"] = False
        elif row["id"] == "evidence_labels" and row["code"] == "ok":
            labels = row["typed"]["labels"]
            result["checks"] = [dict(id=case["id"], expected=case["expected"], actual=label,
                                     passed=label == case["expected"])
                                for case, label in zip(cases["evidence_probe"]["claims"], labels, strict=True)]
            result["passed"] = all(check["passed"] for check in result["checks"])
        else:
            result["passed"] = False
        rows.append(result)
    return {**observed, "mode": "live_model", "rows": rows}


def safe_output_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("output_must_be_absolute")
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("output_link_rejected")
    resolved = output_root(path)
    if resolved.exists():
        raise ValueError("output_exists")
    if not resolved.parent.is_dir():
        raise ValueError("output_parent_missing")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--model-id", default=MODEL_ID)
    args = parser.parse_args(argv)
    try:
        destination = safe_output_path(args.output)
        raw = args.cases.read_bytes()
        if hashlib.sha256(raw).hexdigest() != EVALUATION_SHA256:
            raise ValueError("evaluation_identity_rejected")
        cases = json.loads(raw)
        if args.live_model:
            observed = asyncio.run(local_probe(cases, base_url=args.base_url, model_id=args.model_id))
            result = evaluate_probe(cases, observed)
        else:
            result = offline_evaluation(cases)
        result["evaluation_sha256"] = EVALUATION_SHA256
        with destination.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"mode": result["mode"], "model_requests": result["model_requests"],
                          "output": str(destination)}))
        return int(result.get("failed", 0) > 0)
    except (ValueError, OSError):
        # Exception details may contain user paths，model text or invalid JSON．Never persist them．
        print("answer_routing_evaluation_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
