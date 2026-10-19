"""Synthetic reconciliation controls only; no model execution or improvement claim."""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from ephy_worker.benchmark import BenchmarkRun, Conditions, compare_runs, fingerprint, make_run
from ephy_worker.coding_profiles import load_coding_profiles
from ephy_worker.coding_schema import (
    CodingArtifacts,
    CodingFailure,
    CodingMetrics,
    CodingResult,
    ModelResult,
    RepositoryResult,
    ResultStatus,
)
from ephy_worker.transfer_evaluation import comparison_plan, freeze_transfer_suite, variant_manifest
from ephy_worker.trial_reconciliation import (
    ExecutionLedger,
    ExecutionRecord,
    PlannedTrial,
    TrialPlan,
    execution_ledger_sha256,
    main,
    make_transfer_trial_plan,
    plan_sha256,
    reconcile_trials,
    record_execution,
)


def synthetic_plan():
    return TrialPlan(
        plan_id="synthetic-control",
        axis="model",
        evidence_kind="synthetic",
        trials=tuple(
            PlannedTrial(
                run_id=f"{model}-{task}-{repeat}",
                task_id=task,
                repeat_index=repeat,
                model_label=model,
                variant_label="none",
                conditions=Conditions(
                    suite_id="synthetic-suite",
                    suite_sha256="a" * 64,
                    task_sha256=fingerprint(task),
                    checker_sha256="b" * 64,
                    environment_sha256="c" * 64,
                    budget_sha256="d" * 64,
                    model_sha256=fingerprint(model),
                    variant_sha256="e" * 64,
                ),
            )
            for model in ("model-a", "model-b")
            for task in ("task-a", "task-b")
            for repeat in (1, 2)
        ),
    )


def synthetic_results(plan):
    runs = [
        BenchmarkRun(
            **trial.model_dump(),
            evidence_kind=plan.evidence_kind,
            result_sha256=fingerprint({"synthetic_run": trial.run_id}),
            status="succeeded",
            validation_passed=True,
            exit_code=0,
            wall_time=float(index + 1),
        )
        for index, trial in enumerate(plan.trials)
    ]
    return [changed(run, result_sha256=fingerprint(synthetic_raw(run).model_dump(mode="json"))) for run in runs]


def changed(run, **fields):
    return BenchmarkRun.model_validate({**run.model_dump(mode="json"), **fields})


def synthetic_raw(run):
    """Invent a raw result only for synthetic controls, never as execution evidence."""
    failure = None
    if run.failure_code is not None:
        failure = CodingFailure(code=run.failure_code, stage=run.failure_stage)
    return CodingResult(
        run_id=run.run_id, task_id=run.task_id, job_id="synthetic-job",
        model=ModelResult(profile=run.model_label, provider="synthetic", model_id=run.model_label),
        repository=RepositoryResult(),
        result=ResultStatus(
            status=run.status, validation_passed=run.validation_passed,
            exit_code=run.exit_code, failure=failure,
        ),
        metrics=CodingMetrics(
            wall_time=run.wall_time, files_changed=1, diff_additions=1, diff_deletions=0,
        ),
        artifacts=CodingArtifacts(),
    )


def retained_evidence(plan, runs, raw_results=None):
    """Freeze synthetic commitments before an attack; never call after mutation."""
    originals = [synthetic_raw(run) for run in runs] if raw_results is None else raw_results
    receipts = {}
    for run, result in zip(runs, originals, strict=True):
        # Metadata duplicate controls retain duplicated inputs; the trusted
        # recorder has one receipt per ID and never overwrites the first receipt.
        receipts.setdefault(run.run_id, record_execution(run, result))
    ledger = ExecutionLedger(plan_sha256=plan_sha256(plan), records=tuple(receipts.values()))
    return {
        "expected_plan_sha256": plan_sha256(plan),
        "ledger": ledger,
        "raw_results": originals,
        "expected_ledger_sha256": execution_ledger_sha256(ledger),
    }


def reconcile(plan, runs):
    """Prepare invented evidence for the original metadata controls only."""
    # Python-mode validation preserves bool/model_copy attacks instead of coercing.
    runs = [BenchmarkRun.model_validate(run.model_dump()) for run in runs]
    originals = [synthetic_raw(run) for run in runs]
    rebound = [
        changed(run, result_sha256=fingerprint(result.model_dump(mode="json")))
        for run, result in zip(runs, originals, strict=True)
    ]
    return reconcile_trials(plan, rebound, **retained_evidence(plan, rebound, originals))


def assert_incomplete(report):
    assert report["ok"] is False
    assert report["comparison"] is None
    assert report["record_integrity_verified"] is False
    assert report["execution_verified"] is False


def write_cli_inputs(tmp_path, plan, runs, evidence):
    """Save independently committed synthetic inputs before changing any file."""
    paths = [tmp_path / name for name in ("plan.json", "records.json", "raw.json", "ledger.json")]
    values = [
        plan.model_dump(mode="json"),
        [run.model_dump(mode="json") for run in runs],
        [result.model_dump(mode="json") for result in evidence["raw_results"]],
        evidence["ledger"].model_dump(mode="json"),
    ]
    for path, value in zip(paths, values, strict=True):
        path.write_text(json.dumps(value), encoding="utf-8")
    return paths, [
        *(str(path) for path in paths),
        "--plan-sha256", evidence["expected_plan_sha256"],
        "--ledger-sha256", evidence["expected_ledger_sha256"],
    ]


def test_complete_results_preserve_exact_existing_comparison_and_inputs():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    before = copy.deepcopy((plan, runs))
    report = reconcile(plan, list(reversed(runs)))
    assert report["ok"] is True
    assert report["comparison"] == compare_runs(runs)
    assert report["expected_runs"] == report["supplied_runs"] == 8
    assert report["plan_sha256"] == plan_sha256(plan)
    assert before == (plan, runs)
    assert report["evidence_kind"] == "synthetic"


@pytest.mark.parametrize("omit", ["one", "both_conditions", "entire_task", "all"])
def test_missing_trials_never_produce_partial_statistics(omit):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    kept = {
        "one": runs[1:],
        "both_conditions": [r for r in runs if not (r.task_id == "task-a" and r.repeat_index == 2)],
        "entire_task": [r for r in runs if r.task_id != "task-a"],
        "all": [],
    }[omit]
    if omit in ("both_conditions", "entire_task"):
        assert compare_runs(kept)["summaries"]  # Demonstrate the pre-existing blind spot.
    report = reconcile(plan, kept)
    assert report["ok"] is False
    assert report["comparison"] is None
    assert report["missing_run_ids"] == sorted({r.run_id for r in runs} - {r.run_id for r in kept})
    assert report["supplied_runs"] == len(kept)


def test_duplicate_ids_and_duplicate_trial_with_new_id_cannot_overwrite_results():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    for duplicate in (runs[0], changed(runs[0], run_id="extra-id")):
        report = reconcile(plan, runs + [duplicate])
        assert report["ok"] is False
        assert report["comparison"] is None
        assert report["duplicate_trials"] == [
            {"group": "model-a", "task_id": "task-a", "repeat_index": 1, "count": 2}
        ]
    report = reconcile(plan, runs + [runs[0]])
    assert report["duplicate_run_ids"] == [runs[0].run_id]


@pytest.mark.parametrize("fields", [
    {"run_id": "replacement-id"},
    {"task_id": "unexpected-task"},
    {"repeat_index": 3},
    {"model_label": "another-model"},
    {"variant_label": "another-variant"},
    {"evidence_kind": "live"},
])
def test_id_labels_repeat_and_evidence_drift_are_rejected(fields):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    report = reconcile(plan, [changed(runs[0], **fields), *runs[1:]])
    assert report["ok"] is False
    assert report["comparison"] is None
    if "run_id" in fields:
        assert report["missing_run_ids"] == [runs[0].run_id]
        assert report["unexpected_run_ids"] == ["replacement-id"]
    else:
        assert report["mismatches"] == [{"run_id": runs[0].run_id, "fields": sorted(fields)}]


@pytest.mark.parametrize("field", list(Conditions.model_fields))
def test_every_condition_is_bound_to_the_plan_including_budget(field):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    value = "different-suite" if field == "suite_id" else "f" * 64
    runs[0] = changed(runs[0], conditions={**runs[0].conditions.model_dump(), field: value})
    report = reconcile(plan, runs)
    assert not report["ok"]
    assert report["mismatches"][0]["fields"] == ["conditions." + field]
    assert report["comparison"] is None


def test_shared_budget_drift_cannot_hide_behind_matching_result_conditions():
    plan = synthetic_plan()
    runs = [
        changed(run, conditions={**run.conditions.model_dump(), "budget_sha256": "f" * 64})
        for run in synthetic_results(plan)
    ]
    assert compare_runs(runs)["summaries"]
    assert len(reconcile(plan, runs)["mismatches"]) == len(runs)


def test_extra_balanced_trials_and_groups_are_rejected():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    extras = [changed(r, run_id=r.run_id + "-extra", repeat_index=3) for r in runs if r.repeat_index == 2]
    assert compare_runs(runs + extras)["summaries"]
    report = reconcile(plan, runs + extras)
    assert report["unexpected_run_ids"] == sorted(r.run_id for r in extras)
    assert report["comparison"] is None
    extra_group = [
        changed(r, run_id=r.run_id + "-extra", model_label="model-c",
                conditions={**r.conditions.model_dump(), "model_sha256": "f" * 64})
        for r in runs if r.model_label == "model-a"
    ]
    assert compare_runs(runs + extra_group)["summaries"]
    assert not reconcile(plan, runs + extra_group)["ok"]


def test_swapping_ids_cannot_reassign_outcomes():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    runs[0], runs[1] = changed(runs[0], run_id=runs[1].run_id), changed(runs[1], run_id=runs[0].run_id)
    report = reconcile(plan, runs)
    assert len(report["mismatches"]) == 2
    assert report["comparison"] is None


def test_failures_cancellations_and_validation_unexecuted_remain_in_denominator():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    runs[0] = changed(
        runs[0], status="failed", validation_passed=None, exit_code=None,
        failure_code="model_unavailable", failure_stage="profile",
    )
    runs[1] = changed(runs[1], status="cancelled", validation_passed=None, exit_code=None)
    runs[2] = changed(
        runs[2], status="failed", validation_passed=False, exit_code=1,
        failure_code="validation_failure", failure_stage="validation",
    )
    report = reconcile(plan, runs)
    assert report["ok"] is True  # Complete accounting is not successful execution.
    assert report["comparison"] == compare_runs(runs)
    assert report["validation_unexecuted_run_ids"] == [runs[0].run_id, runs[1].run_id]
    task_a = report["comparison"]["summaries"][0]
    assert task_a["runs"] == task_a["validation_unexecuted"] == 2
    assert task_a["succeeded"] == 0
    assert task_a["median_wall_time_success_seconds"] is None


@pytest.mark.parametrize("mutation", ["empty", "one_group", "duplicate_id", "duplicate_trial", "unbalanced",
                                      "same_variable", "variable_drift", "fixed_drift"])
def test_invalid_plan_is_rejected_before_results_exist(mutation):
    raw = synthetic_plan().model_dump()
    trials = list(raw["trials"])
    if mutation == "empty":
        trials = []
    elif mutation == "one_group":
        trials = trials[:4]
    elif mutation == "duplicate_id":
        trials.append(copy.deepcopy(trials[0]))
    elif mutation == "duplicate_trial":
        trials.append({**trials[0], "run_id": "distinct"})
    elif mutation == "unbalanced":
        trials.pop()
    elif mutation == "same_variable":
        for trial in trials:
            trial["conditions"]["model_sha256"] = "f" * 64
    elif mutation == "variable_drift":
        trials[0]["conditions"]["model_sha256"] = "f" * 64
    else:
        trials[0]["conditions"]["budget_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        TrialPlan.model_validate({**raw, "trials": tuple(trials)})


def test_pinned_plan_digest_prevents_retrospective_coverage_reduction():
    plan = synthetic_plan()
    evidence = retained_evidence(plan, synthetic_results(plan))
    reduced = plan.model_copy(update={"trials": tuple(t for t in plan.trials if t.repeat_index == 1)})
    with pytest.raises(ValueError, match="predeclared commitment"):
        reconcile_trials(reduced, synthetic_results(reduced), **evidence)
    with pytest.raises(ValidationError):
        plan.trials[0].conditions.budget_sha256 = "f" * 64
    with pytest.raises(ValidationError):
        plan.trials = ()


@pytest.mark.parametrize("field,value", [("repeat_index", True), ("wall_time", float("inf")),
                                         ("validation_passed", None)])
def test_result_model_copy_bypass_is_revalidated(field, value):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    runs[0] = runs[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        reconcile(plan, runs)


def test_plan_nested_model_copy_bypass_is_revalidated():
    plan = synthetic_plan()
    trial = plan.trials[0].model_copy(update={"repeat_index": True})
    plan = plan.model_copy(update={"trials": (trial, *plan.trials[1:])})
    with pytest.raises(ValueError):
        plan_sha256(plan)


@pytest.mark.parametrize("split", ["development", "heldout"])
def test_transfer_plan_reuses_frozen_contract_and_make_run_adapter(split):
    frozen = freeze_transfer_suite()
    profile = load_coding_profiles()["mock"]
    env = {"platform": "synthetic", "python": "3.12", "dependencies": "fixture-lock", "executor": "synthetic"}
    budget = {"timeout_seconds": 10, "max_turns": 8, "max_tool_calls": 16}
    args = {"split": split, "profile": profile, "environment": env, "budget": budget, "repeat": 2}
    source = comparison_plan(frozen, **args)
    plan = make_transfer_trial_plan(frozen, plan_id="synthetic-transfer", **args)
    commitment = plan_sha256(plan)  # Freeze before constructing synthetic outcomes.
    assert plan.source_plan_sha256 == fingerprint(source)
    assert len(plan.trials) == 18
    assert [t.variant_label for t in plan.trials] == [r["variant_label"] for r in source["runs"]]
    assert TrialPlan.model_validate_json(plan.model_dump_json()) == plan
    runs = []
    originals = []
    for trial in plan.trials:
        fixture = frozen.task(trial.task_id).fixture
        result = CodingResult(
            run_id=trial.run_id, task_id=trial.task_id, job_id="synthetic-job",
            model=ModelResult(profile=profile.profile_id, provider=profile.provider, model_id=profile.model_id),
            repository=RepositoryResult(),
            result=ResultStatus(status="succeeded", validation_passed=True, exit_code=0),
            metrics=CodingMetrics(wall_time=1, files_changed=1, diff_additions=1, diff_deletions=0),
            artifacts=CodingArtifacts(),
        )
        originals.append(result)
        runs.append(make_run(
            result, profile=profile, fixture=fixture, suite_id=frozen.suite.suite_id,
            suite_sha256=frozen.suite_sha256, checker_sha256=frozen.checker_sha256,
            environment=env, budget=budget, variant=variant_manifest(frozen, trial.variant_label),
            variant_label=trial.variant_label, repeat_index=trial.repeat_index, evidence_kind="synthetic",
        ))
    evidence = retained_evidence(plan, runs, originals)
    assert evidence["expected_plan_sha256"] == commitment
    report = reconcile_trials(plan, runs, **evidence)
    assert report["ok"] is True
    assert report["comparison"] == compare_runs(runs, axis="variant")
    kept = [r for r in runs if not (r.task_id == runs[0].task_id and r.repeat_index == 2)]
    assert compare_runs(kept, axis="variant")["summaries"]
    missing = reconcile_trials(plan, kept, **evidence)
    assert missing["ok"] is False
    assert missing["comparison"] is None
    assert len(missing["missing_run_ids"]) == 3


def test_cli_success_mismatch_and_invalid_input(tmp_path, capsys):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    paths, argv = write_cli_inputs(tmp_path, plan, runs, retained_evidence(plan, runs))
    plan_file, records_file = paths[:2]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["comparison"] == compare_runs(runs)
    records_file.write_text("[]", encoding="utf-8")
    assert main(argv) == 1
    assert json.loads(capsys.readouterr().out)["comparison"] is None
    for text in ('{}', '[{"run_id":"a","run_id":"b"}]', '[NaN]', '[true]', '[{}]', 'not-json'):
        records_file.write_text(text, encoding="utf-8")
        assert main(argv) == 2
        assert json.loads(capsys.readouterr().out)["ok"] is False
    records_file.write_text("[]", encoding="utf-8")
    wrong_plan = list(argv)
    wrong_plan[wrong_plan.index("--plan-sha256") + 1] = "f" * 64
    assert main(wrong_plan) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    plan_file.write_text('{"plan_id":"first","plan_id":"second"}', encoding="utf-8")
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_excess_result_records_are_rejected_without_truncation():
    plan = synthetic_plan()
    with pytest.raises(ValueError, match="at most"):
        reconcile_trials(plan, [synthetic_results(plan)[0]] * 10001,
                         **retained_evidence(plan, synthetic_results(plan)))


def test_execution_receipt_binds_complete_record_and_raw_result():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    receipt = evidence["ledger"].records[0]
    assert receipt.record_sha256 == fingerprint(runs[0].model_dump(mode="json"))
    assert receipt.result_sha256 == fingerprint(evidence["raw_results"][0].model_dump(mode="json"))
    assert receipt.state == "recorded"
    assert receipt.reason is None
    assert execution_ledger_sha256(evidence["ledger"]) != fingerprint(
        evidence["ledger"].model_dump(mode="json")
    )
    with pytest.raises(ValidationError):
        receipt.record_sha256 = "f" * 64
    with pytest.raises(ValidationError):
        evidence["ledger"].records = ()


@pytest.mark.parametrize("fields", [
    {"state": "recorded"},
    {"state": "recorded", "record_sha256": "a" * 64},
    {"state": "recorded", "result_sha256": "a" * 64},
    {"state": "recorded", "record_sha256": "a" * 64, "result_sha256": "b" * 64, "reason": "no"},
    {"state": "not_executed"},
    {"state": "not_executed", "reason": " \t\n"},
    {"state": "not_executed", "reason": "synthetic skip", "record_sha256": "a" * 64},
    {"state": "not_executed", "reason": "synthetic skip", "result_sha256": "a" * 64},
])
def test_invalid_execution_states_cannot_describe_an_observed_result(fields):
    with pytest.raises(ValueError):
        ExecutionRecord(run_id="synthetic-run", **fields)


def test_cross_plan_replay_rejected_even_with_correct_new_plan_commitment():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    other_plan = plan.model_copy(update={"plan_id": "other-synthetic-experiment"})
    # Correct new plan digest cannot make the old trusted ledger belong to it.
    with pytest.raises(ValueError, match="different plan"):
        reconcile_trials(plan=other_plan, runs=runs, **{
            **evidence, "expected_plan_sha256": plan_sha256(other_plan),
        })


def test_edited_ledger_cannot_be_rebound_with_the_old_commitment():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    original_digest = evidence["expected_ledger_sha256"]
    ledger = evidence["ledger"]
    replacement = ledger.records[0].model_copy(update={"record_sha256": "f" * 64})
    edited = ledger.model_copy(update={"records": (replacement, *ledger.records[1:])})
    with pytest.raises(ValueError, match="retained commitment"):
        reconcile_trials(plan, runs, **{**evidence, "ledger": edited})
    assert evidence["expected_ledger_sha256"] == original_digest


@pytest.mark.parametrize("fields", [
    {"wall_time": 0.0},
    {"status": "cancelled", "validation_passed": None, "exit_code": None},
    {"result_sha256": "f" * 64},
])
def test_same_id_record_substitution_rejected_by_prior_ledger(fields):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    tampered = [changed(runs[0], **fields), *runs[1:]]
    report = reconcile_trials(plan, tampered, **evidence)
    assert_incomplete(report)
    assert report["evidence_mismatches"][0]["run_id"] == runs[0].run_id
    assert "execution.record_sha256" in report["evidence_mismatches"][0]["fields"]


def test_raw_body_change_detected_when_all_projected_outcomes_are_unchanged():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    originals = evidence["raw_results"]
    altered = originals[0].model_copy(update={"job_id": "another-synthetic-job"})
    report = reconcile_trials(plan, runs, **{**evidence, "raw_results": [altered, *originals[1:]]})
    assert_incomplete(report)
    fields = report["evidence_mismatches"][0]["fields"]
    assert "raw_result.result_sha256" in fields
    assert "execution.raw_result_sha256" in fields


def test_coordinated_record_and_raw_replacement_cannot_rewrite_trusted_ledger():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    originals = evidence["raw_results"]
    altered = originals[0].model_copy(update={"job_id": "coordinated-synthetic-substitution"})
    replacement = changed(runs[0], result_sha256=fingerprint(altered.model_dump(mode="json")))
    report = reconcile_trials(plan, [replacement, *runs[1:]], **{
        **evidence, "raw_results": [altered, *originals[1:]],
    })
    assert_incomplete(report)
    assert "execution.record_sha256" in report["evidence_mismatches"][0]["fields"]


@pytest.mark.parametrize("field", [
    "run_id", "task_id", "model_label", "status", "validation_passed", "exit_code",
    "wall_time", "failure_code", "failure_stage",
])
def test_recorder_checks_projection_even_with_recomputed_raw_hash(field):
    plan = synthetic_plan()
    run = synthetic_results(plan)[0]
    original = synthetic_raw(run)
    data = original.model_dump(mode="json")
    if field in {"run_id", "task_id"}:
        data[field] = "different-synthetic-id"
    elif field == "model_label":
        data["model"]["profile"] = "different-synthetic-profile"
    elif field == "wall_time":
        data["metrics"]["wall_time"] = 99.0
    elif field == "failure_code":
        data["result"]["failure"] = {"code": "validation_failure", "stage": "validation"}
    elif field == "failure_stage":
        # Start from a valid failed result so code matches and only stage differs.
        run = changed(run, status="failed", validation_passed=False, exit_code=1,
                      failure_code="validation_failure", failure_stage="validation")
        data = synthetic_raw(run).model_dump(mode="json")
        data["result"]["failure"]["stage"] = "agent"
    else:
        data["result"][field] = {"status": "failed", "validation_passed": False, "exit_code": 1}[field]
    altered = CodingResult.model_validate(data)
    matching_hash = changed(run, result_sha256=fingerprint(altered.model_dump(mode="json")))
    with pytest.raises(ValueError, match="differs"):
        record_execution(matching_hash, altered)


def test_recorder_rejects_raw_hash_claim_without_matching_body():
    plan = synthetic_plan()
    run = synthetic_results(plan)[0]
    with pytest.raises(ValueError, match="differs"):
        record_execution(changed(run, result_sha256="f" * 64), synthetic_raw(run))


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "replacement_id"])
def test_all_raw_result_ids_are_reconciled_without_overwrite(mutation):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    originals = evidence["raw_results"]
    extra = originals[0].model_copy(update={"run_id": "extra-raw-id"})
    changed_raw = {
        "missing": originals[1:],
        "extra": [*originals, extra],
        "duplicate": [*originals, originals[0]],
        "replacement_id": [extra, *originals[1:]],
    }[mutation]
    report = reconcile_trials(plan, runs, **{**evidence, "raw_results": changed_raw})
    assert_incomplete(report)
    if mutation in {"missing", "replacement_id"}:
        assert report["missing_raw_result_ids"] == [runs[0].run_id]
    if mutation in {"extra", "replacement_id"}:
        assert report["unexpected_raw_result_ids"] == ["extra-raw-id"]
    if mutation == "duplicate":
        assert report["duplicate_raw_result_ids"] == [runs[0].run_id]


@pytest.mark.parametrize("mutation", ["missing", "extra", "empty"])
def test_missing_or_extra_trusted_execution_records_block_submitted_success(mutation):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    records = evidence["ledger"].records
    extra = records[0].model_copy(update={"run_id": "unplanned-execution"})
    selected = {"missing": records[1:], "extra": (*records, extra), "empty": ()}[mutation]
    # This is an independently incomplete ledger, committed before accepting the
    # submitted successes. It is not a post-submission repair of the commitment.
    ledger = ExecutionLedger(plan_sha256=plan_sha256(plan), records=selected)
    commitment = execution_ledger_sha256(ledger)
    report = reconcile_trials(plan, runs, **{
        **evidence, "ledger": ledger, "expected_ledger_sha256": commitment,
    })
    assert_incomplete(report)
    if mutation == "extra":
        assert report["unexpected_execution_record_ids"] == ["unplanned-execution"]
    else:
        assert report["missing_execution_record_ids"] == sorted(
            [run.run_id for run in runs] if mutation == "empty" else [runs[0].run_id]
        )


@pytest.mark.parametrize("submit_forged_success", [False, True])
def test_explicit_not_executed_never_becomes_a_complete_experiment(submit_forged_success):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    skipped = ExecutionRecord(
        run_id=runs[0].run_id, state="not_executed", reason="synthetic scheduler did not start trial",
    )
    ledger = ExecutionLedger(
        plan_sha256=plan_sha256(plan), records=(skipped, *evidence["ledger"].records[1:]),
    )
    commitment = execution_ledger_sha256(ledger)
    submitted = runs if submit_forged_success else runs[1:]
    originals = evidence["raw_results"] if submit_forged_success else evidence["raw_results"][1:]
    report = reconcile_trials(plan, submitted, **{
        **evidence, "ledger": ledger, "expected_ledger_sha256": commitment, "raw_results": originals,
    })
    assert_incomplete(report)
    assert report["not_executed_run_ids"] == [runs[0].run_id]
    if submit_forged_success:
        assert "execution.state" in report["evidence_mismatches"][0]["fields"]
    else:
        assert report["missing_run_ids"] == [runs[0].run_id]


def test_duplicate_execution_record_is_rejected_in_constructor_and_constructed_bypass():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    ledger = evidence["ledger"]
    duplicates = (*ledger.records, ledger.records[0])
    with pytest.raises(ValueError, match="duplicate execution"):
        ExecutionLedger(plan_sha256=plan_sha256(plan), records=duplicates)
    bypass = ledger.model_copy(update={"records": duplicates})
    with pytest.raises(ValueError, match="duplicate execution"):
        reconcile_trials(plan, runs, **{**evidence, "ledger": bypass})


@pytest.mark.parametrize("version", [True, 1.0, "1"])
@pytest.mark.parametrize("target", ["plan", "ledger", "record", "raw"])
def test_noninteger_schema_versions_rejected_even_after_model_copy(target, version):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    if target == "plan":
        plan = plan.model_copy(update={"schema_version": version})
    elif target == "ledger":
        evidence = {**evidence, "ledger": evidence["ledger"].model_copy(update={"schema_version": version})}
    elif target == "record":
        runs = [runs[0].model_copy(update={"schema_version": version}), *runs[1:]]
    else:
        originals = evidence["raw_results"]
        evidence = {**evidence, "raw_results": [
            originals[0].model_copy(update={"schema_version": version}), *originals[1:],
        ]}
    with pytest.raises(ValueError, match="schema_version"):
        reconcile_trials(plan, runs, **evidence)


@pytest.mark.parametrize("version", [True, 1.0, "1"])
@pytest.mark.parametrize("target", ["plan", "ledger"])
def test_plan_and_ledger_constructors_reject_noninteger_versions(target, version):
    plan = synthetic_plan()
    if target == "plan":
        value, cls = plan.model_dump(), TrialPlan
    else:
        value = retained_evidence(plan, synthetic_results(plan))["ledger"].model_dump()
        cls = ExecutionLedger
    with pytest.raises(ValueError, match="schema_version"):
        cls.model_validate({**value, "schema_version": version})


@pytest.mark.parametrize("target", ["plan_id", "run_id", "task_id", "model_label", "variant_label", "suite_id"])
def test_blank_plan_identifiers_rejected_after_construction_bypass(target):
    plan = synthetic_plan()
    if target == "plan_id":
        altered = plan.model_copy(update={"plan_id": " \t\n"})
    else:
        trial = plan.trials[0]
        if target == "suite_id":
            trial = trial.model_copy(update={"conditions": trial.conditions.model_copy(update={"suite_id": " \t\n"})})
        else:
            trial = trial.model_copy(update={target: " \t\n"})
        altered = plan.model_copy(update={"trials": (trial, *plan.trials[1:])})
    with pytest.raises(ValueError, match="blank"):
        plan_sha256(altered)


@pytest.mark.parametrize("target", ["run_id", "task_id", "model_label", "variant_label", "suite_id"])
def test_blank_result_identifiers_cannot_match_a_trusted_receipt(target):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    run = runs[0]
    if target == "suite_id":
        run = run.model_copy(update={"conditions": run.conditions.model_copy(update={"suite_id": " \t\n"})})
    else:
        run = run.model_copy(update={target: " \t\n"})
    with pytest.raises(ValueError, match="blank"):
        reconcile_trials(plan, [run, *runs[1:]], **evidence)


@pytest.mark.parametrize("target", ["run_id", "task_id", "profile"])
def test_blank_raw_identity_is_rejected(target):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    originals = evidence["raw_results"]
    raw = originals[0]
    if target == "profile":
        raw = raw.model_copy(update={"model": raw.model.model_copy(update={"profile": " \t\n"})})
    else:
        raw = raw.model_copy(update={target: " \t\n"})
    with pytest.raises(ValueError, match="blank"):
        reconcile_trials(plan, runs, **{**evidence, "raw_results": [raw, *originals[1:]]})


def test_blank_execution_record_identity_is_rejected():
    with pytest.raises(ValueError, match="blank"):
        ExecutionRecord(run_id=" \t\n", state="not_executed", reason="synthetic skip")


@pytest.mark.parametrize("target", ["plan", "records", "raw", "ledger"])
@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_cli_rejects_noninteger_versions_before_schema_coercion(tmp_path, capsys, target, version):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    paths, argv = write_cli_inputs(tmp_path, plan, runs, retained_evidence(plan, runs))
    path = paths[{"plan": 0, "records": 1, "raw": 2, "ledger": 3}[target]]
    content = json.loads(path.read_text(encoding="utf-8"))
    record = content[0] if isinstance(content, list) else content
    record["schema_version"] = version
    path.write_text(json.dumps(content), encoding="utf-8")
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_distinct_hash_kinds_cannot_replace_pinned_commitments():
    plan = synthetic_plan().model_copy(update={"source_plan_sha256": fingerprint({"synthetic": "source-plan"})})
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    canonical = fingerprint(plan.model_dump(mode="json"))
    raw_file = hashlib.sha256(plan.model_dump_json(indent=2).encode("utf-8")).hexdigest()
    source = plan.source_plan_sha256
    result = runs[0].result_sha256
    ledger = evidence["expected_ledger_sha256"]
    bare_ledger = fingerprint(evidence["ledger"].model_dump(mode="json"))
    assert canonical == evidence["expected_plan_sha256"]
    assert len({canonical, raw_file, source, result, ledger, bare_ledger}) == 6
    for wrong in (raw_file, source, result, ledger, bare_ledger):
        with pytest.raises(ValueError, match="predeclared commitment"):
            reconcile_trials(plan, runs, **{**evidence, "expected_plan_sha256": wrong})
    for wrong in (canonical, raw_file, source, result, bare_ledger):
        with pytest.raises(ValueError, match="retained commitment"):
            reconcile_trials(plan, runs, **{**evidence, "expected_ledger_sha256": wrong})


def test_raw_result_limit_is_checked_without_truncation():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    with pytest.raises(ValueError, match="at most"):
        reconcile_trials(plan, runs, **{**evidence, "raw_results": [evidence["raw_results"][0]] * 10001})


def test_required_evidence_arguments_have_no_self_pinning_defaults():
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    evidence = retained_evidence(plan, runs)
    for field in evidence:
        missing = {key: value for key, value in evidence.items() if key != field}
        with pytest.raises(TypeError):
            reconcile_trials(plan, runs, **missing)


@pytest.mark.parametrize("mutation", ["empty_conditions", "missing_budget_hash", "empty_budget_hash"])
def test_empty_conditions_and_missing_budget_are_rejected_in_plan_json(tmp_path, capsys, mutation):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    paths, argv = write_cli_inputs(tmp_path, plan, runs, retained_evidence(plan, runs))
    content = json.loads(paths[0].read_text(encoding="utf-8"))
    trial = content["trials"][0]
    if mutation == "empty_conditions":
        trial["conditions"] = {}
    elif mutation == "missing_budget_hash":
        del trial["conditions"]["budget_sha256"]
    else:
        trial["conditions"]["budget_sha256"] = ""
    paths[0].write_text(json.dumps(content), encoding="utf-8")
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("target", ["raw", "ledger"])
def test_duplicate_json_keys_in_raw_and_ledger_are_rejected(tmp_path, capsys, target):
    plan = synthetic_plan()
    runs = synthetic_results(plan)
    paths, argv = write_cli_inputs(tmp_path, plan, runs, retained_evidence(plan, runs))
    path = paths[2 if target == "raw" else 3]
    content = path.read_text(encoding="utf-8")
    entry = '"run_id": ' + json.dumps(runs[0].run_id)
    assert entry in content
    path.write_text(content.replace(entry, entry + ", " + entry, 1), encoding="utf-8")
    assert main(argv) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
