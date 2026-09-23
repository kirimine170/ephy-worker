#!/usr/bin/env python3
"""Fail-closed evidence gate for one self-improvement candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

EVALUATION_VERSION = "self-improvement-gate-v2"
COMMAND_TIMEOUT_SECONDS = 120


def run(argv: list[str], root: Path, env: dict[str, str] | None = None) -> dict[str, object]:
    started = datetime.now(UTC).isoformat()
    try:
        result = subprocess.run(
            argv, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS, check=False,
        )
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        code, stdout, stderr = 124, str(error.stdout or ""), str(error.stderr or "") + "\nCommand timed out"
    except OSError as error:
        code, stdout, stderr = 127, "", str(error)
    return {
        "argv": argv,
        "cwd": str(root),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "exit_code": code,
        "stdout": stdout,
        "stderr": stderr,
    }


def git(root: Path, *argv: str) -> dict[str, object]:
    return run(["git", *argv], root)


def git_root_error(root: Path) -> str | None:
    result = git(root, "rev-parse", "--show-toplevel")
    if result["exit_code"] != 0:
        return f"Git root unavailable: {result['stderr']}"
    raw = str(result["stdout"]).strip()
    # MSYS Git may return /c/Users/... even when launched by native Python.
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[1].isalpha() and raw[2] == "/":
        raw = f"{raw[1]}:{raw[2:]}"
    reported = Path(raw).resolve()
    if os.path.normcase(str(reported)) != os.path.normcase(str(root.resolve())):
        return f"Directory is not a Git root: {root} (Git reported {reported})"
    return None


def relative_file(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError(f"Expected a repository-relative path: {value}")
    return path.as_posix()


def gate(result: dict[str, object], reason: str) -> dict[str, object]:
    return {"passed": result["exit_code"] == 0, "reason": reason if result["exit_code"] else "", "result": result}


def evaluate(args: argparse.Namespace) -> dict[str, object]:
    baseline = args.baseline_root.resolve()
    candidate = args.candidate_root.resolve()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    evidence: dict[str, object] = {
        "evaluation_version": EVALUATION_VERSION,
        "baseline_root": str(baseline), "candidate_root": str(candidate), "command": command,
        "fixture": args.fixture, "allowed": sorted(set(args.allowed)),
        "python": str(args.python.resolve()) if args.python else None,
        "source_package": args.source_package,
        "unset_env": sorted(set(args.unset_env)),
        "required_modules": sorted(set(args.required_module)),
        "required_files": sorted(set(args.required_file)),
        "conditions": {"passed": False, "reason": ""}, "baseline": None, "candidate": None,
        "preflight": {}, "changed_files": [], "gates": {}, "passed": False,
        "status": "failed", "reason_code": "environment_blocked",
        "baseline_state": "unexecuted", "candidate_state": "unexecuted",
    }

    def reject(reason: str) -> dict[str, object]:
        evidence["conditions"] = {"passed": False, "reason": reason}
        return evidence

    if not command:
        return reject("No correctness command supplied")
    if baseline == candidate:
        return reject("Baseline and candidate must be distinct Git working trees")
    for name, root in (("baseline", baseline), ("candidate", candidate)):
        if not root.is_dir():
            return reject(f"{name} directory does not exist: {root}")
        if error := git_root_error(root):
            return reject(f"{name} {error}")
    heads = {name: git(root, "rev-parse", "HEAD") for name, root in (("baseline", baseline), ("candidate", candidate))}
    if any(item["exit_code"] for item in heads.values()):
        return reject("Could not resolve baseline and candidate HEAD")
    head_values = {name: str(item["stdout"]).strip() for name, item in heads.items()}
    evidence["head"] = head_values
    if args.expected_head and head_values != {"baseline": args.expected_head, "candidate": args.expected_head}:
        return reject("Baseline or candidate HEAD does not match --expected-head")
    if args.require_clean_baseline:
        status = git(baseline, "status", "--porcelain", "--untracked-files=all")
        if status["exit_code"] or str(status["stdout"]).strip():
            return reject("Baseline worktree is not clean")
    for name, root in (("baseline", baseline), ("candidate", candidate)):
        for relative in args.required_file:
            if not (root / relative).is_file():
                return reject(f"Required file missing in {name}: {relative}")
    ruff = args.ruff or shutil.which("ruff")
    if not ruff:
        return reject("Ruff is unavailable")
    ruff_probe = run([ruff, "--version"], baseline)
    evidence["preflight"]["ruff"] = ruff_probe
    if ruff_probe["exit_code"]:
        return reject("Ruff cannot execute")

    envs: dict[str, dict[str, str]] = {}
    for name, root in (("baseline", baseline), ("candidate", candidate)):
        env = os.environ.copy()
        for key in args.unset_env:
            env.pop(key, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PIP_NO_INDEX"] = "1"
        env["UV_OFFLINE"] = "1"
        if args.source_package:
            env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
        envs[name] = env

    if args.python:
        python = args.python.resolve()
        if not python.is_file() or not command or Path(command[0]).resolve() != python:
            return reject("Correctness command must start with the selected existing Python executable")
        probe = run([str(python), "-c", "import sys; print(sys.executable); print(sys.version_info[:2])"], baseline, envs["baseline"])
        evidence["preflight"]["python"] = probe
        if probe["exit_code"]:
            return reject("Selected Python cannot execute")
        for module in args.required_module:
            check = run([str(python), "-c", f"import {module}"], baseline, envs["baseline"])
            evidence["preflight"][f"module:{module}"] = check
            if check["exit_code"]:
                return reject(f"Required Python module unavailable: {module}")
        if args.source_package:
            for name, root in (("baseline", baseline), ("candidate", candidate)):
                check = run([str(python), "-c", f"import {args.source_package}; print({args.source_package}.__file__)"], root, envs[name])
                evidence["preflight"][f"source:{name}"] = check
                source = Path(str(check["stdout"]).strip()).resolve() if check["exit_code"] == 0 else None
                if source is None or root / "src" not in source.parents:
                    return reject(f"{name} does not import {args.source_package} from its own src tree")
    if args.fixed_check:
        fixed = args.fixed_check.resolve()
        if not fixed.is_file() or any(fixed == root or root in fixed.parents for root in (baseline, candidate)):
            return reject("Fixed check must exist outside both measured worktrees")
        evidence["fixed_check"] = {"path": str(fixed), "sha256": hashlib.sha256(fixed.read_bytes()).hexdigest()}

    baseline_fixture = baseline / args.fixture
    candidate_fixture = candidate / args.fixture
    if not baseline_fixture.is_file() or not candidate_fixture.is_file():
        return reject("Fixture is missing in baseline or candidate")
    hashes = {
        "baseline": hashlib.sha256(baseline_fixture.read_bytes()).hexdigest(),
        "candidate": hashlib.sha256(candidate_fixture.read_bytes()).hexdigest(),
    }
    evidence["fixture_sha256"] = hashes
    if hashes["baseline"] != hashes["candidate"]:
        return reject("Baseline and candidate fixture bytes differ")
    evidence["conditions"] = {"passed": True, "reason": ""}

    evidence["baseline"] = run(command, baseline, envs["baseline"])
    evidence["baseline_state"] = "timed_out" if evidence["baseline"]["exit_code"] == 124 else (
        "passed" if evidence["baseline"]["exit_code"] == 0 else "failed"
    )
    if evidence["baseline_state"] == "timed_out":
        evidence["status"] = "timed_out"
        evidence["reason_code"] = "baseline_timed_out"
        return evidence
    output = str(evidence["baseline"]["stdout"]) + str(evidence["baseline"]["stderr"])
    if args.baseline_failure_text and (
        evidence["baseline"]["exit_code"] == 0
        or any(pattern not in output for pattern in args.baseline_failure_text)
    ):
        evidence["conditions"] = {"passed": False, "reason": "Expected baseline failure was not reproduced"}
        evidence["status"] = "verification_failed"
        evidence["reason_code"] = "baseline_not_reproduced"
        return evidence
    evidence["candidate"] = run(command, candidate, envs["candidate"])
    evidence["candidate_state"] = "timed_out" if evidence["candidate"]["exit_code"] == 124 else (
        "passed" if evidence["candidate"]["exit_code"] == 0 else "failed"
    )
    if not baseline_fixture.is_file() or not candidate_fixture.is_file():
        evidence["conditions"] = {"passed": False, "reason": "A measurement removed its fixture"}
    elif (
        hashlib.sha256(baseline_fixture.read_bytes()).hexdigest() != hashes["baseline"]
        or hashlib.sha256(candidate_fixture.read_bytes()).hexdigest() != hashes["candidate"]
    ):
        evidence["conditions"] = {"passed": False, "reason": "A measurement changed its fixture"}
    gates: dict[str, dict[str, object]] = {}
    gates["correctness"] = gate(evidence["candidate"], "Candidate correctness command failed")

    if args.fixed_check:
        fixed_argv = [str(args.python.resolve() if args.python else Path(sys.executable).resolve()), str(args.fixed_check.resolve()), str(baseline), str(candidate)]
        gates["fixed_behavior"] = gate(run(fixed_argv, candidate, envs["candidate"]), "Fixed behavior check failed")
        if not args.fixed_check.resolve().is_file() or hashlib.sha256(args.fixed_check.resolve().read_bytes()).hexdigest() != evidence["fixed_check"]["sha256"]:
            gates["fixed_behavior"]["passed"] = False
            gates["fixed_behavior"]["reason"] = "Fixed check changed during measurement"

    validator = candidate / "scripts" / "validate_repository.py"
    gates["repository"] = (
        gate(run([str(args.python.resolve()) if args.python else sys.executable, str(validator)], candidate, envs["candidate"]), "Repository validation failed")
        if validator.is_file() else {"passed": False, "reason": "Repository validator is missing", "result": None}
    )
    lint_targets = sorted(path for path in set(args.allowed) if path.endswith(".py") and (candidate / path).is_file())
    lint_argv = [ruff, "check", *lint_targets] if lint_targets else [ruff, "--version"]
    gates["lint"] = (
        gate(run(lint_argv, candidate, envs["candidate"]), "Ruff lint failed or could not start")
        if ruff else {"passed": False, "reason": "Ruff is unavailable", "result": None}
    )
    gates["lint"]["targets"] = lint_targets
    gates["lint"]["applicable"] = bool(lint_targets)
    # Measure the *final* state: correctness, repository validation, and lint
    # may themselves create files or alter tracked content.
    changed = git(candidate, "diff", "--name-only", "HEAD", "--")
    untracked = git(candidate, "ls-files", "--others", "--exclude-standard")
    if changed["exit_code"] or untracked["exit_code"]:
        gates["scope"] = {
            "passed": False, "reason": "Git could not enumerate all candidate changes",
            "result": {"tracked": changed, "untracked": untracked},
        }
    else:
        names = set(str(changed["stdout"]).splitlines()) | set(str(untracked["stdout"]).splitlines())
        names.discard("")
        evidence["changed_files"] = sorted(names)
        outside = sorted(names - set(args.allowed))
        gates["scope"] = {
            "passed": not outside,
            "reason": f"Out-of-scope files: {', '.join(outside)}" if outside else "",
            "result": {"changed_files": sorted(names), "outside": outside},
        }
    gates["diff_check"] = gate(git(candidate, "diff", "--check", "HEAD", "--"), "git diff --check failed")
    evidence["gates"] = gates
    evidence["passed"] = bool(evidence["conditions"]["passed"]) and all(
        bool(item["passed"]) for item in gates.values()
    )
    evidence["status"] = "review_ready" if evidence["passed"] else (
        "timed_out" if evidence["candidate_state"] == "timed_out" else "verification_failed"
    )
    evidence["reason_code"] = "passed" if evidence["passed"] else (
        "candidate_timed_out" if evidence["candidate_state"] == "timed_out" else "candidate_gates_failed"
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", required=True, type=Path)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=relative_file)
    parser.add_argument("--allowed", action="append", type=relative_file, default=[])
    parser.add_argument("--ruff", help="Explicit Ruff executable; unavailable paths fail closed")
    parser.add_argument("--python", type=Path, help="Absolute Python executable used for both measurements and preflight")
    parser.add_argument("--source-package", help="Package that must import from each worktree's src directory")
    parser.add_argument("--required-module", action="append", default=[], help="Import required before either measurement")
    parser.add_argument("--required-file", action="append", type=relative_file, default=[], help="File required in each worktree before measurement")
    parser.add_argument("--unset-env", action="append", default=[], help="Environment variable removed for both measurements")
    parser.add_argument("--expected-head", help="Required identical HEAD commit")
    parser.add_argument("--require-clean-baseline", action="store_true")
    parser.add_argument("--baseline-failure-text", action="append", default=[], help="Literal text required in failing baseline output")
    parser.add_argument("--fixed-check", type=Path, help="Immutable evaluator script outside both worktrees")
    parser.add_argument("--evidence", type=Path, help="Optional JSON evidence output path")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="One argv used in both roots; prefix it with --")
    args = parser.parse_args()
    if args.evidence:
        output = args.evidence.resolve()
        for root in (args.baseline_root.resolve(), args.candidate_root.resolve()):
            if output == root or root in output.parents:
                parser.error("--evidence must be outside both measured working trees")
    try:
        evidence = evaluate(args)
    except KeyboardInterrupt:
        evidence = {
            "evaluation_version": EVALUATION_VERSION, "status": "cancelled", "reason_code": "user_cancelled",
            "baseline_state": "unexecuted", "candidate_state": "unexecuted", "passed": False,
        }
    encoded = json.dumps(evidence, ensure_ascii=True, indent=2) + "\n"
    if args.evidence:
        args.evidence.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
