#!/usr/bin/env python3
"""Self‑improvement validation script.

This script is used by the test suite to validate a self‑improvement
process.  It runs a baseline command and a candidate command, checks for
unexpected new files, tab characters, repository structure, and linting.

The script accepts the following command‑line arguments:

  --baseline <cmd>          Command to run for the baseline.
  --candidate <cmd>         Command to run for the candidate.
  --allowed <files>         Comma‑separated list of allowed new files.
  --skip-repo-validation    Skip running the repository validation script.
  --skip-lint                Skip running the lint tool.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Set

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def list_files(root: Path) -> Set[Path]:
    """Return a set of all file paths relative to *root*.

    Hidden files and common temporary directories are ignored.  The
    comparison is performed on relative paths to keep the logic simple.
    """
    files: Set[Path] = set()
    # Exclude common temporary directories
    exclude_dirs = {".venv", "venv", ".git", ".ruff_cache", ".pytest_cache", "__pycache__"}
    for path in root.rglob("*"):
        if path.is_file():
            # Skip files inside excluded directories
            if any(part in exclude_dirs for part in path.parts):
                continue
            files.add(path.relative_to(root))
    return files


def read_text(file: Path) -> str:
    """Return the content of *file* decoded as UTF‑8.

    Decoding errors are replaced to avoid failures when binary data is
    encountered.
    """
    try:
        return file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def run_command(cmd: str, cwd: Path) -> int:
    """Run *cmd* in a shell and return its exit code.

    The command is executed with ``shell=True`` to allow complex shell
    expressions.  Output is discarded; any errors are captured via the
    process object and printed to stderr if the command fails.
    """
    result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        sys.stderr.write(f"Command failed: {cmd}\n")
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
    return result.returncode

# ---------------------------------------------------------------------------
# Main validation logic
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a self‑improvement run.")
    parser.add_argument("--baseline", required=True, help="Command for baseline run")
    parser.add_argument("--candidate", required=True, help="Command for candidate run")
    parser.add_argument("--allowed", default="", help="Comma‑separated list of allowed new files")
    parser.add_argument("--skip-repo-validation", action="store_true", help="Skip repository validation")
    parser.add_argument("--skip-lint", action="store_true", help="Skip linting with ruff")

    args = parser.parse_args()

    repo_root = Path.cwd()

    allowed_set: Set[Path] = set()
    if args.allowed:
        allowed_set = {Path(p.strip()) for p in args.allowed.split(",") if p.strip()}

    # Run baseline
    if run_command(args.baseline, repo_root) != 0:
        sys.exit(1)
    after_baseline = list_files(repo_root)

    # Run candidate
    if run_command(args.candidate, repo_root) != 0:
        sys.exit(1)
    after_candidate = list_files(repo_root)

    # Detect new files created by candidate
    new_files = after_candidate - after_baseline
    for f in new_files:
        if f not in allowed_set:
            sys.stderr.write(f"Unexpected new file: {f}\n")
            sys.exit(1)
        # Special rule: fail if a new file contains exactly the string "diff"
        content = read_text(repo_root / f)
        if content == "diff":
            sys.stderr.write(f"New file {f} contains disallowed content 'diff'\n")
            sys.exit(1)

    # Detect disallowed content "diff" in any file after candidate
    for f in after_candidate:
        content = read_text(repo_root / f)
        if content == "diff":
            sys.stderr.write(f"File {f} contains disallowed content 'diff'\n")
            sys.exit(1)

    # Check for tab characters in all files after candidate
    for f in after_candidate:
        content = read_text(repo_root / f)
        if "\t" in content:
            sys.stderr.write(f"Tab character found in file: {f}\n")
            sys.exit(1)

    # Run repository validation unless skipped
    if not args.skip_repo_validation:
        repo_validator = repo_root / "scripts" / "validate_repository.py"
        if repo_validator.exists():
            if run_command(f"python {repo_validator}", repo_root) != 0:
                sys.exit(1)
        else:
            sys.stderr.write("Repository validation script not found.")
            sys.exit(1)

    # Run linting unless skipped
    if not args.skip_lint:
        ruff_cmd = "ruff check . --exclude tests"
        if run_command(ruff_cmd, repo_root) != 0:
            sys.exit(1)

    # All checks passed
    sys.exit(0)


if __name__ == "__main__":
    main()
