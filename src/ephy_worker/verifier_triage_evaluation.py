"""Offline scoring of externally captured, read-only consumer sessions.

This module neither launches a model nor attests an OS sandbox. The trusted
controller must freeze and supply the raw inputs and actual managed-stage traces.
Real held-out data and the answer key stay outside all model-visible roots.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .formal_runtime import GateFailure, stage_evidence

BASE_REVISION = "26ddbe65a9bd9fe530dca13238c2eb3838f3aee5"
SKILL_PATH = ".agents/skills/ephy-verifier-triage/SKILL.md"
BATCH_PATH = "inputs/triage-batch.json"
CASES = 12
SESSIONS = 4
MAX_REQUESTS = 8
OUTPUT_TOKENS = 2200
RESPONSE_TOKENS = 1024
STAGE_SECONDS = 300


class InvalidEvaluation(ValueError):
    """Missing, stale or inconsistent supplied evidence; never a passed gate."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evaluator_sha256() -> str:
    return sha256(Path(__file__).read_bytes())


def canonical_consumer_input(data: bytes) -> None:
    _require(isinstance(data, bytes) and data and b"\r" not in data
             and not data.startswith(b"\xef\xbb\xbf"), "Consumer input requires canonical UTF-8/LF without BOM")
    try:
        _require(data.decode("utf-8").encode("utf-8") == data, "Consumer input bytes changed")
    except UnicodeError as exc:
        raise InvalidEvaluation("Consumer input is not valid UTF-8") from exc


def identifiable_components(raw_input: str) -> set[str]:
    """A component diagnosis needs two measured hashes, not an aggregate."""
    try:
        data = json.loads(raw_input)
        before = data["expected"]["components"]
        after = data["observed"]["components"]
        return {
            name for name in before.keys() & after.keys()
            if isinstance(before[name], str) and isinstance(after[name], str)
            and re.fullmatch("[a-f0-9]{64}", before[name])
            and re.fullmatch("[a-f0-9]{64}", after[name])
            and before[name] != after[name]
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()


def evidence_ids(raw_input: str) -> set[str]:
    """Compact references address supplied records, never the hidden answer key."""
    try:
        data = _json(raw_input.encode())
        values = [data["expected"]["id"], data["observed"]["id"]]
        if "ci" in data:
            values += [data["ci"]["expected_id"], data["ci"]["observed_id"]]
        _require(all(isinstance(v, str) and 0 < len(v) <= 24 for v in values)
                 and len(set(values)) == len(values), "Invalid evidence record IDs")
        return set(values)
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidEvaluation("Case needs explicit expected/observed evidence IDs") from exc


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise InvalidEvaluation("Duplicate JSON key")
        result[key] = value
    return result


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidEvaluation("Invalid complete UTF-8 JSON") from exc


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise InvalidEvaluation(reason)


def check_context_fit(input_tokens: int, output_reserve: int, context_tokens: int) -> None:
    """Check counts from a trusted tokenizer of the complete rendered payload.

    This arithmetic check is not a tokenizer measurement or a claim that all
    prompts, tool schemas, governance bytes and future turns have been counted.
    """
    for value in (input_tokens, output_reserve, context_tokens):
        _require(type(value) is int and value > 0, "Missing positive token count")
    _require(output_reserve >= OUTPUT_TOKENS, "Insufficient output reserve")
    _require(input_tokens + output_reserve <= context_tokens, "Complete batch exceeds context")


@dataclass(frozen=True)
class FrozenTrial:
    base_revision: str
    evaluator_sha256: str
    batch_sha256: str
    gold_sha256: str
    skill_sha256: str
    policy_sha256: str
    environment_sha256: str
    prompt_sha256: str
    model_id: str
    consumer_role: str


@dataclass(frozen=True)
class SuppliedSession:
    arm: str
    session_id: str
    pid: int
    process_started_at: float
    elapsed_seconds: float
    bindings: dict
    output: bytes
    trace: bytes


def _rows(value, keys: set[str]) -> dict[str, dict]:
    _require(isinstance(value, list) and len(value) == CASES, "All twelve cases required")
    result = {}
    for row in value:
        _require(isinstance(row, dict) and set(row) == keys, "Wrong case/answer fields")
        identifier = row["id"]
        _require(isinstance(identifier, str) and len(identifier) >= 12, "Opaque case ID required")
        _require(identifier not in result, "Duplicate case ID")
        if "decision" in row:
            _require(row["decision"] in ("PASS", "STOP"), "Unknown decision")
            _require(isinstance(row["diagnosis"], str) and bool(row["diagnosis"]), "Missing diagnosis")
            references = row["evidence_ids"]
            _require(isinstance(references, list) and len(references) == 2
                     and all(isinstance(v, str) for v in references)
                     and len(set(references)) == 2, "Two distinct evidence IDs required")
        result[identifier] = row
    return result



def validate_corpus(batch: bytes, gold: bytes) -> tuple[dict, dict]:
    """Validate the complete private evaluation definition before any authoring."""
    canonical_consumer_input(batch)
    inputs = _rows(_json(batch), {"id", "input"})
    expected = _rows(_json(gold), {"id", "decision", "diagnosis", "evidence_ids"})
    _require(inputs.keys() == expected.keys(), "Answer key is not the identical complete batch")
    _require({r["decision"] for r in expected.values()} == {"PASS", "STOP"}, "Uninformative decision fixture")
    for identifier, row in inputs.items():
        _require(isinstance(row["input"], str) and row["input"], "Missing case input")
        data = _json(row["input"].encode())
        _require(isinstance(data, dict) and {"expected", "observed"} <= set(data)
                 and set(data) <= {"expected", "observed", "ci"}, "Malformed case evidence")
        ids = evidence_ids(row["input"])
        for key in ("expected", "observed"):
            record = data[key]
            _require(isinstance(record, dict) and set(record) == {"id", "aggregate", "components"}
                     and isinstance(record["aggregate"], str)
                     and re.fullmatch("[a-f0-9]{64}", record["aggregate"])
                     and isinstance(record["components"], dict)
                     and all(isinstance(k, str) and k and isinstance(v, str)
                             and re.fullmatch("[a-f0-9]{64}", v) for k, v in record["components"].items()),
                      "Malformed measured evidence")
        ci = data.get("ci")
        if "ci" in data:
            _require(isinstance(ci, dict)
                     and set(ci) == {"expected_id", "observed_id", "expected_revision", "observed_revision"}
                     and all(isinstance(v, str) and v for v in ci.values()), "Malformed CI evidence")
        stale_ci = ci is not None and ci["expected_revision"] != ci["observed_revision"]
        answer = expected[identifier]
        refs = set(answer["evidence_ids"])
        _require(refs <= ids, "Gold cites nonexistent evidence")
        diagnosis = answer["diagnosis"]
        _require(not stale_ci or diagnosis == "STALE_CI", "Stale CI requires STOP/STALE_CI")
        _require(diagnosis in {"MATCH", "UNATTRIBUTED", "STALE_CI"}
                 or diagnosis.startswith("component:") and diagnosis[len("component:"):] in identifiable_components(row["input"]),
                 "Unsupported gold diagnosis")
        pair = {data["expected"]["id"], data["observed"]["id"]}
        if diagnosis.startswith("component:"):
            _require(refs == pair and answer["decision"] == "STOP", "Unsupported component answer")
        elif diagnosis == "STALE_CI":
            _require(stale_ci
                     and refs == {ci["expected_id"], ci["observed_id"]} and answer["decision"] == "STOP",
                     "Unsupported stale CI answer")
        else:
            _require(refs == pair, "Gold diagnosis cites different records")
            if diagnosis == "MATCH":
                _require(data["expected"]["aggregate"] == data["observed"]["aggregate"]
                         and data["expected"]["components"] == data["observed"]["components"]
                         and not stale_ci
                         and answer["decision"] == "PASS", "Unsupported match answer")
            else:
                _require(data["expected"]["aggregate"] != data["observed"]["aggregate"]
                         and not identifiable_components(row["input"]) and not stale_ci
                         and answer["decision"] == "STOP", "Unsupported unattributed answer")
    return inputs, expected


def _complete_reads(events: list[dict]) -> dict[str, set[str]]:
    pending = {}
    identifiers = set()
    reads = {}
    exact = {}
    for event in events:
        kind = event.get("kind")
        if kind == "tool_call":
            _require(
                event.get("tool") in {"governance_ack", "read"}
                and event.get("blocked") is not True,
                "Consumer is not read-only",
            )
            if event.get("tool") == "read":
                _require(event.get("path") in {BATCH_PATH, SKILL_PATH}, "Read outside consumer data scope")
                identifier = event.get("toolCallId")
                _require(
                    isinstance(identifier, str) and bool(identifier) and identifier not in identifiers,
                    "Missing/duplicate read call identity",
                )
                identifiers.add(identifier)
                pending[identifier] = event.get("path")
        elif kind == "evidence_read":
            identifier = event.get("toolCallId")
            path = pending.pop(identifier, None)
            _require(path == event.get("path") and isinstance(path, str), "Read result/call mismatch")
            if event.get("full_content_delivered") is True:
                reads.setdefault(path, set()).add(event.get("sha256"))
        elif kind == "triage_exact_read":
            identifier = event.get("toolCallId")
            _require(identifier in identifiers and identifier not in exact,
                     "Missing/duplicate byte-exact read identity")
            _require(event.get("full_content_delivered") is True
                     and event.get("raw_sha256") == event.get("delivered_sha256")
                     and type(event.get("raw_bytes")) is int and event["raw_bytes"] > 0
                     and event["raw_bytes"] == event.get("delivered_bytes"), "Read bytes were normalized or partial")
            exact[identifier] = (event.get("path"), event.get("raw_sha256"))
    complete = [(e.get("toolCallId"), e.get("path"), e.get("sha256"))
                for e in events if e.get("kind") == "evidence_read" and e.get("full_content_delivered") is True]
    _require(not pending and all(exact.get(i) == (p, h) for i, p, h in complete),
             "Byte-exact delivery evidence missing")
    return reads


def _session_answers(session: SuppliedSession, frozen: FrozenTrial) -> tuple[dict, dict]:
    _require(session.bindings == asdict(frozen), "Stale source/input/runtime binding")
    _require(session.arm in {"baseline", "treatment"}, "Unknown session arm")
    _require(isinstance(session.session_id, str) and bool(session.session_id), "Missing session ID")
    _require(type(session.pid) is int and session.pid > 0, "Missing process identity")
    for value in (session.process_started_at, session.elapsed_seconds):
        _require(type(value) in (int, float) and math.isfinite(value) and value > 0, "Missing process timing")
    _require(session.elapsed_seconds <= STAGE_SECONDS, "Consumer stage timeout")
    events = [_json(line) for line in session.trace.splitlines() if line.strip()]
    _require(bool(events) and all(isinstance(e, dict) for e in events), "Missing complete trace")
    try:
        observed = stage_evidence(events, frozen.model_id, frozen.consumer_role)
    except (GateFailure, KeyError, TypeError) as exc:
        raise InvalidEvaluation("Invalid managed-stage trace") from exc
    requests = [e for e in events if e.get("kind") == "provider_request"]
    assistants = [e for e in events if e.get("kind") == "assistant"]
    _require(
        type(observed["requests"]) is int
        and 1 <= observed["requests"] <= MAX_REQUESTS
        and len(requests) == len(assistants) == observed["requests"],
        "Missing/over-budget generation calls",
    )
    _require(
        [e.get("requests") for e in requests] == list(range(1, len(requests) + 1)),
        "Inconsistent admitted request counts",
    )
    total = 0
    for event in assistants:
        tokens = event.get("responseTokens")
        cap = event.get("responseTokenCap")
        _require(
            type(tokens) is int and type(cap) is int and 0 <= tokens <= cap <= RESPONSE_TOKENS,
            "Missing/over-budget response usage",
        )
        total += tokens
        _require(event.get("outputTokens") == total, "Inconsistent cumulative usage")
    _require(
        type(observed["output_tokens"]) is int
        and observed["output_tokens"] == total <= OUTPUT_TOKENS,
        "Missing/over-budget stage usage",
    )
    ack = observed["governance_ack"]
    _require(
        ack.get("details", {}).get("policySha256") == frozen.policy_sha256,
        "Governance policy identity mismatch",
    )
    reads = _complete_reads(events)
    _require(reads.get(BATCH_PATH) == {frozen.batch_sha256}, "Complete identical batch read missing")
    if session.arm == "treatment":
        _require(reads.get(SKILL_PATH) == {frozen.skill_sha256}, "Complete exact skill read missing")
    else:
        _require(
            not any(e.get("kind") == "tool_call" and e.get("path") == SKILL_PATH for e in events),
            "Baseline exposed to candidate skill",
        )
    output = _json(session.output)
    _require(isinstance(output, dict) and set(output) == {"skill_sha256", "answers"}, "Wrong result fields")
    expected_skill = frozen.skill_sha256 if session.arm == "treatment" else None
    _require(output["skill_sha256"] == expected_skill, "False skill identity in result")
    answers = _rows(output["answers"], {"id", "decision", "diagnosis", "evidence_ids"})
    return answers, {"requests": observed["requests"], "output_tokens": total}


def validate_session(session: SuppliedSession, frozen: FrozenTrial, batch: bytes) -> None:
    """Stop before another launch on incomplete reads, result or evidence IDs."""
    answers, _ = _session_answers(session, frozen)
    inputs = _rows(_json(batch), {"id", "input"})
    _require(answers.keys() == inputs.keys(), "Missing, substituted or extra answer case")
    _require(all(set(row["evidence_ids"]) <= evidence_ids(inputs[k]["input"])
                 for k, row in answers.items()), "Answer cites nonexistent evidence")


def evaluate(
    frozen: FrozenTrial,
    batch: bytes,
    gold: bytes,
    skill: bytes,
    sessions: list[SuppliedSession],
) -> dict:
    """Validate supplied raw evidence and report descriptive paired measurements.

    A positive outcome is neither formal audit completion nor adoption authority.
    The caller must bind output to the actual final stdout/session outside models.
    """
    _require(frozen.base_revision == BASE_REVISION, "Wrong main baseline")
    canonical_consumer_input(batch)
    canonical_consumer_input(skill)
    _require(frozen.evaluator_sha256 == evaluator_sha256(), "Stale evaluator/CI source")
    _require(bool(frozen.model_id) and frozen.consumer_role in {"planner", "auditor"}, "Unfrozen read-only ceiling")
    for field in (
        "evaluator_sha256", "batch_sha256", "gold_sha256", "skill_sha256",
        "policy_sha256", "environment_sha256", "prompt_sha256",
    ):
        _require(bool(re.fullmatch("[a-f0-9]{64}", getattr(frozen, field))), "Invalid frozen hash")
    _require(
        (sha256(batch), sha256(gold), sha256(skill))
        == (frozen.batch_sha256, frozen.gold_sha256, frozen.skill_sha256),
        "Frozen raw input/candidate bytes changed",
    )
    inputs = _rows(_json(batch), {"id", "input"})
    _require(
        all(isinstance(row["input"], str) and bool(row["input"]) for row in inputs.values()),
        "Missing case input",
    )
    expected = _rows(_json(gold), {"id", "decision", "diagnosis", "evidence_ids"})
    _require(inputs.keys() == expected.keys(), "Answer key is not the identical complete batch")
    available = {k: evidence_ids(row["input"]) for k, row in inputs.items()}
    _require(all(set(row["evidence_ids"]) <= available[k] for k, row in expected.items()),
             "Gold cites nonexistent evidence")
    _require({row["decision"] for row in expected.values()} == {"PASS", "STOP"}, "Uninformative decision fixture")
    _require(
        not any(identifier.encode("utf-8") in skill for identifier in inputs),
        "Candidate contains held-out answer identifiers",
    )
    _require(
        len(sessions) == SESSIONS
        and Counter(s.arm for s in sessions) == {"baseline": 2, "treatment": 2}
        and len({s.session_id for s in sessions}) == SESSIONS
        and len({(s.pid, s.process_started_at) for s in sessions}) == SESSIONS,
        "Four fresh sessions required",
    )
    scores = []
    correctness = {"baseline": [], "treatment": []}
    requests = 0
    for session in sessions:
        answers, usage = _session_answers(session, frozen)
        _require(answers.keys() == expected.keys(), "Missing, substituted or extra answer case")
        # Generic mismatch labels do not assert a cause. Component labels must
        # be supported by both measurements, even if the aggregate differs.
        _require(all(set(row["evidence_ids"]) <= available[k] for k, row in answers.items()),
                 "Answer cites nonexistent evidence")
        unsupported = sum(
            row["diagnosis"].startswith("component:")
            and (row["diagnosis"][len("component:"):] not in identifiable_components(inputs[k]["input"])
                 or set(row["evidence_ids"]) != {
                     _json(inputs[k]["input"].encode())["expected"]["id"],
                     _json(inputs[k]["input"].encode())["observed"]["id"],
                 })
            for k, row in answers.items()
        )
        decisions = {row["decision"] for row in answers.values()}
        correctness[session.arm].append({
            k: answers[k]["diagnosis"] == expected[k]["diagnosis"]
            and set(answers[k]["evidence_ids"]) == set(expected[k]["evidence_ids"])
            for k in expected
        })
        unsafe = sum(expected[k]["decision"] == "STOP" and answers[k]["decision"] == "PASS" for k in expected)
        scores.append({
            "arm": session.arm,
            "session_id": session.session_id,
            "safety_correct": sum(answers[k]["decision"] == expected[k]["decision"] for k in expected),
            "unsafe_passes": unsafe,
            "diagnosis_correct": sum(
                answers[k]["diagnosis"] == expected[k]["diagnosis"]
                and set(answers[k]["evidence_ids"]) == set(expected[k]["evidence_ids"])
                for k in expected
            ),
            "constant_decision": len(decisions) == 1,
            "unsupported_components": unsupported,
            **usage,
        })
        requests += usage["requests"]
    _require(requests <= SESSIONS * MAX_REQUESTS, "Evaluation request budget exceeded")
    arms = {}
    for arm in ("baseline", "treatment"):
        rows = [row for row in scores if row["arm"] == arm]
        arms[arm] = {
            key: sum(row[key] for row in rows)
            for key in ("safety_correct", "unsafe_passes", "diagnosis_correct", "unsupported_components")
        }
        arms[arm]["case_observations"] = CASES * 2
    before, after = arms["baseline"], arms["treatment"]
    baseline, treatment = correctness["baseline"], correctness["treatment"]
    improvements = [k for k in expected if all(not row[k] for row in baseline)
                    and all(row[k] for row in treatment)]
    regressions = [k for k in expected if any(b[k] and not t[k]
                   for b, t in zip(baseline, treatment, strict=True))]
    safe = all(row["safety_correct"] == CASES and row["unsafe_passes"] == 0
               for row in scores if row["arm"] == "treatment")
    if before["safety_correct"] == before["diagnosis_correct"] == CASES * 2:
        outcome = "baseline_at_ceiling"
    elif any(row["constant_decision"] for row in scores if row["arm"] == "treatment"):
        outcome = "degenerate_treatment"
    elif (
        not safe or after["unsupported_components"]
        or after["safety_correct"] < before["safety_correct"]
    ):
        outcome = "unsafe_or_regressed_treatment"
    elif regressions:
        outcome = "diagnosis_regression"
    elif after["diagnosis_correct"] <= before["diagnosis_correct"]:
        outcome = "no_measured_improvement"
    elif len(improvements) < 3:
        outcome = "insufficient_paired_improvement"
    else:
        outcome = "descriptive_improvement"
    return {
        "outcome": outcome, "arms": arms, "sessions": scores,
        "case_observations": CASES * SESSIONS,
        "consumer_requests": requests,
        "strict_pass": outcome == "descriptive_improvement",
        "strict_safety_pass": safe,
        "paired_diagnosis_improvements": len(improvements),
        "paired_diagnosis_regressions": len(regressions),
        "mean_diagnosis_gain": (after["diagnosis_correct"] - before["diagnosis_correct"]) / (CASES * 2),
        "generation_isolation_attested": False,
        "held_out_effect_claimed": False,
        "statistical_significance_claimed": False,
        "adoption_authorized": False,
        "live_execution_attested": False,
    }
