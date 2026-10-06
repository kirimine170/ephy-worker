from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "validate_self_improvement.py"

class ValidateSelfImprovementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        shutil.copytree(REPOSITORY_ROOT, self.repo, ignore=shutil.ignore_patterns(".git", ".venv", "venv", ".ruff_cache", ".pytest_cache", "__pycache__", "*.pyc"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_validator(self, baseline_cmd: str, candidate_cmd: str, allowed: str = "", skip_repo_validation: bool = False, skip_lint: bool = False):
        args = [str(SCRIPT_PATH), "--baseline", baseline_cmd, "--candidate", candidate_cmd]
        if allowed:
            args.extend(["--allowed", allowed])
        if skip_repo_validation:
            args.append("--skip-repo-validation")
        if skip_lint:
            args.append("--skip-lint")
        return subprocess.run(args, cwd=self.repo, text=True, capture_output=True, check=False)

    def test_successful_validation(self):
        baseline = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('baseline')\""
        candidate = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('candidate')\""
        result = self.run_validator(baseline, candidate, allowed="test.txt")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scope_violation(self):
        baseline = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('baseline')\""
        candidate = "python -c \"import pathlib; pathlib.Path('extra.txt').write_text('extra')\""
        result = self.run_validator(baseline, candidate, allowed="test.txt")
        self.assertNotEqual(result.returncode, 0)

    def test_command_mismatch(self):
        baseline = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('baseline')\""
        candidate = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('diff')\""
        # Commands are different but we also need to ensure allowed file
        result = self.run_validator(baseline, candidate, allowed="test.txt")
        self.assertNotEqual(result.returncode, 0)

    def test_nonzero_exit(self):
        baseline = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('baseline')\""
        candidate = "python -c \"import sys; sys.exit(1)\""
        result = self.run_validator(baseline, candidate, allowed="test.txt")
        self.assertNotEqual(result.returncode, 0)

    def test_git_diff_check_failure(self):
        baseline = "python -c \"import pathlib; pathlib.Path('bad.txt').write_text('a\tb')\""
        candidate = "python -c \"import pathlib; pathlib.Path('bad.txt').write_text('a\tb')\""
        result = self.run_validator(baseline, candidate, allowed="bad.txt")
        self.assertNotEqual(result.returncode, 0)

    def test_repository_validation_failure(self):
        baseline = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('baseline')\""
        candidate = "python -c \"import pathlib; pathlib.Path('test.txt').write_text('candidate')\""
        # Corrupt project.yaml to fail validation
        project_yaml = self.repo / ".ephy" / "project.yaml"
        content = project_yaml.read_text(encoding='utf-8')
        project_yaml.write_text(content.replace('type: "core"', 'type: "unknown"'), encoding='utf-8')
        result = self.run_validator(baseline, candidate, allowed="test.txt")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
