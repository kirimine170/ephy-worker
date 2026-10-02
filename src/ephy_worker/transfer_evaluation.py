"""Frozen synthetic transfer fixtures and model-free comparison interfaces.

This module neither starts models nor establishes transfer gains or formal audit.
Candidate code must run in a separately verified sandbox before live use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .coding_schema import CodingFixture, CodingModelProfile, safe_relative_path
from .evaluation import create_fixture_repository
from .schema import StrictModel

Split = Literal["development", "heldout"]
Variant = Literal["none", "skill", "tool"]
ASSETS = ("suite.json", "checker.py", "skill.md", "inspect_python.py")
VARIANTS = ("none", "skill", "tool")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_digest(value: object) -> str:
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
    return _digest(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    )


class TransferTask(StrictModel):
    split: Split
    family: str = Field(min_length=1)
    allowed_files: list[str] = Field(min_length=1)
    expected_tests: int = Field(ge=1)
    fixture: CodingFixture

    @model_validator(mode="after")
    def scope_is_implementation_only(self):
        if len(self.allowed_files) != len(set(self.allowed_files)):
            raise ValueError("allowed files must be unique")
        for path in self.allowed_files:
            safe_relative_path(path)
            if path not in self.fixture.files or path.startswith("tests/") or not path.endswith(".py"):
                raise ValueError("allowed files must be existing implementation Python files")
        if any(change.path not in self.allowed_files for change in self.fixture.mock_changes):
            raise ValueError("known-good control is outside implementation scope")
        if not any(path.startswith("tests/test_") for path in self.fixture.files):
            raise ValueError("fixed tests are required")
        return self


class TransferSuite(StrictModel):
    schema_version: Literal[1]
    suite_id: Literal["transfer-v1"]
    tasks: list[TransferTask] = Field(min_length=2)

    @model_validator(mode="after")
    def separated_tasks(self):
        ids = [task.fixture.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task IDs must be unique across splits")
        if {task.split for task in self.tasks} != {"development", "heldout"}:
            raise ValueError("development and heldout tasks are required")
        fingerprints = [_json_digest(task.fixture.files) for task in self.tasks]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("duplicate task contents cannot form separate splits")
        return self


@dataclass(frozen=True)
class FrozenTransferSuite:
    root: Path
    assets: tuple[tuple[str, bytes], ...]
    harness: bytes

    @property
    def suite(self) -> TransferSuite:
        # Fresh models prevent a caller mutating the frozen definition in memory.
        return TransferSuite.model_validate_json(dict(self.assets)["suite.json"])

    @property
    def suite_sha256(self) -> str:
        return _digest(dict(self.assets)["suite.json"])

    @property
    def checker_sha256(self) -> str:
        return _json_digest(
            {
                "checker": _digest(dict(self.assets)["checker.py"]),
                "harness": _digest(self.harness),
                "fixed_tests": {
                    task.fixture.task_id: {
                        path: _digest(content.encode())
                        for path, content in task.fixture.files.items()
                        if path.startswith("tests/")
                    }
                    for task in self.suite.tasks
                },
            }
        )

    def verify(self) -> None:
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError("frozen suite root changed to symbolic link")
        for name, data in self.assets:
            path = self.root / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise ValueError(f"frozen asset changed or missing: {name}")
        if Path(__file__).read_bytes() != self.harness:
            raise ValueError("frozen evaluation harness changed")

    def task(self, task_id: str) -> TransferTask:
        self.verify()
        for task in self.suite.tasks:
            if task.fixture.task_id == task_id:
                return task
        raise ValueError(f"unknown transfer task: {task_id}")


def freeze_transfer_suite(root: Path | None = None) -> FrozenTransferSuite:
    root = root or Path(__file__).parent / "fixtures" / "coding" / "transfer"
    root = root.absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("suite root cannot traverse a symbolic link")
    if any((root / name).is_symlink() for name in ASSETS):
        raise ValueError("suite assets cannot be symbolic links")
    frozen = FrozenTransferSuite(
        root, tuple((name, (root / name).read_bytes()) for name in ASSETS), Path(__file__).read_bytes()
    )
    _ = frozen.suite
    frozen.verify()
    return frozen


def variant_manifest(frozen: FrozenTransferSuite, variant: Variant) -> dict:
    frozen.verify()
    if variant not in VARIANTS:
        raise ValueError("unknown assistance variant")
    assets = dict(frozen.assets)
    name = {"none": None, "skill": "skill.md", "tool": "inspect_python.py"}[variant]
    return {"kind": variant, "asset": name, "sha256": _digest(assets[name]) if name else None}


def comparison_plan(
    frozen: FrozenTransferSuite,
    *,
    split: Split,
    profile: CodingModelProfile,
    environment: dict,
    budget: dict,
    repeat: int = 1,
) -> dict:
    """Create a balanced same-model plan, without running or scoring anything.

    Model-visible requests omit controls, checkers and evaluation assertions.
    Heldout is a workflow split in a public synthetic set, not secret data.
    """
    frozen.verify()
    if split not in {"development", "heldout"} or type(repeat) is not int or not 1 <= repeat <= 100:
        raise ValueError("choose one split and repeat between 1 and 100")
    for key in ("platform", "python", "dependencies", "executor"):
        if not isinstance(environment.get(key), str) or not environment[key].strip():
            raise ValueError(f"environment requires {key}")
    timeout = budget.get("timeout_seconds")
    if type(timeout) not in {int, float} or any(
        task.fixture.timeout_seconds != timeout for task in frozen.suite.tasks if task.split == split
    ):
        raise ValueError("budget timeout_seconds must match every selected fixture")
    common = {
        "suite_id": frozen.suite.suite_id,
        "suite_sha256": frozen.suite_sha256,
        "checker_sha256": frozen.checker_sha256,
        "split": split,
        "environment_sha256": _json_digest(environment),
        "budget_sha256": _json_digest(budget),
        "model_sha256": _json_digest(
            profile.model_dump(mode="json", exclude={"profile_id", "notes", "endpoint", "pi_executable"})
        ),
    }
    runs = []
    for iteration in range(1, repeat + 1):
        for task in frozen.suite.tasks:
            if task.split != split:
                continue
            # Rotate variant ordering to avoid always placing assistance last.
            order = VARIANTS[(iteration - 1) % 3 :] + VARIANTS[: (iteration - 1) % 3]
            for variant in order:
                manifest = variant_manifest(frozen, variant)
                request = {
                    "task_id": task.fixture.task_id,
                    "goal": task.fixture.description,
                    "allowed_files": task.allowed_files,
                    "files": {p: c for p, c in task.fixture.files.items() if not p.startswith("tests/")},
                }
                if variant == "skill":
                    request["skill"] = dict(frozen.assets)["skill.md"].decode("utf-8")
                elif variant == "tool":
                    request["tool"] = {
                        "name": "inspect_python",
                        "description": "Read-only Python AST inspection",
                        "request": {"source": "Python source string (at most 65536 UTF-8 bytes)"},
                        "source": dict(frozen.assets)["inspect_python.py"].decode("utf-8"),
                    }
                runs.append(
                    {
                        **common,
                        "task_id": task.fixture.task_id,
                        "family": task.family,
                        "task_sha256": _json_digest(task.fixture.model_dump(mode="json")),
                        "repeat_index": iteration,
                        "variant_label": variant,
                        "variant": manifest,
                        "variant_sha256": _json_digest(manifest),
                        "request": request,
                    }
                )
    return {
        "schema_version": 1,
        "status": "plan_only",
        "evidence_kind": "synthetic",
        "transfer_effect": "unmeasured",
        "formal_workflow": "unverified",
        "runs": runs,
    }


def _candidate_files(root: Path) -> dict[str, bytes]:
    if any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
        raise ValueError("candidate must be a real directory")
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"candidate contains symbolic link: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"candidate contains non-regular or hardlinked file: {relative}")
        files[relative.as_posix()] = path.read_bytes()
    return files


def validate_candidate(frozen: FrozenTransferSuite, task_id: str, candidate: Path) -> dict:
    """Check implementation scope and fixed behavior outside candidate tests.

    For trusted synthetic controls only. Hash checks detect ordinary mutation;
    they do not isolate hostile code or prevent transient changes/restoration.
    """
    task = frozen.task(task_id)
    candidate = candidate.absolute()
    if candidate == frozen.root or candidate in frozen.root.parents or frozen.root in candidate.parents:
        raise ValueError("fixed suite must be outside the candidate")
    expected = {path: content.encode() for path, content in task.fixture.files.items()}
    before = _candidate_files(candidate)
    changed = sorted(path for path in set(before) | set(expected) if before.get(path) != expected.get(path))
    outside = sorted(set(changed) - set(task.allowed_files))
    payload = {
        "task_id": task_id,
        "suite_sha256": frozen.suite_sha256,
        "checker_sha256": frozen.checker_sha256,
        "candidate_sha256": _json_digest({path: _digest(data) for path, data in before.items()}),
        "changed_files": changed,
        "scope_passed": not outside,
        "outside_scope": outside,
        "correctness_passed": False,
        "checker_unchanged": True,
        "candidate_unchanged": True,
        "passed": False,
        "status": "scope_failure" if outside else "validation_failure",
    }
    if outside:
        return payload
    with tempfile.TemporaryDirectory(prefix="ephy-transfer-fixed-") as directory:
        fixed = Path(directory)
        checker = fixed / "checker.py"
        checker.write_bytes(dict(frozen.assets)["checker.py"])
        fixed_files = {"checker.py": checker.read_bytes()}
        for path, content in expected.items():
            if path.startswith("tests/"):
                target = fixed / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                fixed_files[path] = content
        command = [sys.executable, "-I", "-B", str(checker), str(candidate), str(fixed / "tests")]
        try:
            result = subprocess.run(
                command,
                cwd=fixed,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=task.fixture.timeout_seconds,
                check=False,
            )
            payload.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
            try:
                report = json.loads(result.stdout)
            except (ValueError, TypeError):
                report = None
            expected_report = {
                "tests_run": task.expected_tests,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "expected_failures": 0,
                "unexpected_successes": 0,
            }
            payload["correctness_passed"] = (
                result.returncode == 0
                and report == expected_report
                and isinstance(report, dict)
                and all(type(value) is int for value in report.values())
            )
        except subprocess.TimeoutExpired:
            payload.update(status="execution_timeout", exit_code=None)
        except (OSError, UnicodeError) as exc:
            payload.update(status="environment_failure", error=type(exc).__name__, exit_code=None)
        payload["checker_unchanged"] = all(
            (fixed / path).is_file()
            and not (fixed / path).is_symlink()
            and (fixed / path).read_bytes() == data
            for path, data in fixed_files.items()
        )
    frozen.verify()
    payload["candidate_unchanged"] = _candidate_files(candidate) == before
    payload["passed"] = all(
        payload[key]
        for key in ("scope_passed", "correctness_passed", "checker_unchanged", "candidate_unchanged")
    )
    if payload["passed"]:
        payload["status"] = "passed"
    elif not payload["checker_unchanged"] or not payload["candidate_unchanged"]:
        payload["status"] = "integrity_failure"
    return payload


def run_controls(frozen: FrozenTransferSuite) -> dict:
    """Use existing fixture repository construction; never invoke a model."""
    results = []
    for task in frozen.suite.tasks:
        for control in ("known_good", "unchanged", "wrong_answer", "test_weakening", "out_of_scope"):
            repository, _ = create_fixture_repository(task.fixture)
            try:
                if control in {"known_good", "test_weakening", "out_of_scope"}:
                    for change in task.fixture.mock_changes:
                        (repository / change.path).write_text(change.content, encoding="utf-8")
                if control == "wrong_answer":
                    (repository / task.allowed_files[0]).write_text(
                        "# incomplete implementation\n", encoding="utf-8"
                    )
                elif control == "test_weakening":
                    (repository / "tests/test_behavior.py").write_text(
                        "# assertions removed\n", encoding="utf-8"
                    )
                elif control == "out_of_scope":
                    (repository / "extra.py").write_text("# unrelated change\n", encoding="utf-8")
                result = validate_candidate(frozen, task.fixture.task_id, repository)
                results.append(
                    {
                        "task_id": task.fixture.task_id,
                        "split": task.split,
                        "control": control,
                        "expected_pass": control == "known_good",
                        "verdict": result,
                    }
                )
            finally:
                shutil.rmtree(repository)
    return {
        "schema_version": 1,
        "evidence_kind": "synthetic",
        "transfer_effect": "unmeasured",
        "formal_workflow": "unverified",
        "passed": all(row["expected_pass"] == row["verdict"]["passed"] for row in results),
        "controls": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["controls"])
    parser.parse_args(argv)
    result = run_controls(freeze_transfer_suite())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
