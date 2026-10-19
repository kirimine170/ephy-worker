"""Offline reconciliation of a fixed trial plan and independently retained execution records.

Pin the plan before execution and the trusted recorder's ledger before accepting
submitted results. Missing/unexecuted trials never become partial comparisons.
This verifies supplied evidence integrity, not physical execution or model improvement.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .benchmark import BenchmarkRun, Conditions, compare_runs, fingerprint
from .coding_schema import CodingModelProfile, CodingResult
from .schema import StrictModel
from .transfer_evaluation import FrozenTransferSuite, Split, comparison_plan

Axis = Literal["model", "variant"]
EvidenceKind = Literal["synthetic", "mock", "live"]


def _version_is_integer(data):
    if isinstance(data, dict) and "schema_version" in data and type(data["schema_version"]) is not int:
        raise ValueError("schema_version must be an integer")
    return data


def _nonblank(*values: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError("identifiers must not be blank")


class PlannedTrial(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=128)
    repeat_index: int = Field(ge=1, le=100)
    model_label: str = Field(min_length=1, max_length=128)
    variant_label: str = Field(min_length=1, max_length=128)
    conditions: Conditions

    @model_validator(mode="after")
    def identifiers_are_nonblank(self):
        _nonblank(self.run_id, self.task_id, self.model_label, self.variant_label, self.conditions.suite_id)
        return self


def _key(trial: PlannedTrial | BenchmarkRun, axis: Axis) -> tuple[str, str, int]:
    return getattr(trial, axis + "_label"), trial.task_id, trial.repeat_index


class TrialPlan(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1, max_length=128)
    axis: Axis
    evidence_kind: EvidenceKind
    source_plan_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    trials: tuple[PlannedTrial, ...] = Field(min_length=1, max_length=10000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_is_integer(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def balanced_conditions(self):
        _nonblank(self.plan_id)
        # Validate the expected design without manufacturing any result records.
        if len({trial.run_id for trial in self.trials}) != len(self.trials):
            raise ValueError("duplicate planned run_id")
        groups = defaultdict(list)
        fixed_conditions = {}
        for trial in self.trials:
            groups[getattr(trial, self.axis + "_label")].append(trial)
            fixed = trial.conditions.model_dump(exclude={self.axis + "_sha256"})
            if trial.task_id in fixed_conditions and fixed_conditions[trial.task_id] != fixed:
                raise ValueError("planned fixed conditions mismatch")
            fixed_conditions[trial.task_id] = fixed
        if len(groups) < 2:
            raise ValueError("plan needs at least two groups")
        reference_grid = None
        variable_hashes = set()
        for trials in groups.values():
            grid = {(trial.task_id, trial.repeat_index) for trial in trials}
            if len(grid) != len(trials):
                raise ValueError("duplicate planned trial")
            if reference_grid is not None and grid != reference_grid:
                raise ValueError("planned task/repeat coverage mismatch")
            reference_grid = grid
            hashes = {getattr(trial.conditions, self.axis + "_sha256") for trial in trials}
            if len(hashes) != 1:
                raise ValueError("planned variable configuration drift")
            variable_hashes.update(hashes)
        if len(variable_hashes) != len(groups):
            raise ValueError("planned labels reuse variable configuration")
        return self


def _validated_plan(plan: TrialPlan) -> TrialPlan:
    # Deeply revalidate even objects made with model_copy/model_construct.
    return TrialPlan.model_validate(plan.model_dump())


def plan_sha256(plan: TrialPlan) -> str:
    return fingerprint(_validated_plan(plan).model_dump(mode="json"))


def make_transfer_trial_plan(
    frozen: FrozenTransferSuite,
    *,
    plan_id: str,
    split: Split,
    profile: CodingModelProfile,
    environment: dict,
    budget: dict,
    repeat: int = 1,
) -> TrialPlan:
    """Reuse the transfer plan and assign IDs before any results exist.

    The order (including variant rotation) is retained. Runners must use these
    assigned IDs, not replace them with IDs inferred from completed results.
    """
    source = comparison_plan(
        frozen, split=split, profile=profile, environment=environment, budget=budget, repeat=repeat
    )
    return TrialPlan(
        plan_id=plan_id,
        axis="variant",
        evidence_kind=source["evidence_kind"],
        source_plan_sha256=fingerprint(source),
        trials=tuple(
            PlannedTrial(
                run_id=f"{plan_id}-{index:05d}",
                task_id=row["task_id"],
                repeat_index=row["repeat_index"],
                model_label=profile.profile_id,
                variant_label=row["variant_label"],
                conditions=Conditions(**{field: row[field] for field in Conditions.model_fields}),
            )
            for index, row in enumerate(source["runs"], start=1)
        ),
    )


class ExecutionRecord(StrictModel):
    """One observation retained by a trusted recorder, including explicit nonexecution."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_id: str = Field(min_length=1, max_length=256)
    state: Literal["recorded", "not_executed"]
    record_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    result_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def state_is_consistent(self):
        _nonblank(self.run_id)
        if self.state == "recorded":
            if self.record_sha256 is None or self.result_sha256 is None or self.reason is not None:
                raise ValueError("recorded execution requires both hashes and no nonexecution reason")
        elif self.record_sha256 is not None or self.result_sha256 is not None or not self.reason or not self.reason.strip():
            raise ValueError("not_executed requires a reason and no result hashes")
        return self


class ExecutionLedger(StrictModel):
    """Trusted recorder snapshot; retain its digest separately from submitted results."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    records: tuple[ExecutionRecord, ...] = Field(max_length=10000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_is_integer(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_records(self):
        if len({record.run_id for record in self.records}) != len(self.records):
            raise ValueError("duplicate execution record")
        return self


def execution_ledger_sha256(ledger: ExecutionLedger) -> str:
    ledger = ExecutionLedger.model_validate(ledger.model_dump())
    return fingerprint({"kind": "trial-execution-ledger-v1", "ledger": ledger.model_dump(mode="json")})


def _validated_run(run: BenchmarkRun) -> BenchmarkRun:
    data = _version_is_integer(run.model_dump())
    validated = BenchmarkRun.model_validate(data)
    _nonblank(validated.run_id, validated.task_id, validated.model_label,
              validated.variant_label, validated.conditions.suite_id)
    return validated


def _validated_result(result: CodingResult) -> CodingResult:
    data = _version_is_integer(result.model_dump())
    validated = CodingResult.model_validate(data, strict=True)
    _nonblank(validated.run_id, validated.task_id, validated.model.profile)
    fingerprint(validated.model_dump(mode="json"))  # Reject nonfinite values anywhere in the result.
    return validated


def _result_mismatches(run: BenchmarkRun, result: CodingResult) -> list[str]:
    failure = result.result.failure
    projected = {
        "run_id": result.run_id,
        "task_id": result.task_id,
        "model_label": result.model.profile,
        "status": result.result.status,
        "validation_passed": result.result.validation_passed,
        "exit_code": result.result.exit_code,
        "wall_time": result.metrics.wall_time,
        "failure_code": failure.code if failure else None,
        "failure_stage": failure.stage if failure else None,
        "result_sha256": fingerprint(result.model_dump(mode="json")),
    }
    return ["raw_result." + field for field, value in projected.items() if getattr(run, field) != value]


def record_execution(run: BenchmarkRun, result: CodingResult) -> ExecutionRecord:
    """Trusted recorder helper, used at capture time, never to self-pin submitted evidence."""
    run, result = _validated_run(run), _validated_result(result)
    if _result_mismatches(run, result):
        raise ValueError("BenchmarkRun differs from raw CodingResult")
    return ExecutionRecord(
        run_id=run.run_id, state="recorded",
        record_sha256=fingerprint(run.model_dump(mode="json")),
        result_sha256=run.result_sha256,
    )


def reconcile_trials(
    plan: TrialPlan, runs: list[BenchmarkRun], *, expected_plan_sha256: str,
    ledger: ExecutionLedger, raw_results: list[CodingResult], expected_ledger_sha256: str,
) -> dict:
    """Require an exact plan/result bijection before calling the existing summary.

    Failed/cancelled results remain results; absent trials remain missing. The
    digest must be the caller's separately retained pre-execution commitment.
    Neither digest may be recomputed from documents supplied by an untrusted
    result producer. The ledger must come from the separately trusted recorder.
    """
    plan = _validated_plan(plan)
    digest = plan_sha256(plan)
    if expected_plan_sha256 != digest:
        raise ValueError("plan digest differs from predeclared commitment")
    if not isinstance(runs, list) or len(runs) > 10000:
        raise ValueError("expected at most 10000 result records")
    runs = [_validated_run(run) for run in runs]
    ledger = ExecutionLedger.model_validate(ledger.model_dump())
    ledger_digest = execution_ledger_sha256(ledger)
    if expected_ledger_sha256 != ledger_digest:
        raise ValueError("execution ledger differs from retained commitment")
    if ledger.plan_sha256 != digest:
        raise ValueError("execution ledger belongs to a different plan")
    if not isinstance(raw_results, list) or len(raw_results) > 10000:
        raise ValueError("expected at most 10000 raw results")
    raw_results = [_validated_result(result) for result in raw_results]
    # Count before indexing so duplicates cannot silently replace earlier records.
    ids = Counter(run.run_id for run in runs)
    trials = Counter(_key(run, plan.axis) for run in runs)
    expected = {trial.run_id: trial for trial in plan.trials}
    missing = sorted(expected.keys() - ids.keys())
    unexpected = sorted(ids.keys() - expected.keys())
    duplicate_ids = sorted(key for key, count in ids.items() if count > 1)
    duplicate_trials = [
        {"group": group, "task_id": task, "repeat_index": repeat, "count": count}
        for (group, task, repeat), count in sorted(trials.items())
        if count > 1
    ]
    mismatches = []
    for run in runs:
        if run.run_id not in expected:
            continue
        trial = expected[run.run_id]
        fields = [
            field
            for field in ("task_id", "repeat_index", "model_label", "variant_label")
            if getattr(run, field) != getattr(trial, field)
        ]
        if run.evidence_kind != plan.evidence_kind:
            fields.append("evidence_kind")
        fields.extend(
            "conditions." + field
            for field in Conditions.model_fields
            if getattr(run.conditions, field) != getattr(trial.conditions, field)
        )
        if fields:
            mismatches.append({"run_id": run.run_id, "fields": sorted(fields)})
    receipts = {record.run_id: record for record in ledger.records}
    missing_receipts = sorted(expected.keys() - receipts.keys())
    unexpected_receipts = sorted(receipts.keys() - expected.keys())
    not_executed = sorted(record.run_id for record in ledger.records if record.state == "not_executed")
    raw_ids = Counter(result.run_id for result in raw_results)
    recorded_ids = {record.run_id for record in ledger.records if record.state == "recorded"}
    missing_raw = sorted(recorded_ids - raw_ids.keys())
    unexpected_raw = sorted(raw_ids.keys() - recorded_ids)
    duplicate_raw = sorted(key for key, count in raw_ids.items() if count > 1)
    raw_by_id = {result.run_id: result for result in raw_results}
    evidence_mismatches = []
    for run in runs:
        receipt = receipts.get(run.run_id)
        fields = []
        if receipt is not None:
            if receipt.state != "recorded":
                fields.append("execution.state")
            else:
                if receipt.record_sha256 != fingerprint(run.model_dump(mode="json")):
                    fields.append("execution.record_sha256")
                if receipt.result_sha256 != run.result_sha256:
                    fields.append("execution.result_sha256")
        if run.run_id in raw_by_id:
            result = raw_by_id[run.run_id]
            fields.extend(_result_mismatches(run, result))
            if receipt is not None and receipt.result_sha256 != fingerprint(result.model_dump(mode="json")):
                fields.append("execution.raw_result_sha256")
        if fields:
            evidence_mismatches.append({"run_id": run.run_id, "fields": sorted(fields)})
    ok = not (
        missing or unexpected or duplicate_ids or duplicate_trials or mismatches
        or missing_receipts or unexpected_receipts or not_executed
        or missing_raw or unexpected_raw or duplicate_raw or evidence_mismatches
    )
    return {
        "schema_version": 1,
        "ok": ok,
        "plan_id": plan.plan_id,
        "plan_sha256": digest,
        "execution_ledger_sha256": ledger_digest,
        "axis": plan.axis,
        "evidence_kind": plan.evidence_kind,
        "expected_runs": len(plan.trials),
        "supplied_runs": len(runs),
        "missing_run_ids": missing,
        "unexpected_run_ids": unexpected,
        "duplicate_run_ids": duplicate_ids,
        "duplicate_trials": duplicate_trials,
        "mismatches": sorted(mismatches, key=lambda item: (item["run_id"], item["fields"])),
        "validation_unexecuted_run_ids": sorted(run.run_id for run in runs if run.validation_passed is None),
        "missing_execution_record_ids": missing_receipts,
        "unexpected_execution_record_ids": unexpected_receipts,
        "not_executed_run_ids": not_executed,
        "missing_raw_result_ids": missing_raw,
        "unexpected_raw_result_ids": unexpected_raw,
        "duplicate_raw_result_ids": duplicate_raw,
        "evidence_mismatches": sorted(evidence_mismatches, key=lambda item: (item["run_id"], item["fields"])),
        "record_integrity_verified": ok,
        "execution_verified": False,
        "comparison": compare_runs(runs, axis=plan.axis) if ok else None,
        "coverage": "exact records/raw results match to committed plan and ledger" if ok else "incomplete or mismatched evidence",
        "interpretation": (
            "Integrity relative to trusted plan/recorder commitments only; not proof of chronology, execution, "
            "budget enforcement, model improvement, formal workflow or adoption."
        ),
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="TrialPlan JSON saved before execution")
    parser.add_argument("records", type=Path, help="JSON array containing all BenchmarkRun outcomes")
    parser.add_argument("raw_results", type=Path, help="JSON array of original CodingResult outcomes")
    parser.add_argument("ledger", type=Path, help="ExecutionLedger captured by the trusted recorder")
    parser.add_argument("--plan-sha256", required=True, help="separately retained pre-execution plan digest")
    parser.add_argument("--ledger-sha256", required=True, help="separately retained trusted recorder digest")
    args = parser.parse_args(argv)
    try:
        # JSON-mode validation accepts arrays for immutable tuple fields.
        plan = TrialPlan.model_validate_json(json.dumps(_read_json(args.plan), allow_nan=False))
        raw = _read_json(args.records)
        if not isinstance(raw, list) or len(raw) > 10000:
            raise ValueError("expected at most 10000 result records")
        originals = _read_json(args.raw_results)
        if not isinstance(originals, list) or len(originals) > 10000:
            raise ValueError("expected at most 10000 raw results")
        ledger = ExecutionLedger.model_validate_json(json.dumps(_read_json(args.ledger), allow_nan=False))
        report = reconcile_trials(
            plan, [BenchmarkRun.model_validate(_version_is_integer(item)) for item in raw],
            expected_plan_sha256=args.plan_sha256, ledger=ledger,
            raw_results=[CodingResult.model_validate(_version_is_integer(item), strict=True) for item in originals],
            expected_ledger_sha256=args.ledger_sha256,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
