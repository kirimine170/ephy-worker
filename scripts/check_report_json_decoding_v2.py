#!/usr/bin/env python3
"""Strict, candidate-path evaluation for the Windows report JSON task."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFINITION_ID = "report-json-decoding-v2"
TARGETS = {
    Path("tests/test_research.py"): "test_complete_report_provenance_and_fresh_job",
    Path("tests/test_tavily.py"): "test_full_workflow_uses_tavily_candidates_not_snippet_evidence",
}
TARGET_NODE_IDS = (
    "tests/test_research.py::test_complete_report_provenance_and_fresh_job",
    "tests/test_tavily.py::test_full_workflow_uses_tavily_candidates_not_snippet_evidence",
)
MARKER_NAME = "__EPHY_STRICT_REPORT_JSON_READ__"


class EvaluationError(ValueError):
    """A candidate violates the frozen evaluation definition."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _literal(value: ast.expr | None) -> object:
    return value.value if isinstance(value, ast.Constant) else object()


def _contains_report_json(expression: ast.AST) -> bool:
    return any(isinstance(node, ast.Constant) and node.value == "report.json" for node in ast.walk(expression))


def _argument(call: ast.Call, position: int, name: str) -> ast.expr | None:
    positional = call.args[position] if position < len(call.args) else None
    keywords = [item.value for item in call.keywords if item.arg == name]
    if positional is not None and keywords:
        raise EvaluationError(f"duplicate {name} argument")
    if len(keywords) > 1:
        raise EvaluationError(f"duplicate {name} argument")
    return positional if positional is not None else (keywords[0] if keywords else None)


def _validate_strict_text_options(call: ast.Call, *, decode: bool) -> None:
    if any(item.arg is None for item in call.keywords):
        raise EvaluationError("dynamic keyword arguments are not allowed on the report JSON read")
    if len(call.args) > 2:
        raise EvaluationError("unexpected positional arguments on the report JSON read")
    encoding = _argument(call, 0, "encoding")
    errors = _argument(call, 1, "errors")
    if _literal(encoding) != "utf-8":
        operation = "decode" if decode else "read_text"
        raise EvaluationError(f"{operation} must select literal UTF-8")
    if errors is not None and _literal(errors) != "strict":
        raise EvaluationError("errors must be omitted or literal strict")


def _marker() -> ast.Call:
    return ast.Call(func=ast.Name(id=MARKER_NAME, ctx=ast.Load()), args=[], keywords=[])


def _is_marker(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == MARKER_NAME


class ReportReadNormalizer(ast.NodeTransformer):
    def __init__(self, *, candidate: bool) -> None:
        self.candidate = candidate
        self.count = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node = self.generic_visit(node)
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return node
        if node.func.attr == "read_text" and _contains_report_json(node.func.value):
            if self.candidate:
                _validate_strict_text_options(node, decode=False)
            self.count += 1
            return ast.copy_location(_marker(), node)
        if node.func.attr != "decode" or not isinstance(node.func.value, ast.Call):
            return node
        byte_call = node.func.value
        if not isinstance(byte_call.func, ast.Attribute) or byte_call.func.attr != "read_bytes":
            return node
        if not _contains_report_json(byte_call.func.value):
            return node
        if byte_call.args or byte_call.keywords:
            raise EvaluationError("read_bytes for report.json must not receive arguments")
        if self.candidate:
            _validate_strict_text_options(node, decode=True)
        self.count += 1
        return ast.copy_location(_marker(), node)


class ReplaceName(ast.NodeTransformer):
    def __init__(self, name: str) -> None:
        self.name = name

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            return ast.copy_location(_marker(), node)
        return node


def _inline_single_use_alias(function: ast.AsyncFunctionDef) -> None:
    aliases: list[tuple[int, str]] = []
    for index, statement in enumerate(function.body):
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and _is_marker(statement.value)
        ):
            aliases.append((index, statement.targets[0].id))
    for index, name in reversed(aliases):
        loads = [
            node
            for statement in function.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
        ]
        if len(loads) != 1:
            raise EvaluationError("report JSON alias must be a simple single-use value")
        del function.body[index]
        function = ReplaceName(name).visit(function)


def _target(module: ast.Module, name: str, path: Path) -> ast.AsyncFunctionDef:
    matches = [
        node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise EvaluationError(f"expected exactly one async {name} in {path.as_posix()}")
    return matches[0]


def canonical_target(path: Path, name: str, *, candidate: bool) -> str:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = copy.deepcopy(_target(module, name, path))
    normalizer = ReportReadNormalizer(candidate=candidate)
    function = normalizer.visit(function)
    if normalizer.count != 1:
        raise EvaluationError(f"expected one report.json read in {name}, found {normalizer.count}")
    _inline_single_use_alias(function)
    ast.fix_missing_locations(function)
    return ast.dump(function, include_attributes=False)


def canonical_module_without_target(path: Path, name: str) -> str:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target = _target(module, name, path)
    module.body = [node for node in module.body if node is not target]
    return ast.dump(module, include_attributes=False)


def changed_files(candidate: Path) -> list[str]:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(candidate), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if result.returncode:
        raise EvaluationError(f"git status failed: {result.stderr.strip()}")
    names: list[str] = []
    for line in result.stdout.splitlines():
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        names.append(value.replace("\\", "/"))
    return sorted(set(names))


def validate_static(baseline: Path, candidate: Path) -> dict[str, object]:
    observed = changed_files(candidate)
    allowed = sorted(path.as_posix() for path in TARGETS)
    outside = sorted(set(observed) - set(allowed))
    if outside:
        raise EvaluationError(f"out-of-scope files: {', '.join(outside)}")
    if observed != allowed:
        raise EvaluationError(f"both target files must change; observed: {observed}")
    files: dict[str, object] = {}
    for relative, target_name in TARGETS.items():
        baseline_path = baseline / relative
        candidate_path = candidate / relative
        if not baseline_path.is_file() or not candidate_path.is_file():
            raise EvaluationError(f"required file is missing: {relative.as_posix()}")
        if canonical_module_without_target(baseline_path, target_name) != canonical_module_without_target(
            candidate_path, target_name
        ):
            raise EvaluationError(f"unrelated module content changed: {relative.as_posix()}")
        if canonical_target(baseline_path, target_name, candidate=False) != canonical_target(
            candidate_path, target_name, candidate=True
        ):
            raise EvaluationError(f"target semantics changed beyond strict decoding: {relative.as_posix()}")
        files[relative.as_posix()] = {
            "baseline_sha256": sha256(baseline_path),
            "candidate_sha256": sha256(candidate_path),
        }
    return {"allowed_files": allowed, "observed_files": observed, "files": files}


def run_probe(
    candidate: Path,
    python: Path,
    plugin: Path,
    mode: str,
    basetemp: Path,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment.pop("PYTHONUTF8", None)
    environment.pop("PYTHONIOENCODING", None)
    python_paths = [str(plugin.parent), str(candidate / "src"), str(candidate / "tests")]
    existing = environment.get("PYTHONPATH")
    if existing:
        python_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    environment["EPHY_REPORT_JSON_PROBE_MODE"] = mode
    command = [
        str(python),
        "-m",
        "pytest",
        "-q",
        *TARGET_NODE_IDS,
        "-p",
        "report_json_decoding_probe",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        "--basetemp",
        str(basetemp),
    ]
    result = subprocess.run(
        command,
        cwd=candidate,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = result.stdout + "\n" + result.stderr
    if mode == "valid-utf8":
        passed = result.returncode == 0 and "3 passed" in output
    elif mode == "invalid-utf8":
        passed = result.returncode != 0 and "3 failed" in output and "UnicodeDecodeError" in output
    else:
        passed = (
            result.returncode != 0
            and "3 failed" in output
            and "AssertionError" in output
            and "UnicodeDecodeError" not in output
        )
    evidence = {
        "mode": mode,
        "argv": command,
        "cwd": str(candidate),
        "exit_code": result.returncode,
        "passed": passed,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if not passed:
        raise EvaluationError(f"dynamic probe failed: {mode}; exit={result.returncode}\n{output[-4000:]}")
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--plugin", type=Path, default=Path(__file__).with_name("report_json_decoding_probe.py"))
    parser.add_argument("--basetemp", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline = args.baseline.resolve()
    candidate = args.candidate.resolve()
    plugin = args.plugin.resolve()
    evidence: dict[str, object] = {
        "definition_id": DEFINITION_ID,
        "checker_sha256": sha256(Path(__file__)),
        "plugin_sha256": sha256(plugin),
        "baseline": str(baseline),
        "candidate": str(candidate),
        "static": None,
        "probes": [],
        "passed": False,
    }
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        evidence["static"] = validate_static(baseline, candidate)
        if args.basetemp:
            temp_root = args.basetemp.resolve()
            temp_root.mkdir(parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="ephy-report-json-v2-")
            temp_root = Path(temporary.name)
        for index, mode in enumerate(("valid-utf8", "invalid-utf8", "semantic-negative"), start=1):
            evidence["probes"].append(run_probe(candidate, args.python, plugin, mode, temp_root / str(index)))
        evidence["passed"] = True
    except (EvaluationError, OSError, UnicodeError) as error:
        evidence["error"] = str(error)
    finally:
        if temporary is not None:
            temporary.cleanup()
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
