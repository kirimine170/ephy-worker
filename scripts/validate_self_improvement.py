#!/usr/bin/env python3
"""Validate a self‑improvement candidate against a baseline.

The script implements the hard gates described in the ephy‑worker
self‑improvement skill:

* baseline and candidate must use identical commands;
* both commands must succeed (exit code 0);
* repository validation (scripts/validate_repository.py) must succeed;
* ``git diff --check`` must succeed;
* any file changed by the candidate must be in the ``--allowed`` list;
* if any gate fails, the script exits with status 1.

The script is intentionally deterministic and side‑effect free apart from
running the two supplied commands and performing the checks.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: str, cwd: Path, name: str) -> subprocess.CompletedProcess[str]:
    """Run *cmd* in *cwd* using the shell.

    Returns the :class:`subprocess.CompletedProcess` object.
    """
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"{name} failed with exit code {result.returncode}", file=sys.stderr)
        print(f"{name} stdout:\n{result.stdout}", file=sys.stderr)
        print(f"{name} stderr:\n{result.stderr}", file=sys.stderr)
    return result


def _git_diff_check(cwd: Path) -> bool:
    """Return True if ``git diff --check`` succeeds.
    """
    result = subprocess.run(["git", "diff", "--check"], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print("git diff --check failed:\n" + result.stdout + result.stderr, file=sys.stderr)
        return False
    return True


def _git_diff_names(cwd: Path) -> set[str]:
    """Return a set of file paths that are modified or untracked.
    """
    tracked = subprocess.run(["git", "diff", "--name-only", "HEAD", "--"], cwd=cwd, capture_output=True, text=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd, capture_output=True, text=True)
    names = set()
    if tracked.stdout:
        names.update(line.strip() for line in tracked.stdout.splitlines() if line.strip())
    if untracked.stdout:
        names.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    return names


def _run_repo_validation(cwd: Path) -> bool:
    """Run the repository validation script.

    Returns True if it exits with status 0.
    """
    validator = cwd / "scripts" / "validate_repository.py"
    result = subprocess.run([sys.executable, str(validator), "--root", str(cwd)], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print("Repository validation failed:\n" + result.stdout + result.stderr, file=sys.stderr)
        return False
    return True


def _run_lint(cwd: Path) -> bool:
    """Run ``ruff check .`` if available.

    Returns True if ruff is not found or if it exits with status 0.
    """
    ruff = shutil.which("ruff")
    if not ruff:
        print("ruff not found; skipping lint check", file=sys.stderr)
        return True
    result = subprocess.run([ruff, "check", "."], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ruff check failed:\n" + result.stdout + result.stderr, file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self‑improvement validator")
    parser.add_argument("--baseline", required=True, help="command to run baseline")
    parser.add_argument("--candidate", required=True, help="command to run candidate")
    parser.add_argument("--allowed", default="", help="comma‑separated list of files that may be changed")
    parser.add_argument("--skip-repo-validation", action="store_true", help="do not run repository validation")
    parser.add_argument("--skip-lint", action="store_true", help="do not run ruff lint check")
    args = parser.parse_args(argv)

    cwd = Path.cwd()

    # Run baseline
    baseline_res = _run(args.baseline, cwd, "baseline")
    # Run candidate
    candidate_res = _run(args.candidate, cwd, "candidate")

    # Check exit codes
    if baseline_res.returncode != 0 or candidate_res.returncode != 0:
        print("One of the commands failed; aborting.", file=sys.stderr)
        return 1

    # Commands must be identical
    if args.baseline.strip() != args.candidate.strip():
        print("Baseline and candidate commands differ; aborting.", file=sys.stderr)
        return 1

    # Git diff --check
    if not _git_diff_check(cwd):
        return 1

    # Scope check
    allowed = {p.strip() for p in args.allowed.split(",") if p.strip()}
    changed = _git_diff_names(cwd)
    if changed:
        outside = changed - allowed
        if outside:
            print("Files changed outside allowed scope: " + ", ".join(sorted(outside)), file=sys.stderr)
            return 1
    else:
        print("No changes detected after candidate run.", file=sys.stderr)
        return 1

    # Repository validation
    if not args.skip_repo_validation and not _run_repo_validation(cwd):
        return 1

    # Lint check
    if not args.skip_lint and not _run_lint(cwd):
        return 1

    print("All hard gates passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
