"""Offline synthetic controls with accepted local P2 dependency pins.

Dependency acceptance and transport are recorded in external evidence.
Actual execution results belong to evidence/verification-runs/
sol-integration-v1/summary.json; this file makes no execution or pass claim.
No supplied command, model descriptor, research URL or campaign draft is executed.
"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker import formal_campaign
from ephy_worker.benchmark import BenchmarkRun, Conditions, fingerprint, make_run
from ephy_worker.coding_schema import (
    CodingArtifacts,
    CodingFixture,
    CodingMetrics,
    CodingModelProfile,
    CodingResult,
    ModelResult,
    RepositoryResult,
    ResultStatus,
)
from ephy_worker.evaluation_discovery import DiscoveryInputError, normalize_evaluation_candidates
from ephy_worker.formal_artifacts import GateFailure, digest, encode, read_json, write_json
from ephy_worker.schema import Claim, Evidence, Passage, Report, Source
from ephy_worker.trial_reconciliation import (
    ExecutionLedger,
    ExecutionRecord,
    PlannedTrial,
    TrialPlan,
    execution_ledger_sha256,
    plan_sha256,
    reconcile_trials,
    record_execution,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/improvement_integration/planner_gap_case_v1.json"
CHECKER_SHA256 = "d8d4c2c47a97cdaac946c5ff5f4ae5cee1ea1bebcec96c0e8b2d616350b45180"


def raw_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


@pytest.fixture
def case():
    raw = FIXTURE.read_bytes()
    value = json.loads(raw)
    assert value["schema"] == "ephy.synthetic-improvement-integration-fixture.v1"
    assert value["evidence_kind"] == "synthetic"
    assert len(value["variants"]) == 2
    assert [variant["label"] for variant in value["variants"]] == ["baseline", "candidate"]
    assert value["repeat_index"] == 1
    assert value["candidate_count"] == 1
    assert sum(item["label"] == "candidate" for item in value["variants"]) == value["candidate_count"]
    assert value["budget"] == {
        "timeout_seconds": 30, "model_calls": 0, "search_calls": 0, "automatic_repairs": 0,
    }
    assert value["candidate_id"] == value["research"]["selection"]["candidate_id"]
    return SimpleNamespace(data=value, raw=raw, sha256=digest(raw))


def saved_planner_inputs(case, label, *, text=None, job_id=None):
    """Construct invented saved files using the owner's actual five-input contract.

    Pins are retained before mutations by callers; this builder is never used to
    regenerate expectations from attacked or caller-submitted evidence.
    """
    data = case.data["planner"]
    completion = copy.deepcopy(data["completion_contract"])
    contract_bytes = raw_json(completion)
    runtime = {
        "planner_only": True, "max_repairs": 0,
        "allowed_files": completion["allowed_files"].copy(),
        "semantic_scope": completion["semantic_scope"],
        "checks": [{**check, "baseline_exit_code": 0} for check in completion["checks"]],
        "timeout_seconds": case.data["budget"]["timeout_seconds"],
        "task": "Synthetic saved planner output only.",
    }
    pins = {
        "expected_contract_sha256": digest(contract_bytes),
        "expected_job_id": job_id if job_id is not None else "synthetic-planner-" + label,
        "expected_job_contract_sha256": digest(encode(runtime)),
    }
    assert label in {"baseline", "candidate"}
    if text is None:
        plan = copy.deepcopy(data["candidate_plan"])
        plan["contract_sha256"] = pins["expected_contract_sha256"]
        if label == "baseline":
            plan["verification"] = [
                item for item in plan["verification"]
                if item["check_id"] != data["baseline_missing_check"]
            ]
        text = encode(plan).decode("utf-8")
    message = {
        "role": "assistant", "stopReason": "stop", "model": "synthetic-no-model-execution",
        "provider": "offline", "content": [{"type": "text", "text": text}],
    }
    terminal = {"type": "message", "id": "terminal-" + label, "message": message}
    session = raw_json({"type": "session", "id": "synthetic-session-" + label}) + b"\n"
    session += raw_json(terminal) + b"\n"
    job = {
        "schemaVersion": 2, "id": pins["expected_job_id"], "status": "planner_stopped",
        "outcome": "no_hypothesis" if text.strip() == "NO_HYPOTHESIS" else "planned_only",
        "contract": runtime,
    }
    stop = {
        "job_id": job["id"], "contract_sha256": pins["expected_job_contract_sha256"],
        "plan_sha256": digest(text.encode("utf-8")), "snapshot_sha256": case.sha256,
        "implementation_started": False, "formal_audit_executed": False, "automatic_adoption": False,
    }
    return SimpleNamespace(
        pins=pins, terminal=terminal, message=message, text=text,
        inputs={"contract_bytes": contract_bytes, "job_bytes": raw_json(job),
                "session_bytes": session, "plan_bytes": text.encode("utf-8"),
                "stop_bytes": raw_json(stop)},
    )


def research_input(case):
    """Serialize every persisted Report field before its raw bytes are pinned."""
    spec = case.data["research"]
    text = spec["text"]
    passage = Passage(
        passage_id=spec["passage_id"], source_id=spec["source_id"], text=text, start=0, end=len(text),
    )
    report = Report(
        job_id=spec["job_id"], question="Synthetic public planning evaluation method",
        profile_id=case.data["profile"]["profile_id"], created_at=spec["timestamp"],
        finished_at=spec["timestamp"], state="completed", stop_reason="synthetic_fixture",
        sources=[Source(
            source_id=spec["source_id"], requested_url=spec["source_url"], status="ok",
            fetched_at=spec["timestamp"], body_hash=None, passages=[passage],
            read_passage_ids=[spec["passage_id"]], limitations=["Invented passage; not fetched."],
        )],
        evidence=[Evidence(
            evidence_id=spec["evidence_id"], source_id=spec["source_id"],
            passage_id=spec["passage_id"], quote=text, quote_start=0, quote_end=len(text),
            relation="supports", checked=True, check_reason="Exact synthetic passage match.",
            conditions=spec["conditions"], conditions_match=True, is_primary=True,
        )],
        claims=[Claim(
            claim_id=spec["claim_id"], text=text, evidence_ids=[spec["evidence_id"]],
            status="supported_primary", checked=True, reason="Supplied synthetic primary support.",
            conditions=spec["conditions"], uncertainty=["No actual efficacy was evaluated."],
        )],
        gaps=spec["information_gaps"],
    )
    return raw_json(report.model_dump(mode="json"))


def normalize(case, raw, *, expected_sha256, selections=None):
    selected = [case.data["research"]["selection"]] if selections is None else selections
    assert len(selected) == case.data["candidate_count"] == 1, "candidate_count"
    return normalize_evaluation_candidates(
        raw, expected_report_sha256=expected_sha256,
        expected_job_id=case.data["research"]["job_id"], selections=selected,
    )


def trial_design(case):
    """Build and commit two planned IDs/conditions before any invented result exists."""
    source = ROOT / "src/ephy_worker/plan_completion.py"
    assert digest(source.read_bytes()) == CHECKER_SHA256
    data = case.data
    profile = CodingModelProfile.model_validate(data["profile"])
    task = CodingFixture.model_validate(data["task"])
    fixed = {
        "suite_id": "single-case-contract-integration", "suite_sha256": case.sha256,
        "task_sha256": fingerprint(task.model_dump(mode="json")),
        "checker_sha256": CHECKER_SHA256,
        "environment_sha256": fingerprint(data["environment"]),
        "budget_sha256": fingerprint(data["budget"]),
        "model_sha256": fingerprint(profile.model_dump(
            mode="json", exclude={"profile_id", "notes", "endpoint", "pi_executable"},
        )),
    }
    plan = TrialPlan(
        plan_id="synthetic-single-case-plan", axis="variant", evidence_kind="synthetic",
        trials=tuple(PlannedTrial(
            run_id=variant["run_id"], task_id=task.task_id, repeat_index=data["repeat_index"],
            model_label=profile.profile_id, variant_label=variant["label"],
            conditions=Conditions(**fixed, variant_sha256=fingerprint(variant["configuration"])),
        ) for variant in data["variants"]),
    )
    return SimpleNamespace(profile=profile, task=task, plan=plan, expected_plan_sha256=plan_sha256(plan))


def record_synthetic_trials(case, design):
    """Invent explicit raw records; never map plan completion into task validation."""
    originals, runs = [], []
    for variant in case.data["variants"]:
        result = CodingResult(
            run_id=variant["run_id"], task_id=design.task.task_id, job_id="synthetic-coding-record",
            model=ModelResult(profile=design.profile.profile_id, provider=design.profile.provider,
                              model_id=design.profile.model_id),
            repository=RepositoryResult(), result=ResultStatus.model_validate(variant["invented_result"]),
            metrics=CodingMetrics(wall_time=0.0, files_changed=0, diff_additions=0, diff_deletions=0),
            artifacts=CodingArtifacts(),
        )
        originals.append(result)
        runs.append(make_run(
            result, profile=design.profile, fixture=design.task,
            suite_id="single-case-contract-integration", suite_sha256=case.sha256,
            checker_sha256=CHECKER_SHA256, environment=case.data["environment"],
            budget=case.data["budget"], variant=variant["configuration"],
            variant_label=variant["label"], repeat_index=1, evidence_kind="synthetic",
        ))
    ledger = ExecutionLedger(
        plan_sha256=design.expected_plan_sha256,
        records=tuple(record_execution(run, raw) for run, raw in zip(runs, originals, strict=True)),
    )
    return SimpleNamespace(
        runs=runs, raw_results=originals, ledger=ledger,
        expected_ledger_sha256=execution_ledger_sha256(ledger),
    )


def reconcile(design, evidence, **overrides):
    supplied = {
        "runs": evidence.runs, "raw_results": evidence.raw_results, "ledger": evidence.ledger,
        "expected_plan_sha256": design.expected_plan_sha256,
        "expected_ledger_sha256": evidence.expected_ledger_sha256,
    }
    return reconcile_trials(design.plan, **{**supplied, **overrides})


def test_discovery_actual_report_passage_quote_and_claim_bindings(case):
    raw = research_input(case)
    expected = digest(raw)
    result = normalize(case, raw, expected_sha256=expected)
    output = result.to_dict()
    assert len(output["candidates"]) == 1
    candidate = output["candidates"][0]
    assert candidate["candidate_id"] == case.data["candidate_id"]
    assert candidate["state"] == output["state"] == "proposed"
    assert output["report"]["sha256"] == expected
    selected = candidate["fields"]["evaluation_method"]["claims"][0]
    assert selected["status"] == "report_supported"
    binding = selected["bindings"][0]
    for key in ("claim_id", "source_id", "passage_id", "evidence_id"):
        assert binding[key] == case.data["research"][key]
    text = case.data["research"]["text"]
    assert binding["report_sha256"] == expected
    assert binding["passage_utf8_sha256"] == binding["quote_utf8_sha256"] == digest(text.encode())
    assert binding["quote"] == text
    assert binding["quote_start"] == binding["passage_start"] == 0
    assert binding["quote_end"] == binding["passage_end"] == len(text)
    assert binding["source_body_hash_status"] == "unverified_complete_extracted_body_not_supplied"
    assert output["information_gaps"] == case.data["research"]["information_gaps"]
    assert output["information_gaps_meaning"] == "research_information_only_not_capability_gaps"
    assert "queries" not in output
    for field in ("version", "license", "required_resources", "existing_implementation_delta"):
        assert candidate["fields"][field]["status"] == "unknown"
        assert candidate["fields"][field]["value"] is None
    assert result.sha256 == hashlib.sha256(result.output_bytes).hexdigest()


def test_report_raw_hash_changes_even_when_decoded_report_is_identical(case):
    raw = research_input(case)
    pin = digest(raw)
    reformatted = encode(json.loads(raw))
    assert json.loads(raw) == json.loads(reformatted)
    assert digest(reformatted) != pin
    with pytest.raises(DiscoveryInputError):
        normalize(case, reformatted, expected_sha256=pin)


def test_checked_claim_with_no_support_stays_unresolved(case):
    value = json.loads(research_input(case))
    value["evidence"][0]["relation"] = "context_only"
    raw = raw_json(value)
    output = normalize(case, raw, expected_sha256=digest(raw)).to_dict()
    field = output["candidates"][0]["fields"]["evaluation_method"]
    assert field["claims"][0]["status"] == "unresolved"
    assert field["claims"][0]["bindings"][0]["reported_relation"] == "context_only"


def test_one_case_trial_plan_has_exact_two_variants_and_fixed_conditions(case):
    design = trial_design(case)
    evidence = record_synthetic_trials(case, design)
    report = reconcile(design, evidence)
    assert report["ok"] is True and report["record_integrity_verified"] is True
    assert report["execution_verified"] is False
    assert report["expected_runs"] == report["supplied_runs"] == 2
    assert report["plan_sha256"] == design.expected_plan_sha256
    assert report["execution_ledger_sha256"] == evidence.expected_ledger_sha256
    assert report["comparison"]["axis"] == "variant"
    assert report["comparison"]["evidence_kind"] == "synthetic"
    assert [row["runs"] for row in report["comparison"]["summaries"]] == [1, 1]
    assert [row["succeeded"] for row in report["comparison"]["summaries"]] == [0, 1]
    assert report["validation_unexecuted_run_ids"] == []
    for run, raw, trial in zip(evidence.runs, evidence.raw_results, design.plan.trials, strict=True):
        assert run.conditions == trial.conditions
        assert run.result_sha256 == fingerprint(raw.model_dump(mode="json"))
        raw_bytes = encode(raw.model_dump(mode="json"))
        assert digest(raw_bytes) != run.result_sha256
    fixed = [trial.conditions.model_dump(exclude={"variant_sha256"}) for trial in design.plan.trials]
    assert fixed[0] == fixed[1]


@pytest.mark.parametrize("keep", [[], [0], [1]])
def test_missing_trial_records_never_produce_a_partial_comparison(case, keep):
    design = trial_design(case)
    evidence = record_synthetic_trials(case, design)
    result = reconcile(design, evidence, runs=[evidence.runs[index] for index in keep])
    assert result["ok"] is False and result["comparison"] is None
    assert result["record_integrity_verified"] is False and result["execution_verified"] is False
    assert result["missing_run_ids"] == sorted(
        run.run_id for index, run in enumerate(evidence.runs) if index not in keep
    )


@pytest.mark.parametrize("field", ["checker_sha256", "budget_sha256", "model_sha256",
                                  "environment_sha256", "suite_sha256", "task_sha256"])
def test_trial_fixed_condition_drift_cannot_be_rebound(case, field):
    design = trial_design(case)
    evidence = record_synthetic_trials(case, design)
    data = evidence.runs[1].model_dump(mode="json")
    data["conditions"][field] = "0" * 64
    changed = BenchmarkRun.model_validate(data)
    result = reconcile(design, evidence, runs=[evidence.runs[0], changed])
    assert result["ok"] is False and result["comparison"] is None
    assert result["mismatches"] == [{"run_id": changed.run_id, "fields": ["conditions." + field]}]
    assert result["evidence_mismatches"] == [
        {"run_id": changed.run_id, "fields": ["execution.record_sha256"]},
    ]


def test_raw_result_mutation_missing_raw_and_duplicate_records_are_not_success(case):
    design = trial_design(case)
    evidence = record_synthetic_trials(case, design)
    altered = evidence.raw_results[1].model_dump(mode="json")
    altered["metrics"]["wall_time"] = 1.0
    changed = CodingResult.model_validate(altered)
    result = reconcile(design, evidence, raw_results=[evidence.raw_results[0], changed])
    assert result["ok"] is False and result["comparison"] is None
    assert result["evidence_mismatches"]
    result = reconcile(design, evidence, raw_results=evidence.raw_results[:1])
    assert result["missing_raw_result_ids"] == [evidence.runs[1].run_id]
    assert result["comparison"] is None
    result = reconcile(design, evidence, runs=[*evidence.runs, evidence.runs[1]])
    assert result["duplicate_run_ids"] == [evidence.runs[1].run_id]
    assert result["comparison"] is None


def test_retained_plan_and_ledger_pins_cannot_be_replaced(case):
    design = trial_design(case)
    evidence = record_synthetic_trials(case, design)
    with pytest.raises(ValueError, match="predeclared commitment"):
        reconcile(design, evidence, expected_plan_sha256="0" * 64)
    with pytest.raises(ValueError, match="retained commitment"):
        reconcile(design, evidence, expected_ledger_sha256="0" * 64)


def test_explicit_nonexecution_never_creates_a_result(case):
    design = trial_design(case)
    evidence = record_synthetic_trials(case, design)
    ledger = ExecutionLedger(plan_sha256=design.expected_plan_sha256, records=(
        evidence.ledger.records[0],
        ExecutionRecord(run_id=design.plan.trials[1].run_id, state="not_executed",
                        reason="Synthetic control: candidate was not executed."),
    ))
    ledger_pin = execution_ledger_sha256(ledger)
    result = reconcile(
        design, evidence, runs=evidence.runs[:1], raw_results=evidence.raw_results[:1],
        ledger=ledger, expected_ledger_sha256=ledger_pin,
    )
    assert result["not_executed_run_ids"] == [design.plan.trials[1].run_id]
    assert result["missing_run_ids"] == [design.plan.trials[1].run_id]
    assert result["ok"] is False and result["comparison"] is None
    assert result["execution_verified"] is False


def stopped_campaign(tmp_path, case, *, invalid_initial_freeze=None):
    """Persist synthetic stopped state; invoke no submit, runner or freeze recapture."""
    job_file = tmp_path / "saved-job.json"
    write_json(job_file, {"id": "synthetic-stopped-job", "status": "planner_stopped",
                          "outcome": "planned_only"})
    plan = {
        "specs": [{"runtime": {"backend": "owned_llama"},
                   "contract": {"planner_only": True, "max_repairs": 0, "timeout_seconds": 30}}],
        "max_consecutive_failures": 1, "timeout_seconds": 30,
        "resource_lock": str(tmp_path / "synthetic-local.lock"),
    }
    plan_file = tmp_path / "plan.json"
    state_root = tmp_path / "saved-campaign"
    write_json(plan_file, plan)
    identity = digest(encode(plan))
    freeze = {
        "plan_sha256": identity,
        "verifier_identities": [{"synthetic_fixture_sha256": case.sha256}],
        "observations": [{"evidence_kind": "synthetic", "execution_attested": False}],
    }
    # These controls start with an internally inconsistent checkpoint. The
    # retained hash is fixed from those initial bytes, never refreshed after a
    # submitted checkpoint is mutated.
    if invalid_initial_freeze is not None:
        assert invalid_initial_freeze in {"plan_binding", "identity_count"}
        if invalid_initial_freeze == "plan_binding":
            freeze["plan_sha256"] = "0" * 64
        else:
            freeze["verifier_identities"] = []
    write_json(state_root / "verifier-freeze.json", freeze)
    state = {
        "plan_sha256": identity, "verifier_freeze_sha256": digest(encode(freeze)),
        "status": "stopped", "reason": formal_campaign.PLANNER_STOP_REASON,
        "next_index": 1, "active_job": None, "consecutive_failures": 1,
        "results": [{"job_file": str(job_file), "status": "planner_stopped"}],
        "elapsed_seconds": 0.0,
    }
    write_json(state_root / "campaign.json", state)
    return plan_file, state_root, job_file


def forbid_campaign_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Stopped synthetic campaign attempted execution or identity recapture")
    monkeypatch.setattr(formal_campaign, "submit", forbidden)
    monkeypatch.setattr(formal_campaign, "FormalRunner", forbidden)
    monkeypatch.setattr(formal_campaign, "freeze_submission_identity", forbidden)


def test_existing_campaign_stopped_resume_preserves_state_and_job(tmp_path, monkeypatch, case):
    plan_file, state_root, job_file = stopped_campaign(tmp_path, case)
    state_file = state_root / "campaign.json"
    before = {path: path.read_bytes() for path in (
        plan_file, state_file, job_file, state_root / "verifier-freeze.json",
    )}
    forbid_campaign_execution(monkeypatch)
    result = formal_campaign.run_campaign(plan_file, state_root, resume=True)
    assert result == read_json(state_file)
    assert result["status"] == "stopped" and result["next_index"] == 1
    assert result["reason"] == formal_campaign.PLANNER_STOP_REASON
    assert formal_campaign.proposal_valid(job_file) is False
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("mutation", ["plan", "missing_freeze", "changed_freeze", "freeze_count"])
def test_existing_campaign_rejects_changed_plan_or_freeze(tmp_path, monkeypatch, case, mutation):
    plan_file, state_root, job_file = stopped_campaign(tmp_path, case)
    state_file = state_root / "campaign.json"
    before = (state_file.read_bytes(), job_file.read_bytes())
    freeze_file = state_root / "verifier-freeze.json"
    if mutation == "plan":
        plan = read_json(plan_file)
        plan["timeout_seconds"] = 29
        write_json(plan_file, plan, exclusive=False)
    elif mutation == "missing_freeze":
        freeze_file.unlink()
    else:
        freeze = read_json(freeze_file)
        if mutation == "changed_freeze":
            freeze["plan_sha256"] = "0" * 64
        else:
            freeze["verifier_identities"] = []
        write_json(freeze_file, freeze, exclusive=False)
    forbid_campaign_execution(monkeypatch)
    message = "changed campaign plan" if mutation == "plan" else "cannot recapture"
    with pytest.raises(GateFailure, match=message):
        formal_campaign.run_campaign(plan_file, state_root, resume=True)
    assert (state_file.read_bytes(), job_file.read_bytes()) == before


@pytest.mark.parametrize("invalid_initial_freeze", ["plan_binding", "identity_count"])
def test_campaign_rejects_initial_internal_inconsistency_with_matching_retained_hash(
    tmp_path, monkeypatch, case, invalid_initial_freeze,
):
    plan_file, state_root, job_file = stopped_campaign(
        tmp_path, case, invalid_initial_freeze=invalid_initial_freeze,
    )
    state_file = state_root / "campaign.json"
    freeze_file = state_root / "verifier-freeze.json"
    state, freeze = read_json(state_file), read_json(freeze_file)
    assert state["verifier_freeze_sha256"] == digest(freeze_file.read_bytes())
    assert state["plan_sha256"] == digest(encode(read_json(plan_file)))
    if invalid_initial_freeze == "plan_binding":
        assert freeze["plan_sha256"] != state["plan_sha256"]
        assert len(freeze["verifier_identities"]) == len(read_json(plan_file)["specs"])
    else:
        assert freeze["plan_sha256"] == state["plan_sha256"]
        assert len(freeze["verifier_identities"]) != len(read_json(plan_file)["specs"])
    before = {path: path.read_bytes() for path in (plan_file, state_file, freeze_file, job_file)}
    forbid_campaign_execution(monkeypatch)
    with pytest.raises(GateFailure, match="cannot recapture"):
        formal_campaign.run_campaign(plan_file, state_root, resume=True)
    assert {path: path.read_bytes() for path in before} == before


def assert_planner_report(saved, report):
    """Check the actual saved-output adapter result, not an invented verdict."""
    assert report["schema"] == "ephy.planner-completion-evidence.v1"
    assert report["assurance"] == "structural_only" and report["checks_executed"] is False
    for field in ("semantic_correctness", "workflow_validity", "provenance", "formal_adoption"):
        assert report[field] == "not_assessed"
    assert report["source_bindings_valid"] is True
    assert report["job_id"] == saved.pins["expected_job_id"]
    assert report["job_contract_sha256"] == saved.pins["expected_job_contract_sha256"]
    for source_name, argument in (
        ("completion-contract", "contract_bytes"), ("job.json", "job_bytes"),
        ("planner-session.jsonl", "session_bytes"), ("lead-plan.txt", "plan_bytes"),
        ("planner-stop.json", "stop_bytes"),
    ):
        raw = saved.inputs[argument]
        assert report["source_files"][source_name] == {"sha256": digest(raw), "bytes": len(raw)}
    assert report["terminal_record_sha256"] == digest(raw_json(saved.terminal))
    assert report["terminal_line"] == 2
    checker_input = json.dumps(saved.message, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
    assert report["checker_input_sha256"] == digest(checker_input)
    assert report["checker"]["response_sha256"] == digest(checker_input)
    assert report["original_text_sha256"] == digest(saved.text.encode("utf-8"))
    assert report["lead_plan_representation"] == "exact_utf8"
    assert report["checker"]["contract_sha256"] == saved.pins["expected_contract_sha256"]


def native_packet(case, planner_reports, *, trial_report=None, design=None, evidence=None):
    """Freeze raw native reports and caller-held expectations before submission.

    Caller context labels are deliberately distinct from producer job/run IDs.
    This does not convert a native schema into synthetic_observations.
    """
    artifacts, reports = [], {}
    for label, saved, report in planner_reports:
        identity = case.data["native_inputs"][label]
        raw = raw_json(report)
        assert identity["context_id"] != saved.pins["expected_job_id"]
        artifacts.append({
            **identity, "source_origin": case.data["native_inputs"]["source_origin"],
            "sha256": digest(raw), "byte_length": len(raw),
            "expected": {
                "job_id": saved.pins["expected_job_id"],
                "job_contract_sha256": saved.pins["expected_job_contract_sha256"],
                "contract_sha256": saved.pins["expected_contract_sha256"],
            },
        })
        reports[identity["artifact_id"]] = raw
    if trial_report is not None:
        identity = case.data["native_inputs"]["trials"]
        raw = raw_json(trial_report)
        artifacts.append({
            **identity, "source_origin": case.data["native_inputs"]["source_origin"],
            "sha256": digest(raw), "byte_length": len(raw),
            "expected": {
                "plan_id": design.plan.plan_id, "plan_sha256": design.expected_plan_sha256,
                "execution_ledger_sha256": evidence.expected_ledger_sha256,
                "axis": design.plan.axis, "evidence_kind": design.plan.evidence_kind,
            },
        })
        reports[identity["artifact_id"]] = raw
    manifest = raw_json({"schema": "ephy.capability-native-manifest.v1", "artifacts": artifacts})
    return SimpleNamespace(raw=manifest, expected_manifest_sha256=digest(manifest), reports=reports)


def test_planned_single_case_connects_actual_p0_native_gap_discovery_and_trials(case, tmp_path):
    # These dependencies are intentionally not mocked or replaced when absent.
    # Owner final acceptance and exact source transport must precede any execution.
    from ephy_worker.capability_gap_native import derive_native_gap_candidates
    from ephy_worker.plan_completion_adapter import check_planner_only_output

    design = trial_design(case)
    baseline = saved_planner_inputs(case, "baseline")
    baseline_report = check_planner_only_output(**baseline.inputs, **baseline.pins)
    assert_planner_report(baseline, baseline_report)
    assert baseline_report["status"] == "incomplete" and baseline_report["completed"] is False
    assert baseline_report["checker"]["passed"] is False
    assert baseline_report["diagnostics"][0]["code"] == "check_coverage"
    packet = native_packet(case, [("baseline", baseline, baseline_report)])
    native = derive_native_gap_candidates(
        packet.raw, packet.reports, expected_manifest_sha256=packet.expected_manifest_sha256,
    )
    assert_native_evidence(packet, native)
    assert native["schema"] == "ephy.capability-native-gap-report.v1"
    assert native["ruleset"] == "native-p0-report-projection.v1"
    assert native["evidence_basis"] == "caller_pinned_native_reports"
    assert native["manifest_sha256"] == packet.expected_manifest_sha256
    assert native["assurance"]["raw_report_bindings_verified"] is True
    assert native["assurance"]["source_bytes_rechecked"] is False
    for key in ("producer_provenance", "execution", "semantic_correctness",
                "real_model_improvement", "formal_adoption"):
        assert native["assurance"][key] == "not_assessed"
    assert len(native["gap_candidates"]) == 1
    gap = native["gap_candidates"][0]
    assert gap["category"] == "workflow"
    assert gap["investigation_area"] == "planning_contract"
    assert gap["status"] == "unverified"
    assert gap["supporting_observation_ids"] and gap["evidence_ids"]
    assert gap["contradicting_observation_ids"] == []
    supporting = [item for item in native["observations"]
                  if item["observation_id"] in gap["supporting_observation_ids"]]
    assert {item["observation_id"] for item in supporting} == set(gap["supporting_observation_ids"])
    assert {item["evidence_id"] for item in supporting} == set(gap["evidence_ids"])
    assert all(item["category"] == "workflow" and item["status"] == "failed" for item in supporting)
    assert all(item["status"] == "unverified" for item in native["cause_hypotheses"])

    # The explicit caller mapping is a synthetic lineage record, not a claim
    # that the Research Report diagnosed the Gap or authenticated a producer.
    report_raw = research_input(case)
    report_pin = digest(report_raw)
    native_raw = raw_json(native)
    lineage = {
        "schema": case.data["pending_connection"]["lineage_schema"],
        "native_manifest_sha256": packet.expected_manifest_sha256,
        "native_gap_report_sha256": digest(native_raw),
        "native_report_sha256": digest(packet.reports[case.data["native_inputs"]["baseline"]["artifact_id"]]),
        "gap_candidate_id": gap["candidate_id"],
        "supporting_observation_ids": gap["supporting_observation_ids"],
        "evidence_ids": gap["evidence_ids"],
        "research_report_sha256": report_pin, "research_job_id": case.data["research"]["job_id"],
        "selection": copy.deepcopy(case.data["research"]["selection"]),
    }
    lineage_path = tmp_path / "synthetic-lineage.json"
    write_json(lineage_path, lineage)
    lineage_bytes = lineage_path.read_bytes()
    submitted_lineage = read_json(lineage_path)
    # Normalize from the previously retained fixture Report pin and selection,
    # not expectations recomputed from a submitted lineage object.
    discovery = normalize(case, report_raw, expected_sha256=report_pin,
                          selections=[copy.deepcopy(case.data["research"]["selection"])]).to_dict()
    assert_lineage_bindings(
        submitted_lineage, packet=packet, native=native, research_raw=report_raw,
        expected_selection=case.data["research"]["selection"], discovery=discovery,
    )
    selection = submitted_lineage["selection"]
    assert len(discovery["candidates"]) == 1
    proposed = discovery["candidates"][0]
    assert proposed["candidate_id"] == case.data["candidate_id"] == selection["candidate_id"]
    assert proposed["state"] == discovery["state"] == "proposed"
    claim = proposed["fields"]["evaluation_method"]["claims"][0]
    assert claim["status"] == "report_supported"
    binding = claim["bindings"][0]
    assert binding["report_sha256"] == lineage["research_report_sha256"]
    for key in ("claim_id", "source_id", "passage_id", "evidence_id"):
        assert binding[key] == case.data["research"][key]
    assert selection["claims_by_field"]["evaluation_method"] == [binding["claim_id"]]
    assert binding["quote_utf8_sha256"] == digest(case.data["research"]["text"].encode())
    assert discovery["information_gaps_meaning"] == "research_information_only_not_capability_gaps"

    candidate = saved_planner_inputs(case, "candidate")
    candidate_report = check_planner_only_output(**candidate.inputs, **candidate.pins)
    assert_planner_report(candidate, candidate_report)
    assert candidate_report["status"] == "complete" and candidate_report["completed"] is True
    assert candidate_report["checker"]["passed"] is True
    assert candidate.inputs["contract_bytes"] == baseline.inputs["contract_bytes"]
    assert candidate.pins["expected_job_contract_sha256"] == baseline.pins["expected_job_contract_sha256"]
    evidence = record_synthetic_trials(case, design)
    trials = reconcile(design, evidence)
    assert trials["ok"] is True and trials["record_integrity_verified"] is True
    assert trials["expected_runs"] == trials["supplied_runs"] == 2
    assert trials["execution_verified"] is False
    assert trials["comparison"]["evidence_kind"] == "synthetic"

    combined = native_packet(
        case, [("baseline", baseline, baseline_report), ("candidate", candidate, candidate_report)],
        trial_report=trials, design=design, evidence=evidence,
    )
    manifest = json.loads(combined.raw)
    assert len({item["context_id"] for item in manifest["artifacts"]}) == 3
    assert [item["source_family"] for item in manifest["artifacts"]] == [
        "planner_completion", "planner_completion", "trial_reconciliation",
    ]
    assert {item["source_origin"] for item in manifest["artifacts"]} == {"test_fixture"}
    projected = derive_native_gap_candidates(
        combined.raw, combined.reports, expected_manifest_sha256=combined.expected_manifest_sha256,
    )
    assert_native_evidence(combined, projected)
    assert projected["manifest_sha256"] == combined.expected_manifest_sha256
    assert projected["evidence_basis"] == "caller_pinned_native_reports"
    assert len(projected["evidence"]) == 3
    assert len(projected["gap_candidates"]) == 1
    assert projected["gap_candidates"][0]["candidate_id"] == gap["candidate_id"]
    planning = [item for item in projected["observations"] if item["code"] == "planning_contract"]
    assert [item["status"] for item in planning] == ["failed", "passed"]
    assert len({item["check_key"] for item in planning}) == 2
    assert not any(item["code"] == "conflicting_observations" for item in projected["unknowns"])
    assert all(item["status"] == "unverified" for item in projected["gap_candidates"])
    assert not any(item["category"] in {"task_performance", "environment"}
                   for item in projected["observations"])
    assert lineage_path.read_bytes() == lineage_bytes
    assert digest(native_raw) == lineage["native_gap_report_sha256"]
    assert read_json(lineage_path)["gap_candidate_id"] == gap["candidate_id"]


@pytest.mark.parametrize("text,expected_status", [("ACK\n", "invalid_input"), ("{}", "incomplete"),
                                                  ("NO_HYPOTHESIS", "no_hypothesis")])
def test_saved_plan_negative_outcomes_are_preserved(case, text, expected_status):
    from ephy_worker.plan_completion_adapter import check_planner_only_output

    saved = saved_planner_inputs(case, "candidate", text=text)
    report = check_planner_only_output(**saved.inputs, **saved.pins)
    assert_planner_report(saved, report)
    assert report["completed"] is False and report["status"] == expected_status
    assert report["checker"]["passed"] is False


def test_saved_plan_bad_terminal_record_does_not_fall_back_to_good_record(case):
    from ephy_worker.plan_completion_adapter import check_planner_only_output

    saved = saved_planner_inputs(case, "candidate")
    altered = dict(saved.inputs)
    altered["session_bytes"] += raw_json({"type": "message", "message": {"role": "user", "content": []}}) + b"\n"
    report = check_planner_only_output(**altered, **saved.pins)
    assert report["completed"] is False and report["source_bindings_valid"] is False
    assert report["status"] == "invalid_source" and report["checks_executed"] is False


@pytest.mark.parametrize("mutation", ["report_bytes", "manifest_bytes", "extra_report"])
def test_native_raw_bindings_fail_closed_without_repinning(case, mutation):
    from ephy_worker.capability_gap_native import NativeGapInputError, derive_native_gap_candidates
    from ephy_worker.plan_completion_adapter import check_planner_only_output

    baseline = saved_planner_inputs(case, "baseline")
    report = check_planner_only_output(**baseline.inputs, **baseline.pins)
    packet = native_packet(case, [("baseline", baseline, report)])
    submitted_manifest, submitted_reports = packet.raw, dict(packet.reports)
    artifact_id = case.data["native_inputs"]["baseline"]["artifact_id"]
    if mutation == "report_bytes":
        submitted_reports[artifact_id] = encode(json.loads(submitted_reports[artifact_id]))
        assert json.loads(submitted_reports[artifact_id]) == report
    elif mutation == "manifest_bytes":
        submitted_manifest = encode(json.loads(packet.raw))
        assert json.loads(submitted_manifest) == json.loads(packet.raw)
    else:
        submitted_reports["undeclared-extra"] = b"{}"
    with pytest.raises(NativeGapInputError):
        derive_native_gap_candidates(
            submitted_manifest, submitted_reports, expected_manifest_sha256=packet.expected_manifest_sha256,
        )


def assert_native_evidence(packet, native):
    """Assert the declared native projection against its actual raw input bytes."""
    manifest = json.loads(packet.raw)
    assert native["manifest_sha256"] == packet.expected_manifest_sha256 == digest(packet.raw)
    assert len(native["evidence"]) == len(manifest["artifacts"])
    evidence_ids = set()
    exact_fields = {
        "evidence_id", "artifact_id_sha256", "context_id_sha256", "source_family", "source_origin",
        "native_schema", "raw_report_sha256", "raw_report_byte_length", "evidence_kind",
        "bindings", "reported_commitments",
    }
    for entry, actual in zip(manifest["artifacts"], native["evidence"], strict=True):
        raw = packet.reports[entry["artifact_id"]]
        report = json.loads(raw)
        assert set(actual) == exact_fields
        # These are canonical JSON identity fingerprints, not raw UTF-8 hashes.
        assert actual["artifact_id_sha256"] == fingerprint(entry["artifact_id"])
        assert actual["context_id_sha256"] == fingerprint(entry["context_id"])
        assert actual["evidence_id"] == "e-" + fingerprint(entry["artifact_id"])
        evidence_ids.add(actual["evidence_id"])
        assert actual["source_family"] == entry["source_family"]
        assert actual["source_origin"] == entry["source_origin"] == "test_fixture"
        assert actual["raw_report_sha256"] == entry["sha256"] == digest(raw)
        assert actual["raw_report_byte_length"] == entry["byte_length"] == len(raw)
        expected = entry["expected"]
        if entry["source_family"] == "planner_completion":
            assert actual["native_schema"] == report["schema"] == "ephy.planner-completion-evidence.v1"
            assert actual["evidence_kind"] == "not_reported"
            values = {
                "job_id_sha256": (fingerprint(expected["job_id"]), fingerprint(report["job_id"])),
                "canonical_runtime_contract_sha256": (
                    expected["job_contract_sha256"], report["job_contract_sha256"],
                ),
                "raw_completion_contract_sha256": (
                    expected["contract_sha256"], report["source_files"]["completion-contract"]["sha256"],
                ),
            }
            assert set(actual["bindings"]) == set(values)
            for name, (intended, observed) in values.items():
                assert actual["bindings"][name] == {
                    "intended": intended, "observed": observed, "comparison": "match",
                }
                assert intended == observed
            assert actual["reported_commitments"] == {
                "canonical_runtime_contract_sha256": report["job_contract_sha256"],
                "snapshot_sha256": report["snapshot_sha256"],
                "raw_source_files": report["source_files"],
                "raw_terminal_record_sha256": report["terminal_record_sha256"],
                "terminal_line": report["terminal_line"],
                "reserialized_checker_input_sha256": report["checker_input_sha256"],
                "raw_original_text_sha256": report["original_text_sha256"],
                "lead_plan_representation": report["lead_plan_representation"],
                "nested_core": {name: report["checker"][name] for name in (
                    "status", "passed", "contract_sha256", "response_sha256", "plan_sha256",
                )},
            }
        else:
            assert entry["source_family"] == "trial_reconciliation"
            assert actual["native_schema"] == "trial_reconciliation.schema_version.1"
            assert actual["evidence_kind"] == expected["evidence_kind"] == report["evidence_kind"] == "synthetic"
            expected_bindings = {}
            for name, intended in expected.items():
                output_name = "plan_id_sha256" if name == "plan_id" else name
                observed = report[name]
                if name == "plan_id":
                    intended, observed = fingerprint(intended), fingerprint(observed)
                expected_bindings[output_name] = {
                    "intended": intended, "observed": observed, "comparison": "match",
                }
                assert intended == observed
            assert actual["bindings"] == expected_bindings
            assert actual["reported_commitments"] == {
                "canonical_plan_sha256": report["plan_sha256"],
                "canonical_execution_ledger_sha256": report["execution_ledger_sha256"],
                "comparison_available": report["comparison"] is not None,
                "canonical_comparison_subobject_sha256": (
                    fingerprint(report["comparison"]) if report["comparison"] is not None else None
                ),
            }
    for observation in native["observations"]:
        assert set(observation) == {
            "observation_id", "evidence_id", "check_key", "category", "code", "stage", "status",
            "observation_basis", "diagnostic_codes", "detail_refs",
        }
        assert observation["evidence_id"] in evidence_ids
        assert observation["observation_basis"] == "reported"


def assert_lineage_bindings(lineage, *, packet, native, research_raw, expected_selection, discovery):
    """Test-only assertions on actual artifact edges; no adoption/decision API."""
    assert_native_evidence(packet, native)
    assert lineage["schema"] == "ephy.synthetic-improvement-lineage.v1", "lineage_schema"
    assert lineage["native_manifest_sha256"] == packet.expected_manifest_sha256, "lineage_manifest"
    assert lineage["native_gap_report_sha256"] == digest(raw_json(native)), "lineage_gap_report"
    candidates = [item for item in native["gap_candidates"] if item["candidate_id"] == lineage["gap_candidate_id"]]
    assert len(candidates) == 1, "lineage_gap_candidate"
    gap = candidates[0]
    assert lineage["supporting_observation_ids"] == gap["supporting_observation_ids"], "lineage_observations"
    assert lineage["evidence_ids"] == gap["evidence_ids"], "lineage_evidence"
    observations = [item for item in native["observations"]
                    if item["observation_id"] in gap["supporting_observation_ids"]]
    assert [item["observation_id"] for item in observations] == gap["supporting_observation_ids"]
    assert {item["evidence_id"] for item in observations} == set(gap["evidence_ids"])
    assert all(item["observation_basis"] == "reported" and item["status"] == "failed" for item in observations)
    evidence = [item for item in native["evidence"] if item["evidence_id"] in gap["evidence_ids"]]
    assert len(evidence) == 1, "lineage_evidence_count"
    assert lineage["native_report_sha256"] == evidence[0]["raw_report_sha256"], "lineage_native_report"
    assert lineage["native_report_sha256"] in {digest(raw) for raw in packet.reports.values()}, "lineage_source"
    assert lineage["research_report_sha256"] == digest(research_raw), "lineage_research_report"
    report = json.loads(research_raw)
    assert lineage["research_job_id"] == report["job_id"], "lineage_research_job"
    assert lineage["selection"] == expected_selection, "lineage_selection"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    raw_evidence = {item["evidence_id"]: item for item in report["evidence"]}
    sources = {source["source_id"]: source for source in report["sources"]}
    assert discovery["report"]["sha256"] == lineage["research_report_sha256"], "lineage_discovery_report"
    assert discovery["report"]["byte_length"] == len(research_raw)
    assert discovery["report"]["job_id"] == lineage["research_job_id"]
    assert len(discovery["candidates"]) == 1, "candidate_count"
    candidate = discovery["candidates"][0]
    assert candidate["candidate_id"] == expected_selection["candidate_id"]
    assert candidate["state"] == discovery["state"] == "proposed"
    for field, ids in expected_selection["claims_by_field"].items():
        selected = candidate["fields"][field]["claims"]
        assert {item["claim_id"] for item in selected} == set(ids), "lineage_discovery_claim"
        assert ids and set(ids) <= set(claims), "lineage_claim_reference"
        for claim in selected:
            original = claims[claim["claim_id"]]
            assert claim["text"] == original["text"] and claim["status"] == "report_supported"
            assert {item["evidence_id"] for item in claim["bindings"]} == set(original["evidence_ids"])
            for binding in claim["bindings"]:
                item = raw_evidence[binding["evidence_id"]]
                assert binding["report_sha256"] == lineage["research_report_sha256"]
                assert binding["claim_id"] == claim["claim_id"]
                assert binding["source_id"] == item["source_id"], "lineage_discovery_source"
                assert binding["passage_id"] == item["passage_id"], "lineage_discovery_passage"
                source = sources[item["source_id"]]
                passages = {value["passage_id"]: value for value in source["passages"]}
                passage = passages[item["passage_id"]]
                assert passage["source_id"] == item["source_id"]
                assert binding["passage_utf8_sha256"] == digest(passage["text"].encode("utf-8"))
                assert binding["quote_utf8_sha256"] == digest(item["quote"].encode("utf-8")), "lineage_discovery_quote"
                assert binding["quote"] == item["quote"]
                assert binding["quote_start"] == item["quote_start"]
                assert binding["quote_end"] == item["quote_end"]
                left, right = item["quote_start"] - passage["start"], item["quote_end"] - passage["start"]
                assert passage["text"][left:right] == item["quote"]


def test_same_context_and_intended_pins_preserve_conflict_and_suppress_gap(case):
    from ephy_worker.capability_gap_native import derive_native_gap_candidates
    from ephy_worker.plan_completion_adapter import check_planner_only_output

    control = case.data["native_inputs"]["conflict_control"]
    data = copy.deepcopy(case.data)
    for label in ("baseline", "candidate"):
        data["native_inputs"][label]["context_id"] = control["context_id"]
    same_context = SimpleNamespace(data=data, raw=case.raw, sha256=case.sha256)
    baseline = saved_planner_inputs(same_context, "baseline", job_id=control["job_id"])
    candidate = saved_planner_inputs(same_context, "candidate", job_id=control["job_id"])
    assert baseline.pins == candidate.pins
    baseline_report = check_planner_only_output(**baseline.inputs, **baseline.pins)
    candidate_report = check_planner_only_output(**candidate.inputs, **candidate.pins)
    assert_planner_report(baseline, baseline_report)
    assert_planner_report(candidate, candidate_report)
    assert baseline_report["status"] == "incomplete" and candidate_report["status"] == "complete"
    packet = native_packet(same_context, [("baseline", baseline, baseline_report),
                                         ("candidate", candidate, candidate_report)])
    native = derive_native_gap_candidates(
        packet.raw, packet.reports, expected_manifest_sha256=packet.expected_manifest_sha256,
    )
    assert_native_evidence(packet, native)
    observations = [item for item in native["observations"] if item["code"] == "planning_contract"]
    assert len(observations) == 2
    assert [item["status"] for item in observations] == ["failed", "passed"]
    expected_key = "k-" + fingerprint([
        control["context_id"], ["planning_contract", baseline.pins["expected_contract_sha256"]],
    ])
    assert {item["check_key"] for item in observations} == {expected_key}
    conflicts = [item for item in native["unknowns"] if item["code"] == "conflicting_observations"]
    assert conflicts == [{
        "code": "conflicting_observations", "check_key": expected_key,
        "observation_ids": [item["observation_id"] for item in observations],
        "evidence_ids": [item["evidence_id"] for item in observations], "detail_refs": [],
    }]
    assert native["gap_candidates"] == [] and native["cause_hypotheses"] == []
    assert len(native["evidence"]) == len(packet.reports) == 2


@pytest.mark.parametrize("mutation,reason", [
    ("native_manifest_sha256", "lineage_manifest"),
    ("native_gap_report_sha256", "lineage_gap_report"),
    ("native_report_sha256", "lineage_native_report"),
    ("gap_candidate_id", "lineage_gap_candidate"),
    ("supporting_observation_ids", "lineage_observations"),
    ("evidence_ids", "lineage_evidence"),
    ("research_report_sha256", "lineage_research_report"),
    ("research_job_id", "lineage_research_job"),
    ("selection", "lineage_selection"),
    ("research_raw_bytes", "lineage_research_report"),
    ("discovery_source_id", "lineage_discovery_source"),
    ("discovery_passage_id", "lineage_discovery_passage"),
    ("discovery_quote_hash", "lineage_discovery_quote"),
])
def test_gap_to_report_lineage_mismatch_is_rejected(case, mutation, reason):
    from ephy_worker.capability_gap_native import derive_native_gap_candidates
    from ephy_worker.plan_completion_adapter import check_planner_only_output

    baseline = saved_planner_inputs(case, "baseline")
    baseline_report = check_planner_only_output(**baseline.inputs, **baseline.pins)
    packet = native_packet(case, [("baseline", baseline, baseline_report)])
    native = derive_native_gap_candidates(
        packet.raw, packet.reports, expected_manifest_sha256=packet.expected_manifest_sha256,
    )
    assert_native_evidence(packet, native)
    assert len(native["gap_candidates"]) == 1
    gap = native["gap_candidates"][0]
    research_raw = research_input(case)
    lineage = {
        "schema": "ephy.synthetic-improvement-lineage.v1",
        "native_manifest_sha256": packet.expected_manifest_sha256,
        "native_gap_report_sha256": digest(raw_json(native)),
        "native_report_sha256": digest(next(iter(packet.reports.values()))),
        "gap_candidate_id": gap["candidate_id"],
        "supporting_observation_ids": copy.deepcopy(gap["supporting_observation_ids"]),
        "evidence_ids": copy.deepcopy(gap["evidence_ids"]),
        "research_report_sha256": digest(research_raw),
        "research_job_id": case.data["research"]["job_id"],
        "selection": copy.deepcopy(case.data["research"]["selection"]),
    }
    # Retain the valid crossing-artifact bindings before changing submitted copies.
    frozen_lineage = raw_json(lineage)
    expected_selection = copy.deepcopy(case.data["research"]["selection"])
    report_pin = lineage["research_report_sha256"]
    discovery = normalize(case, research_raw, expected_sha256=report_pin,
                          selections=[copy.deepcopy(expected_selection)]).to_dict()
    frozen_discovery = raw_json(discovery)
    assert_lineage_bindings(lineage, packet=packet, native=native, research_raw=research_raw,
                            expected_selection=expected_selection, discovery=discovery)
    submitted, submitted_research = copy.deepcopy(lineage), research_raw
    submitted_discovery = copy.deepcopy(discovery)
    if mutation.endswith("sha256"):
        submitted[mutation] = "0" * 64
    elif mutation in {"gap_candidate_id", "research_job_id"}:
        submitted[mutation] = "unrelated-identity"
    elif mutation in {"supporting_observation_ids", "evidence_ids"}:
        submitted[mutation] = ["unrelated-identity"]
    elif mutation == "selection":
        submitted["selection"]["claims_by_field"]["evaluation_method"] = ["unrelated-claim"]
    elif mutation.startswith("discovery_"):
        binding = submitted_discovery["candidates"][0]["fields"]["evaluation_method"]["claims"][0]["bindings"][0]
        field = {"discovery_source_id": "source_id", "discovery_passage_id": "passage_id",
                 "discovery_quote_hash": "quote_utf8_sha256"}[mutation]
        binding[field] = "0" * 64 if field.endswith("sha256") else "unrelated-identity"
    else:
        submitted_research = encode(json.loads(research_raw))
        assert json.loads(submitted_research) == json.loads(research_raw)
        assert digest(submitted_research) != lineage["research_report_sha256"]
    with pytest.raises(AssertionError, match=reason):
        assert_lineage_bindings(submitted, packet=packet, native=native, research_raw=submitted_research,
                                expected_selection=expected_selection, discovery=submitted_discovery)
    assert raw_json(lineage) == frozen_lineage
    assert raw_json(discovery) == frozen_discovery
    assert expected_selection == case.data["research"]["selection"]


def test_more_than_one_candidate_is_rejected_before_discovery_evaluation(case):
    raw = research_input(case)
    pin = digest(raw)
    second = copy.deepcopy(case.data["research"]["selection"])
    second["candidate_id"] = "out-of-scope-second-candidate"
    with pytest.raises(AssertionError, match="candidate_count"):
        normalize(case, raw, expected_sha256=pin, selections=[case.data["research"]["selection"], second])
