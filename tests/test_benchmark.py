"""Synthetic tests; these are not local-model performance measurements．"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ephy_worker.benchmark import BenchmarkRun, compare_runs, fingerprint, main, make_run
from ephy_worker.coding_profiles import DEFAULT_CODING_PROFILES
from ephy_worker.coding_schema import (
    CodingArtifacts,
    CodingFailure,
    CodingMetrics,
    CodingResult,
    CodingSuite,
    ModelResult,
    RepositoryResult,
    ResultStatus,
)

ROOT = Path(__file__).resolve().parents[1]
SUITE = CodingSuite.model_validate_json(
    (ROOT / "src/ephy_worker/fixtures/coding/smoke.json").read_text(encoding="utf-8")
)
PROFILE = DEFAULT_CODING_PROFILES.by_id()["mock"]
ENV = {
    "platform": "synthetic-linux",
    "python": "3.12",
    "dependencies": "synthetic-lock",
    "executor": "fixture-v1",
}


def run_record(model="model-a", repeat=1, status="succeeded", seconds=10.0, task=0, variant="none"):
    profile = PROFILE.model_copy(update={"profile_id": model, "model_id": model})
    fixture = SUITE.tasks[task]
    failure = None if status == "succeeded" else CodingFailure(code="validation_failure", stage="validation")
    result = CodingResult(
        run_id=f"{model}-{variant}-{fixture.task_id}-{repeat}",
        task_id=fixture.task_id,
        job_id="synthetic-job",
        model=ModelResult(profile=model, provider=profile.provider, model_id=profile.model_id),
        repository=RepositoryResult(base_revision="synthetic-base"),
        result=ResultStatus(
            status=status,
            validation_passed=status == "succeeded",
            exit_code=0 if status == "succeeded" else 1,
            failure=failure,
        ),
        metrics=CodingMetrics(wall_time=seconds, files_changed=1, diff_additions=1, diff_deletions=1),
        artifacts=CodingArtifacts(),
    )
    return make_run(
        result,
        profile=profile,
        fixture=fixture,
        suite_id=SUITE.suite_id,
        suite_sha256=fingerprint(SUITE.model_dump(mode="json")),
        checker_sha256="c" * 64,
        environment=ENV,
        budget={"timeout_seconds": fixture.timeout_seconds},
        variant={"name": variant},
        variant_label=variant,
        repeat_index=repeat,
        evidence_kind="synthetic",
    )


def modified(run, **fields):
    raw = run.model_dump(mode="json")
    raw.update(fields)
    return BenchmarkRun.model_validate(raw)


class BenchmarkTests(unittest.TestCase):
    def test_model_swap_is_allowed_and_statistics_are_separate(self):
        records = [
            run_record("model-a", 1, seconds=2),
            run_record("model-a", 2, seconds=4),
            run_record("model-b", 1, seconds=1),
            run_record("model-b", 2, "failed", 9),
        ]
        report = compare_runs(records)
        self.assertEqual(report["evidence_kind"], "synthetic")
        a, b = report["summaries"]
        self.assertEqual(a["pipeline_success_rate"], 1)
        self.assertEqual(a["median_wall_time_success_seconds"], 3)
        self.assertEqual(b["pipeline_success_rate"], 0.5)
        self.assertEqual(b["median_wall_time_all_seconds"], 5)
        self.assertEqual(b["median_wall_time_success_seconds"], 1)
        self.assertEqual(b["failure_counts"], {"validation:validation_failure": 1})
        self.assertNotIn("ranking", report)

    def test_variant_axis_holds_model_fixed(self):
        a, b = run_record(variant="none"), run_record(variant="skill")
        self.assertEqual(len(compare_runs([a, b], axis="variant")["summaries"]), 2)
        b = modified(
            b, model_label="different", conditions={**b.conditions.model_dump(), "model_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(ValueError, "fixed conditions"):
            compare_runs([a, b], axis="variant")

    def test_every_confounder_rejected(self):
        a, b = run_record(), run_record("model-b")
        for field in (
            "suite_sha256",
            "task_sha256",
            "checker_sha256",
            "environment_sha256",
            "budget_sha256",
            "variant_sha256",
        ):
            with self.subTest(field=field):
                changed = modified(b, conditions={**b.conditions.model_dump(), field: "f" * 64})
                with self.assertRaisesRegex(ValueError, "fixed conditions"):
                    compare_runs([a, changed])

    def test_task_and_repeat_grid_must_match(self):
        for b in (run_record("model-b", 2), run_record("model-b", task=1)):
            with self.assertRaisesRegex(ValueError, "coverage mismatch"):
                compare_runs([run_record(), b])

    def test_duplicate_runs_and_trials_rejected(self):
        a, b = run_record(), run_record("model-b")
        with self.assertRaisesRegex(ValueError, "duplicate run_id"):
            compare_runs([a, a, b])
        with self.assertRaisesRegex(ValueError, "duplicate task/repeat"):
            compare_runs([a, modified(a, run_id="another"), b])

    def test_variable_drift_within_label_rejected(self):
        a = run_record(repeat=2)
        drift = modified(a, conditions={**a.conditions.model_dump(), "model_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "configuration drift"):
            compare_runs([run_record(), drift, run_record("model-b"), run_record("model-b", 2)])

    def test_labels_cannot_invent_a_new_model(self):
        a = run_record()
        b = modified(a, run_id="different", model_label="model-b")
        with self.assertRaisesRegex(ValueError, "same variable"):
            compare_runs([a, b])

    def test_synthetic_and_live_not_mixed(self):
        b = modified(run_record("model-b"), evidence_kind="live")
        with self.assertRaisesRegex(ValueError, "mixed evidence"):
            compare_runs([run_record(), b])

    def test_environment_failure_is_not_a_success_or_zero_latency(self):
        a = modified(
            run_record(status="failed"),
            validation_passed=None,
            failure_code="model_unavailable",
            failure_stage="profile",
        )
        report = compare_runs([a, run_record("model-b")])["summaries"][0]
        self.assertEqual(report["validation_unexecuted"], 1)
        self.assertIsNone(report["median_wall_time_success_seconds"])
        self.assertEqual(report["failure_counts"], {"profile:model_unavailable": 1})

    def test_malformed_records_rejected(self):
        a = run_record()
        for fields in (
            {"wall_time": float("nan")},
            {"wall_time": -1.0},
            {"repeat_index": True},
            {"validation_passed": False},
            {"exit_code": 1},
            {"exit_code": None},
            {"failure_code": "bad"},
            {"unknown": 1},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                modified(a, **fields)

    def test_constructed_invalid_object_revalidated(self):
        a = run_record().model_copy(update={"wall_time": float("inf")})
        with self.assertRaises(ValueError):
            compare_runs([a, run_record("model-b")])

    def test_canonical_hash_and_ambiguous_inputs(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        for bad in ({1: "x"}, {"x": float("nan")}, {"x": float("inf")}):
            with self.assertRaises(ValueError):
                fingerprint(bad)

    def test_adapter_rejects_incomplete_or_mismatched_inputs(self):
        fixture = SUITE.tasks[0]
        result = CodingResult(
            run_id="adapter-control",
            task_id=fixture.task_id,
            job_id="adapter-control",
            model=ModelResult(
                profile=PROFILE.profile_id, provider=PROFILE.provider, model_id=PROFILE.model_id
            ),
            repository=RepositoryResult(),
            result=ResultStatus(status="succeeded", validation_passed=True, exit_code=0),
            metrics=CodingMetrics(wall_time=1, files_changed=1, diff_additions=1, diff_deletions=0),
            artifacts=CodingArtifacts(),
        )
        kwargs = {
            "profile": PROFILE,
            "fixture": fixture,
            "suite_id": SUITE.suite_id,
            "suite_sha256": "a" * 64,
            "checker_sha256": "b" * 64,
            "environment": ENV,
            "budget": {"timeout_seconds": fixture.timeout_seconds},
            "variant": {},
            "variant_label": "none",
            "repeat_index": 1,
            "evidence_kind": "mock",
        }
        self.assertEqual(make_run(result, **kwargs).evidence_kind, "mock")
        for code in (1, None):
            bad = result.model_copy(update={"result": result.result.model_copy(update={"exit_code": code})})
            with self.assertRaisesRegex(ValueError, "exit zero"):
                make_run(bad, **kwargs)
        cases = [
            {"environment": {}},
            {"budget": {}},
            {"budget": {"timeout_seconds": True}},
            {"budget": {"timeout_seconds": fixture.timeout_seconds + 1}},
            {"evidence_kind": "live"},
            {"fixture": SUITE.tasks[1]},
            {"profile": PROFILE.model_copy(update={"model_id": "another-model"})},
        ]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                make_run(result, **{**kwargs, **updates})

    def test_model_configuration_changes_are_fingerprinted(self):
        # Context, reasoning and quantization are part of the independent
        # model-configuration variable, never discarded as cosmetic metadata.
        original = PROFILE.model_dump(
            mode="json", exclude={"profile_id", "notes", "endpoint", "pi_executable"}
        )
        for name, value in (("context_window", 65536), ("reasoning_level", "high"), ("quantization", "Q8")):
            self.assertNotEqual(fingerprint(original), fingerprint({**original, name: value}))

    def test_json_schema_has_closed_record(self):
        schema = BenchmarkRun.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("conditions", schema["required"])

    def test_multiple_tasks_not_pooled(self):
        records = [run_record(model, task=task) for model in ("model-a", "model-b") for task in (0, 1)]
        self.assertEqual(len(compare_runs(records)["summaries"]), 4)

    def test_cli_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps([run_record().model_dump(), run_record("model-b").model_dump()]))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["evidence_kind"], "synthetic")
            path.write_text("{}")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(path)]), 2)

    def test_checked_in_synthetic_fixture(self):
        raw = json.loads((ROOT / "tests/fixtures/benchmark/synthetic-runs.json").read_text())
        report = compare_runs([BenchmarkRun.model_validate(item) for item in raw])
        self.assertEqual(report["evidence_kind"], "synthetic")


if __name__ == "__main__":
    unittest.main()
