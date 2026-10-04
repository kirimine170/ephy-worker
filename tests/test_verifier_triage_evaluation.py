"""Development-only controls; no real held-out data or model calls."""
import json
from dataclasses import asdict, replace

import pytest

from ephy_worker.verifier_triage_evaluation import (
    BASE_REVISION,
    BATCH_PATH,
    SKILL_PATH,
    FrozenTrial,
    InvalidEvaluation,
    SuppliedSession,
    check_context_fit,
    evaluate,
    evaluator_sha256,
    identifiable_components,
    sha256,
)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def development():
    ids = [f"development-{index:03d}" for index in range(12)]
    gold = [
        {"id": identifier, "decision": "STOP" if index % 2 == 0 else "PASS",
         "diagnosis": "ENVIRONMENT_DRIFT" if index % 2 == 0 else "CURRENT_EVIDENCE",
         "evidence_ids": ["e", "o"]}
        for index, identifier in enumerate(ids)
    ]
    batch = encoded([
        {"id": identifier, "input": json.dumps({"expected": {"id": "e", "components": {}},
                                                "observed": {"id": "o", "components": {}}})}
        for identifier in ids
    ])
    skill = b"---\nname: ephy-verifier-triage\n---\nUse current, complete evidence.\n"
    frozen = FrozenTrial(
        base_revision=BASE_REVISION, evaluator_sha256=evaluator_sha256(),
        batch_sha256=sha256(batch), gold_sha256=sha256(encoded(gold)),
        skill_sha256=sha256(skill), policy_sha256="a" * 64, environment_sha256="b" * 64,
        prompt_sha256="c" * 64, model_id="synthetic-test-model", consumer_role="planner",
    )
    sessions = []
    for index, arm in enumerate(("baseline", "baseline", "treatment", "treatment")):
        answers = [dict(row) for row in gold]
        if arm == "baseline":
            for row in answers[:2]:
                row["diagnosis"] = "UNKNOWN"
        events = [
            {"kind": "provider_request", "model": frozen.model_id, "requests": 1},
            {"kind": "assistant", "model": frozen.model_id, "responseTokens": 100,
             "responseTokenCap": 1024, "outputTokens": 100, "stopReason": "toolUse"},
            {"kind": "governance_result", "details": {
                "role": frozen.consumer_role, "acknowledged": True,
                "policySha256": frozen.policy_sha256}},
            {"kind": "provider_request", "model": frozen.model_id, "requests": 2},
            {"kind": "assistant", "model": frozen.model_id, "responseTokens": 100,
             "responseTokenCap": 1024, "outputTokens": 200, "stopReason": "toolUse"},
            {"kind": "tool_call", "tool": "read", "path": BATCH_PATH,
             "toolCallId": "batch-read", "blocked": False},
            {"kind": "evidence_read", "path": BATCH_PATH, "toolCallId": "batch-read",
             "sha256": frozen.batch_sha256, "full_content_delivered": True},
        ]
        if arm == "treatment":
            events += [
                {"kind": "tool_call", "tool": "read", "path": SKILL_PATH,
                 "toolCallId": "skill-read", "blocked": False},
                {"kind": "evidence_read", "path": SKILL_PATH, "toolCallId": "skill-read",
                 "sha256": frozen.skill_sha256, "full_content_delivered": True},
            ]
        events += [
            {"kind": "provider_request", "model": frozen.model_id, "requests": 3},
            {"kind": "assistant", "model": frozen.model_id, "responseTokens": 100,
             "responseTokenCap": 1024, "outputTokens": 300, "stopReason": "stop"},
            {"kind": "stage_end", "failed": False, "requests": 3, "outputTokens": 300},
        ]
        sessions.append(SuppliedSession(
            arm=arm, session_id=f"synthetic-session-{index}", pid=1000 + index,
            process_started_at=1000.0 + index, elapsed_seconds=1.0,
            bindings=asdict(frozen), output=encoded({
                "skill_sha256": frozen.skill_sha256 if arm == "treatment" else None,
                "answers": answers,
            }),
            trace=b"\n".join(encoded(event) for event in events),
        ))
    return frozen, batch, encoded(gold), skill, sessions


def response(session, mutate):
    data = json.loads(session.output)
    mutate(data)
    return replace(session, output=encoded(data))


def trace(session, mutate):
    events = [json.loads(line) for line in session.trace.splitlines()]
    mutate(events)
    return replace(session, trace=b"\n".join(encoded(event) for event in events))


def test_valid_descriptive_comparison_is_not_audit_adoption_or_significance(development):
    result = evaluate(*development)
    assert result["outcome"] == "descriptive_improvement"
    assert result["arms"]["baseline"]["safety_correct"] == 24
    assert result["arms"]["baseline"]["diagnosis_correct"] == 20
    assert result["arms"]["treatment"]["diagnosis_correct"] == 24
    assert result["case_observations"] == 48
    assert result["consumer_requests"] == 12
    assert not result["statistical_significance_claimed"]
    assert not result["adoption_authorized"]
    assert not result["live_execution_attested"]


@pytest.mark.parametrize("decision", ["PASS", "STOP"])
def test_pass_all_and_stop_all_are_not_improvements(development, decision):
    frozen, batch, gold, skill, sessions = development
    sessions[2:] = [
        response(s, lambda d: [row.update(decision=decision) for row in d["answers"]])
        for s in sessions[2:]
    ]
    assert evaluate(frozen, batch, gold, skill, sessions)["outcome"] == "degenerate_treatment"


def test_noop_and_baseline_ceiling_are_not_improvements(development):
    frozen, batch, gold, skill, sessions = development
    baseline_answers = json.loads(sessions[0].output)["answers"]
    sessions[2:] = [
        response(s, lambda d: d.update(answers=baseline_answers)) for s in sessions[2:]
    ]
    assert evaluate(frozen, batch, gold, skill, sessions)["outcome"] == "no_measured_improvement"
    correct = json.loads(gold)
    sessions = [response(s, lambda d: d.update(answers=correct)) for s in sessions]
    assert evaluate(frozen, batch, gold, skill, sessions)["outcome"] == "baseline_at_ceiling"


def test_an_unsafe_pass_is_rejected_even_with_perfect_diagnoses(development):
    frozen, batch, gold, skill, sessions = development
    sessions[2] = response(sessions[2], lambda d: d["answers"][0].update(decision="PASS"))
    result = evaluate(frozen, batch, gold, skill, sessions)
    assert result["outcome"] == "unsafe_or_regressed_treatment"
    assert result["arms"]["treatment"]["unsafe_passes"] == 1
    assert result["arms"]["treatment"]["diagnosis_correct"] == 24


@pytest.mark.parametrize("mutation", [
    "missing_read", "partial_read", "grep_only", "wrong_read_hash", "wrong_read_call",
    "false_output_hash", "stale_ci", "wrong_input", "wrong_policy", "wrong_model",
    "duplicate_session", "reused_process", "write", "gold_read", "timeout", "request_cap",
    "response_cap", "usage_mismatch", "missing_stage_end", "violation", "duplicate_case",
    "missing_case", "substituted_case", "duplicate_json_key", "gold_copy",
])
def test_supplied_evidence_negative_controls(development, mutation):
    frozen, batch, gold, skill, sessions = development
    selected = sessions[2]
    if mutation in {"missing_read", "partial_read", "grep_only", "wrong_read_hash", "wrong_read_call"}:
        def edit(events):
            if mutation in {"missing_read", "grep_only"}:
                events[:] = [e for e in events if e.get("path") != SKILL_PATH]
                if mutation == "grep_only":
                    events.insert(-1, {"kind": "tool_call", "tool": "grep", "path": SKILL_PATH})
            else:
                event = next(e for e in events if e.get("kind") == "evidence_read" and e.get("path") == SKILL_PATH)
                event[{"partial_read": "full_content_delivered", "wrong_read_hash": "sha256",
                       "wrong_read_call": "toolCallId"}[mutation]] = False if mutation == "partial_read" else "wrong"
        selected = trace(selected, edit)
    elif mutation == "false_output_hash":
        selected = response(selected, lambda d: d.update(skill_sha256="0" * 64))
    elif mutation in {"stale_ci", "wrong_input"}:
        bindings = dict(selected.bindings)
        bindings["evaluator_sha256" if mutation == "stale_ci" else "batch_sha256"] = "0" * 64
        selected = replace(selected, bindings=bindings)
    elif mutation in {"wrong_policy", "wrong_model"}:
        def edit(events):
            if mutation == "wrong_policy":
                next(e for e in events if e.get("kind") == "governance_result")["details"]["policySha256"] = "0" * 64
            else:
                events[0]["model"] = "wrong-model"
        selected = trace(selected, edit)
    elif mutation == "duplicate_session":
        selected = replace(selected, session_id=sessions[0].session_id)
    elif mutation == "reused_process":
        selected = replace(selected, pid=sessions[0].pid, process_started_at=sessions[0].process_started_at)
    elif mutation in {"write", "gold_read"}:
        selected = trace(selected, lambda e: e.insert(-1, {
            "kind": "tool_call", "tool": "write" if mutation == "write" else "read",
            "path": "answers/gold.json", "toolCallId": "escape"}))
    elif mutation == "timeout":
        selected = replace(selected, elapsed_seconds=301)
    elif mutation in {"request_cap", "response_cap", "usage_mismatch", "missing_stage_end", "violation"}:
        def edit(events):
            if mutation == "request_cap":
                events[-1]["requests"] = 9
            elif mutation == "response_cap":
                events[1]["responseTokens"] = 1025
            elif mutation == "usage_mismatch":
                events[-1]["outputTokens"] = 1
            elif mutation == "missing_stage_end":
                events.pop()
            else:
                events.append({"kind": "violation", "reason": "Synthetic control"})
        selected = trace(selected, edit)
    elif mutation in {"duplicate_case", "missing_case", "substituted_case"}:
        def edit(data):
            if mutation == "duplicate_case":
                data["answers"][-1] = data["answers"][0]
            elif mutation == "missing_case":
                data["answers"].pop()
            else:
                data["answers"][-1]["id"] = "substituted-case-000"
        selected = response(selected, edit)
    elif mutation == "duplicate_json_key":
        selected = replace(selected, output=b'{"skill_sha256":null,"skill_sha256":null,"answers":[]}')
    elif mutation == "gold_copy":
        skill = gold
        frozen = replace(frozen, skill_sha256=sha256(skill))
    sessions[2] = selected
    with pytest.raises(InvalidEvaluation):
        evaluate(frozen, batch, gold, skill, sessions)


def test_changed_frozen_bytes_and_evaluator_fail_closed(development):
    frozen, batch, gold, skill, sessions = development
    for index in range(3):
        args = [batch, gold, skill]
        args[index] += b"\n"
        with pytest.raises(InvalidEvaluation, match="bytes changed"):
            evaluate(frozen, *args, sessions)
    with pytest.raises(InvalidEvaluation, match="Stale evaluator"):
        evaluate(replace(frozen, evaluator_sha256="0" * 64), batch, gold, skill, sessions)


def test_full_context_arithmetic_boundary():
    check_context_fit(128872, 2200, 131072)
    with pytest.raises(InvalidEvaluation, match="exceeds context"):
        check_context_fit(128873, 2200, 131072)
    with pytest.raises(InvalidEvaluation, match="output reserve"):
        check_context_fit(100, 1024, 131072)
    with pytest.raises(InvalidEvaluation, match="token count"):
        check_context_fit(True, 2200, 131072)


@pytest.mark.parametrize("references", [["e"], ["e", "e"], ["e", "invented"], None])
def test_missing_or_invented_evidence_ids_fail_closed(development, references):
    frozen, batch, gold, skill, sessions = development
    sessions[2] = response(sessions[2], lambda d: d["answers"][0].update(evidence_ids=references))
    with pytest.raises(InvalidEvaluation):
        evaluate(frozen, batch, gold, skill, sessions)


@pytest.mark.parametrize("component", ["PATH", "pytest_version", "python_executable", "locale"])
def test_aggregate_and_one_sided_components_never_identify_cause(component):
    before = {"aggregate": "a" * 64, "components": {}}
    after = {"aggregate": "b" * 64, "components": {component: "b" * 64}}
    assert not identifiable_components(json.dumps({"expected": before, "observed": after}))
    before["components"][component] = "a" * 64
    assert identifiable_components(json.dumps({"expected": before, "observed": after})) == {component}
    after["components"][component] = "a" * 64
    assert not identifiable_components(json.dumps({"expected": before, "observed": after}))


def test_unsupported_component_cannot_count_as_improvement(development):
    frozen, batch, gold, skill, sessions = development
    sessions[2] = response(sessions[2], lambda d: d["answers"][0].update(diagnosis="component:PATH"))
    result = evaluate(frozen, batch, gold, skill, sessions)
    assert result["outcome"] == "unsafe_or_regressed_treatment"
    assert result["arms"]["treatment"]["unsupported_components"] == 1
