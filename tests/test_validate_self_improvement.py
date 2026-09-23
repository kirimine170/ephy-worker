"""Integration tests for the independent self-improvement hard gate."""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_self_improvement.py"
CHECK_GOOD = (
    "from pathlib import Path; import sys; "
    "sys.exit(Path('tracked.txt').read_text(encoding='utf-8') != 'good\\n')"
)


class ValidateSelfImprovementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = self.root / "baseline"
        self.candidate = self.root / "candidate"
        self.ruff = shutil.which("ruff") or str(ROOT / ".venv" / "Scripts" / "ruff.exe")
        for repo in (self.baseline, self.candidate):
            repo.mkdir()
            self.git(repo, "init", "--initial-branch=main")
            self.git(repo, "config", "user.name", "Validator Test")
            self.git(repo, "config", "user.email", "validator@example.invalid")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "validate_repository.py").write_text(
                "import sys\nfrom pathlib import Path\n\n"
                "if Path('bad-repository.flag').exists():\n    sys.exit(1)\n"
                "if Path('write-late.flag').exists():\n    Path('late-file.txt').write_text('late', encoding='utf-8')\n",
                encoding="utf-8",
            )
            (repo / "fixture.txt").write_bytes(b"fixed input\n")
            (repo / "tracked.txt").write_bytes(b"bad\n")
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-m", "fixture")

    def git(self, repo: Path, *arguments: str) -> None:
        result = subprocess.run(["git", *arguments], cwd=repo, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def invoke(
        self,
        *,
        command: list[str] | None = None,
        allowed: tuple[str, ...] = ("tracked.txt",),
        candidate: Path | None = None,
        ruff: str | None = None,
        extra: tuple[str, ...] = (),
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        args = [
            sys.executable, str(SCRIPT),
            "--baseline-root", str(self.baseline),
            "--candidate-root", str(candidate or self.candidate),
            "--fixture", "fixture.txt",
            "--ruff", ruff or self.ruff,
            *extra,
        ]
        for name in allowed:
            args.extend(["--allowed", name])
        args.extend(["--", *(command or [sys.executable, "-c", CHECK_GOOD])])
        result = subprocess.run(
            args, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
        self.assertTrue(result.stdout, result.stderr)
        return result, json.loads(result.stdout)

    def test_success_records_distinct_evidence_with_same_command(self) -> None:
        (self.candidate / "tracked.txt").write_bytes(b"good\n")
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 0, evidence)
        self.assertFalse(evidence["baseline"]["exit_code"] == 0)
        self.assertEqual(evidence["candidate"]["exit_code"], 0)
        self.assertEqual(evidence["baseline"]["argv"], evidence["candidate"]["argv"])
        self.assertEqual(evidence["fixture_sha256"]["baseline"], evidence["fixture_sha256"]["candidate"])
        self.assertTrue(all(item["passed"] for item in evidence["gates"].values()))
        self.assertEqual(evidence["status"], "review_ready")
        self.assertEqual(evidence["baseline_state"], "failed")
        self.assertEqual(evidence["candidate_state"], "passed")

    def test_candidate_failure_is_not_excused_by_baseline(self) -> None:
        result, evidence = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(evidence["gates"]["correctness"]["passed"])
        self.assertIn("Candidate correctness", evidence["gates"]["correctness"]["reason"])
        self.assertEqual(evidence["status"], "verification_failed")

    def test_same_root_is_rejected_before_measurement(self) -> None:
        result, evidence = self.invoke(candidate=self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distinct Git working trees", evidence["conditions"]["reason"])
        self.assertIsNone(evidence["baseline"])

    def test_changed_fixture_is_rejected_before_measurement(self) -> None:
        (self.candidate / "fixture.txt").write_text("changed input\n", encoding="utf-8")
        result, evidence = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture bytes differ", evidence["conditions"]["reason"])
        self.assertIsNone(evidence["candidate"])

    def test_measurement_cannot_mutate_its_fixture(self) -> None:
        command = [
            sys.executable, "-c",
            "from pathlib import Path; Path('fixture.txt').write_bytes(b'changed input\\n')",
        ]
        result, evidence = self.invoke(command=command, allowed=("fixture.txt",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed its fixture", evidence["conditions"]["reason"])

    def test_untracked_out_of_scope_file_is_rejected(self) -> None:
        (self.candidate / "tracked.txt").write_bytes(b"good\n")
        (self.candidate / "extra.txt").write_text("extra\n", encoding="utf-8")
        result, evidence = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra.txt", evidence["gates"]["scope"]["reason"])

    def test_git_diff_check_failure_is_rejected(self) -> None:
        (self.candidate / "tracked.txt").write_bytes(b"good   \n")
        result, evidence = self.invoke(command=[sys.executable, "-c", "pass"])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(evidence["gates"]["diff_check"]["passed"])

    def test_repository_validation_failure_is_rejected(self) -> None:
        (self.candidate / "bad-repository.flag").write_text("bad\n", encoding="utf-8")
        result, evidence = self.invoke(command=[sys.executable, "-c", "pass"], allowed=("bad-repository.flag",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Repository validation failed", evidence["gates"]["repository"]["reason"])

    def test_scope_is_checked_after_all_other_gates(self) -> None:
        (self.candidate / "write-late.flag").write_bytes(b"yes\n")
        result, evidence = self.invoke(command=[sys.executable, "-c", "pass"], allowed=("write-late.flag",))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(evidence["gates"]["repository"]["passed"])
        self.assertIn("late-file.txt", evidence["gates"]["scope"]["reason"])

    def test_ruff_failure_is_rejected(self) -> None:
        (self.candidate / "bad.py").write_text("import os\n", encoding="utf-8")
        result, evidence = self.invoke(command=[sys.executable, "-c", "pass"], allowed=("bad.py",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ruff lint failed", evidence["gates"]["lint"]["reason"])

    def test_unrelated_preexisting_lint_debt_does_not_block_candidate(self) -> None:
        for repo in (self.baseline, self.candidate):
            (repo / "legacy.py").write_bytes(b"import os\n")
            self.git(repo, "add", "legacy.py")
            self.git(repo, "commit", "-m", "preexisting lint debt")
        (self.candidate / "tracked.txt").write_bytes(b"good\n")
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 0, evidence)
        self.assertEqual(evidence["gates"]["lint"]["targets"], [])
        self.assertFalse(evidence["gates"]["lint"]["applicable"])

    def test_missing_ruff_is_not_a_pass(self) -> None:
        result, evidence = self.invoke(command=[sys.executable, "-c", "pass"], ruff=str(self.root / "missing-ruff"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(evidence["preflight"]["ruff"]["exit_code"], 127)
        self.assertEqual(evidence["reason_code"], "environment_blocked")
        self.assertIsNone(evidence["baseline"])

    def test_non_git_candidate_is_rejected(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        result, evidence = self.invoke(candidate=plain)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git root unavailable", evidence["conditions"]["reason"])

    def test_expected_baseline_failure_must_reproduce_before_candidate(self) -> None:
        (self.candidate / "tracked.txt").write_bytes(b"good\n")
        result, evidence = self.invoke(extra=("--baseline-failure-text", "UnicodeDecodeError"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(evidence["reason_code"], "baseline_not_reproduced")
        self.assertIsNone(evidence["candidate"])
        self.assertEqual(evidence["candidate_state"], "unexecuted")

    def test_python_preflight_rejects_missing_interpreter(self) -> None:
        result, evidence = self.invoke(extra=("--python", str(self.root / "missing-python")))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(evidence["reason_code"], "environment_blocked")
        self.assertIsNone(evidence["baseline"])
        self.assertEqual(evidence["baseline_state"], "unexecuted")

    def test_required_module_preflight_rejects_missing_dependency(self) -> None:
        result, evidence = self.invoke(
            extra=("--python", sys.executable, "--required-module", "module_that_cannot_exist_123")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required Python module unavailable", evidence["conditions"]["reason"])
        self.assertIsNone(evidence["baseline"])

    def test_expected_head_rejects_stale_worktree(self) -> None:
        result, evidence = self.invoke(extra=("--expected-head", "0" * 40))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match --expected-head", evidence["conditions"]["reason"])

    def test_cancelled_evaluation_uses_existing_job_status(self) -> None:
        main = runpy.run_path(str(SCRIPT))["main"]
        output = io.StringIO()
        arguments = [
            str(SCRIPT), "--baseline-root", str(self.baseline), "--candidate-root", str(self.candidate),
            "--fixture", "fixture.txt", "--", sys.executable, "-c", "pass",
        ]
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.dict(main.__globals__, {"evaluate": mock.Mock(side_effect=KeyboardInterrupt)}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(), 1)
        evidence = json.loads(output.getvalue())
        self.assertEqual(evidence["status"], "cancelled")
        self.assertEqual(evidence["candidate_state"], "unexecuted")


if __name__ == "__main__":
    unittest.main()
