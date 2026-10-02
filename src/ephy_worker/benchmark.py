"""Offline, condition-matched summaries over existing CodingResult records．

This module never invokes a model, executes a result's commands, or adopts a patch．
Fingerprints bind supplied metadata; they are not runner execution attestations．
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .coding_schema import CodingFixture, CodingModelProfile, CodingResult, FailureCode
from .schema import StrictModel

Digest = str


def fingerprint(value: object) -> str:
    """Hash canonical JSON; reject non-finite values and ambiguous non-string keys．"""

    def check(item):
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("fingerprint keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)

    check(value)
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Conditions(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    suite_id: str = Field(min_length=1, max_length=128)
    suite_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    task_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    checker_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    environment_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    budget_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    model_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    variant_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")


class BenchmarkRun(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=128)
    repeat_index: int = Field(ge=1, le=100)
    model_label: str = Field(min_length=1, max_length=128)
    variant_label: str = Field(min_length=1, max_length=128)
    evidence_kind: Literal["synthetic", "mock", "live"]
    conditions: Conditions
    result_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["succeeded", "failed", "cancelled"]
    validation_passed: bool | None
    exit_code: int | None
    wall_time: float = Field(ge=0, allow_inf_nan=False)
    failure_code: FailureCode | None = None
    failure_stage: Literal["profile", "repository", "worktree", "agent", "validation", "worker"] | None = None

    @model_validator(mode="after")
    def outcome_consistent(self):
        if self.status == "succeeded" and (
            self.validation_passed is not True
            or self.exit_code != 0
            or self.failure_code is not None
            or self.failure_stage is not None
        ):
            raise ValueError("success requires exit zero, validation and no failure")
        if (self.failure_code is None) != (self.failure_stage is None):
            raise ValueError("failure code and stage must be supplied together")
        return self


def make_run(
    result: CodingResult,
    *,
    profile: CodingModelProfile,
    fixture: CodingFixture,
    suite_id: str,
    suite_sha256: str,
    checker_sha256: str,
    environment: dict,
    budget: dict,
    variant: dict,
    variant_label: str,
    repeat_index: int,
    evidence_kind: Literal["synthetic", "mock", "live"],
) -> BenchmarkRun:
    """Adapt an existing result without retaining logs, paths, or credentials．

    Caller freezes metadata before execution and supplies actual runner observations．
    This adapter cannot retrospectively prove those observations or enforce budgets．
    """
    if result.task_id != fixture.task_id:
        raise ValueError("result task does not match fixture")
    actual = result.model
    if (actual.profile, actual.provider, actual.model_id, actual.quantization) != (
        profile.profile_id,
        profile.provider,
        profile.model_id,
        profile.quantization,
    ):
        raise ValueError("result model does not match profile")
    if evidence_kind == "mock" and profile.server_type != "mock":
        raise ValueError("mock evidence requires a mock profile")
    if evidence_kind == "live" and profile.server_type == "mock":
        raise ValueError("mock profile cannot claim live evidence")
    for key in ("platform", "python", "dependencies", "executor"):
        if not isinstance(environment.get(key), str) or not environment[key].strip():
            raise ValueError(f"environment requires {key}")
    timeout = budget.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise ValueError("budget requires a finite timeout_seconds")
    if timeout != fixture.timeout_seconds:
        raise ValueError("budget timeout differs from fixture")
    # Labels, machine paths and endpoint addresses do not identify model configuration．
    # Server/Pi versions, hardware and endpoint mapping belong to environment metadata．
    model_config = profile.model_dump(
        mode="json", exclude={"profile_id", "notes", "endpoint", "pi_executable"}
    )
    failure = result.result.failure
    return BenchmarkRun(
        run_id=result.run_id,
        task_id=result.task_id,
        repeat_index=repeat_index,
        model_label=profile.profile_id,
        variant_label=variant_label,
        evidence_kind=evidence_kind,
        conditions=Conditions(
            suite_id=suite_id,
            suite_sha256=suite_sha256,
            task_sha256=fingerprint(fixture.model_dump(mode="json")),
            checker_sha256=checker_sha256,
            environment_sha256=fingerprint(environment),
            budget_sha256=fingerprint(budget),
            model_sha256=fingerprint(model_config),
            variant_sha256=fingerprint(variant),
        ),
        result_sha256=fingerprint(result.model_dump(mode="json")),
        status=result.result.status,
        validation_passed=result.result.validation_passed,
        exit_code=result.result.exit_code,
        wall_time=result.metrics.wall_time,
        failure_code=failure.code if failure else None,
        failure_stage=failure.stage if failure else None,
    )


def compare_runs(runs: list[BenchmarkRun], *, axis: Literal["model", "variant"] = "model") -> dict:
    """Compare one declared variable, matching task/repeat grids and all other conditions．"""
    if axis not in {"model", "variant"} or not runs:
        raise ValueError("comparison needs runs and axis model or variant")
    # Revalidate model_copy/constructed instances too, rather than trusting object provenance．
    runs = [BenchmarkRun.model_validate(run.model_dump(mode="json")) for run in runs]
    if len({run.run_id for run in runs}) != len(runs):
        raise ValueError("duplicate run_id")
    if len({run.evidence_kind for run in runs}) != 1:
        raise ValueError("mixed evidence kinds")
    groups: dict[str, list[BenchmarkRun]] = defaultdict(list)
    for run in runs:
        groups[getattr(run, axis + "_label")].append(run)
    if len(groups) < 2:
        raise ValueError("comparison needs at least two groups")
    reference_grid = None
    task_conditions: dict[str, dict] = {}
    variable_hashes = set()
    for label, records in groups.items():
        grid = {(run.task_id, run.repeat_index) for run in records}
        if len(grid) != len(records):
            raise ValueError(f"duplicate task/repeat in {label}")
        if reference_grid is None:
            reference_grid = grid
        elif grid != reference_grid:
            raise ValueError("task/repeat coverage mismatch")
        hashes = {getattr(run.conditions, axis + "_sha256") for run in records}
        if len(hashes) != 1:
            raise ValueError(f"variable configuration drift within {label}")
        variable_hashes.update(hashes)
        for run in records:
            fixed = run.conditions.model_dump(exclude={axis + "_sha256"})
            if run.task_id in task_conditions and task_conditions[run.task_id] != fixed:
                raise ValueError(f"fixed conditions mismatch for {run.task_id}")
            task_conditions[run.task_id] = fixed
    if len(variable_hashes) != len(groups):
        raise ValueError("different labels reuse the same variable configuration")
    summaries = []
    for label, records in sorted(groups.items()):
        for task_id in sorted(task_conditions):
            task_runs = [run for run in records if run.task_id == task_id]
            passed = [run for run in task_runs if run.status == "succeeded"]
            failures = Counter(
                f"{run.failure_stage or 'unknown'}:{run.failure_code or run.status}"
                for run in task_runs
                if run.status != "succeeded"
            )
            summaries.append(
                {
                    "group": label,
                    "variable_sha256": getattr(task_runs[0].conditions, axis + "_sha256"),
                    "task_id": task_id,
                    "runs": len(task_runs),
                    "succeeded": len(passed),
                    "pipeline_success_rate": len(passed) / len(task_runs),
                    "validation_unexecuted": sum(run.validation_passed is None for run in task_runs),
                    "median_wall_time_all_seconds": median(run.wall_time for run in task_runs),
                    "median_wall_time_success_seconds": median(run.wall_time for run in passed)
                    if passed
                    else None,
                    "failure_counts": dict(sorted(failures.items())),
                }
            )
    return {
        "schema_version": 1,
        "axis": axis,
        "evidence_kind": runs[0].evidence_kind,
        "coverage": "supplied_records_only; not attestation of a predeclared complete plan",
        "fixed_conditions": dict(sorted(task_conditions.items())),
        "summaries": summaries,
        "run_ids": sorted(run.run_id for run in runs),
        "interpretation": "Descriptive pipeline statistics only; no ranking, causal claim, or adoption decision.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path, help="JSON array of BenchmarkRun metadata, not raw logs")
    parser.add_argument("--axis", choices=("model", "variant"), default="model")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.records.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or len(raw) > 10000:
            raise ValueError("expected at most 10000 records")
        summary = compare_runs([BenchmarkRun.model_validate(item) for item in raw], axis=args.axis)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 2
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
