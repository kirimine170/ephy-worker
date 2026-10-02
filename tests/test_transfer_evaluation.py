"""Synthetic transfer controls; no model invocation or transfer-effect claim."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ephy_worker.coding_profiles import load_coding_profiles
from ephy_worker.evaluation import create_fixture_repository, load_suite
from ephy_worker.transfer_evaluation import (
    ASSETS,
    TransferSuite,
    comparison_plan,
    freeze_transfer_suite,
    main,
    run_controls,
    validate_candidate,
    variant_manifest,
)

ENV = {"platform": "synthetic-linux", "python": "3.12", "dependencies": "lock-v1", "executor": "planned"}
BUDGET = {"timeout_seconds": 10, "max_turns": 8, "max_tool_calls": 16}


class TransferEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        original = freeze_transfer_suite()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        for name, data in original.assets:
            (self.assets / name).write_bytes(data)
        self.frozen = freeze_transfer_suite(self.assets)
        self.profile = load_coding_profiles()["mock"]

    def candidate(self, good=True):
        task = self.frozen.suite.tasks[0]
        repository, _ = create_fixture_repository(task.fixture)
        self.addCleanup(shutil.rmtree, repository)
        if good:
            for change in task.fixture.mock_changes:
                (repository / change.path).write_text(change.content)
        return task.fixture.task_id, repository

    def plan(self, **changes):
        args = {
            "split": "development",
            "profile": self.profile,
            "environment": ENV,
            "budget": BUDGET,
            "repeat": 2,
        }
        args.update(changes)
        return comparison_plan(self.frozen, **args)

    def test_six_distinct_tasks_have_disjoint_splits(self):
        suite = self.frozen.suite
        development = {t.fixture.task_id for t in suite.tasks if t.split == "development"}
        heldout = {t.fixture.task_id for t in suite.tasks if t.split == "heldout"}
        self.assertEqual((len(development), len(heldout)), (3, 3))
        self.assertFalse(development & heldout)
        self.assertFalse((development | heldout) & {t.task_id for t in load_suite("smoke").tasks})
        self.assertEqual(
            {t.family for t in suite.tasks if t.split == "development"},
            {t.family for t in suite.tasks if t.split == "heldout"},
        )

    def test_all_thirty_known_controls(self):
        report = run_controls(self.frozen)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["controls"]), 30)
        self.assertEqual(report["transfer_effect"], "unmeasured")
        self.assertEqual(report["formal_workflow"], "unverified")
        for row in report["controls"]:
            self.assertEqual(row["verdict"]["passed"], row["control"] == "known_good")

    def test_frozen_asset_mutation_rejected_before_execution(self):
        for name in ASSETS:
            path = self.assets / name
            before = path.read_bytes()
            path.write_bytes(before + b"\n")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "frozen asset"):
                self.frozen.verify()
            path.write_bytes(before)

    def test_frozen_asset_removal_and_symlink_rejected(self):
        path = self.assets / "checker.py"
        data = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "frozen asset"):
            self.frozen.verify()
        elsewhere = self.root / "replacement.py"
        elsewhere.write_bytes(data)
        path.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, "frozen asset"):
            self.frozen.verify()
        with self.assertRaisesRegex(ValueError, "symbolic"):
            freeze_transfer_suite(self.assets)

    def test_in_memory_mutations_do_not_change_frozen_suite(self):
        suite = self.frozen.suite
        suite.tasks[0].allowed_files.append("tests/test_behavior.py")
        self.assertEqual(len(self.frozen.suite.tasks[0].allowed_files), 1)

    def test_balanced_plan_freezes_all_nonvariant_conditions(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "plan_only")
        self.assertEqual(len(plan["runs"]), 18)
        for key in ("suite_sha256", "checker_sha256", "model_sha256", "budget_sha256", "environment_sha256"):
            self.assertEqual(len({row[key] for row in plan["runs"]}), 1)
        self.assertEqual({row["variant_label"] for row in plan["runs"]}, {"none", "skill", "tool"})
        self.assertEqual([row["variant_label"] for row in plan["runs"][:3]], ["none", "skill", "tool"])
        self.assertEqual([row["variant_label"] for row in plan["runs"][9:12]], ["skill", "tool", "none"])
        self.assertEqual(len({row["variant_sha256"] for row in plan["runs"]}), 3)

    def test_model_requests_exclude_solutions_checks_and_other_split(self):
        plan = self.plan()
        self.assertNotIn("heldout-", json.dumps(plan))
        for row in plan["runs"]:
            request = row["request"]
            self.assertNotIn("mock_changes", request)
            self.assertNotIn("checker", request)
            self.assertFalse(any(p.startswith("tests/") for p in request["files"]))
            task = self.frozen.task(row["task_id"])
            for change in task.fixture.mock_changes:
                self.assertNotIn(change.content, request["files"].values())
        heldout = self.plan(split="heldout")
        self.assertTrue(all(row["split"] == "heldout" for row in heldout["runs"]))
        for variant in ("none", "skill", "tool"):
            before = next(row["variant"] for row in plan["runs"] if row["variant_label"] == variant)
            after = next(row["variant"] for row in heldout["runs"] if row["variant_label"] == variant)
            self.assertEqual(before, after)

    def test_invalid_plan_and_unknown_tasks_fail_closed(self):
        for changes in (
            {"split": "all"},
            {"repeat": 0},
            {"repeat": True},
            {"environment": {}},
            {"budget": {"timeout_seconds": 99}},
            {"budget": {"timeout_seconds": True}},
            {"budget": {"timeout_seconds": 10, "tokens": float("nan")}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)
        with self.assertRaises(ValueError):
            self.frozen.task("unknown")
        with self.assertRaises(ValueError):
            variant_manifest(self.frozen, "both")

    def test_duplicate_split_and_out_of_scope_good_control_rejected(self):
        raw = self.frozen.suite.model_dump(mode="json")
        raw["tasks"][3]["fixture"]["task_id"] = raw["tasks"][0]["fixture"]["task_id"]
        with self.assertRaises(ValueError):
            TransferSuite.model_validate(raw)
        raw = self.frozen.suite.model_dump(mode="json")
        raw["tasks"][0]["allowed_files"] = ["tests/test_behavior.py"]
        with self.assertRaises(ValueError):
            TransferSuite.model_validate(raw)

    def test_early_zero_exit_cannot_pass_fixed_checker(self):
        task_id, repository = self.candidate()
        (repository / "batches.py").write_text("import os\nos._exit(0)\n")
        verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertEqual(verdict["exit_code"], 0)
        self.assertFalse(verdict["passed"])

    def test_candidate_modification_during_check_rejected(self):
        task_id, repository = self.candidate()
        path = repository / "batches.py"
        path.write_text(
            path.read_text() + "\nfrom pathlib import Path\nPath(__file__).write_text('# mutated\\n')\n"
        )
        verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(verdict["correctness_passed"])
        self.assertFalse(verdict["candidate_unchanged"])
        self.assertEqual(verdict["status"], "integrity_failure")

    def test_checker_copy_modification_during_check_rejected(self):
        task_id, repository = self.candidate()
        real_run = subprocess.run

        def mutate(command, **kwargs):
            result = real_run(command, **kwargs)
            Path(command[3]).write_text("# checker replaced\n")
            return result

        with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
            verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertFalse(verdict["checker_unchanged"])
        self.assertFalse(verdict["passed"])

    def test_scope_checked_before_fixed_test_exec(self):
        task_id, repository = self.candidate()
        (repository / "tests/test_behavior.py").write_text("raise RuntimeError('must never run')")
        with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
            verdict = validate_candidate(self.frozen, task_id, repository)
        run.assert_not_called()
        self.assertEqual(verdict["status"], "scope_failure")
        self.assertFalse(verdict["passed"])

    def test_symlink_and_ignored_untracked_candidate_rejected(self):
        task_id, repository = self.candidate()
        (repository / "cache.pyc").write_bytes(b"extra")
        verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertFalse(verdict["passed"])
        (repository / "cache.pyc").unlink()
        (repository / "batches.py").unlink()
        (repository / "batches.py").symlink_to(self.assets / "checker.py")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            validate_candidate(self.frozen, task_id, repository)

    def test_timeout_and_environment_errors_are_not_pass(self):
        task_id, repository = self.candidate()
        for error, expected in (
            (subprocess.TimeoutExpired("check", 1), "execution_timeout"),
            (OSError("missing"), "environment_failure"),
        ):
            with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=error):
                verdict = validate_candidate(self.frozen, task_id, repository)
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["status"], expected)

    def test_tool_only_reads_supplied_source(self):
        source = "def f(x):\n    if x > 0:\n        return x\n"
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(self.assets / "inspect_python.py")],
            input=json.dumps({"source": source}),
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["functions"][0]["name"], "f")
        self.assertEqual(payload["comparisons"][0]["expression"], "x > 0")
        invalid = subprocess.run(
            [sys.executable, "-I", "-B", str(self.assets / "inspect_python.py")],
            input=json.dumps({"path": "/etc/passwd"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)

    def test_lookup_rejects_ignored_key_and_mutated_stored_value(self):
        task = self.frozen.task("heldout-present-value")
        wrong_implementations = [
            (
                "def present_value(mapping, key, fallback):\n"
                "    return next(iter(mapping.values())) if mapping else fallback\n"
            ),
            (
                "def present_value(mapping, key, fallback):\n"
                "    value = mapping[key] if key in mapping else fallback\n"
                "    if isinstance(value, list):\n"
                "        value.append(99)\n"
                "    return value\n"
            ),
        ]
        for source in wrong_implementations:
            with self.subTest(source=source):
                repository, _ = create_fixture_repository(task.fixture)
                try:
                    (repository / "lookup.py").write_text(source)
                    verdict = validate_candidate(self.frozen, task.fixture.task_id, repository)
                    self.assertFalse(verdict["passed"])
                    self.assertFalse(verdict["correctness_passed"])
                finally:
                    shutil.rmtree(repository)

    def test_cli_controls_report_is_synthetic(self):
        with (
            patch(
                "ephy_worker.transfer_evaluation.run_controls",
                return_value={"passed": True, "evidence_kind": "synthetic"},
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["controls"]), 0)
        self.assertEqual(json.loads(output.getvalue())["evidence_kind"], "synthetic")


if __name__ == "__main__":
    unittest.main()
