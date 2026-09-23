#!/usr/bin/env python3
"""Fixed encoding-task check; run from outside the candidate worktree.

Evaluation definition: windows-cli-encoding-v3. Accepted candidate changes are
an explicit UTF-8 subprocess decoding choice, or binary capture followed
immediately by strict UTF-8 decoding of the complete stdout bytes.
The baseline test's probes and assertions must otherwise remain intact.
"""

from __future__ import annotations

import ast
import copy
import subprocess
import sys
import tempfile
from pathlib import Path

DEFINITION_VERSION = "windows-cli-encoding-v3"
TEST = Path("tests/test_cli.py")
TARGET = "test_cli_help_and_missing_profile"


def canonical(path: Path, *, candidate: bool) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == TARGET]
    if len(functions) != 1:
        raise ValueError(f"expected exactly one {TARGET}")
    if candidate:
        replacements: dict[str, ast.expr] = {}
        body: list[ast.stmt] = []
        index = 0
        while index < len(functions[0].body):
            statement = functions[0].body[index]
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and isinstance(statement.value.func.value, ast.Name)
                and statement.value.func.value.id == "subprocess"
                and statement.value.func.attr == "run"
            ):
                binary = any(
                    keyword.arg == "text" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                    for keyword in statement.value.keywords
                )
                if binary:
                    if index + 1 >= len(functions[0].body):
                        raise ValueError("binary capture lacks immediate strict UTF-8 decode")
                    decode = functions[0].body[index + 1]
                    if not (
                        isinstance(decode, ast.Assign)
                        and len(decode.targets) == 1
                        and isinstance(decode.targets[0], ast.Name)
                        and isinstance(decode.value, ast.Call)
                        and isinstance(decode.value.func, ast.Attribute)
                        and decode.value.func.attr == "decode"
                        and isinstance(decode.value.func.value, ast.Attribute)
                        and isinstance(decode.value.func.value.value, ast.Name)
                        and decode.value.func.value.value.id == statement.targets[0].id
                        and decode.value.func.value.attr == "stdout"
                        and len(decode.value.args) == 1
                        and isinstance(decode.value.args[0], ast.Constant)
                        and decode.value.args[0].value == "utf-8"
                        and not decode.value.keywords
                    ):
                        raise ValueError("binary capture lacks immediate strict UTF-8 decode")
                    replacements[decode.targets[0].id] = copy.deepcopy(decode.value.func.value)
                    for keyword in statement.value.keywords:
                        if keyword.arg == "text":
                            keyword.value = ast.Constant(value=True)
                    index += 1
            body.append(statement)
            index += 1
        functions[0].body = body

        class ReplaceDecodedNames(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name) -> ast.expr:
                if isinstance(node.ctx, ast.Load) and node.id in replacements:
                    return copy.deepcopy(replacements[node.id])
                return node

        ReplaceDecodedNames().visit(functions[0])
        for node in ast.walk(functions[0]):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and node.func.attr == "run"):
                continue
            remaining = []
            has_text = any(keyword.arg == "text" for keyword in node.keywords)
            for keyword in node.keywords:
                if keyword.arg == "errors":
                    raise ValueError("errors= can hide a decode failure or drop content")
                if keyword.arg == "encoding":
                    if not (isinstance(keyword.value, ast.Constant) and keyword.value.value == "utf-8"):
                        raise ValueError("encoding must be literal UTF-8")
                    if not has_text:
                        remaining.append(ast.keyword(arg="text", value=ast.Constant(value=True)))
                    continue
                remaining.append(keyword)
            node.keywords = remaining
    return ast.dump(tree, include_attributes=False)


def check(baseline: Path, candidate: Path) -> None:
    before = canonical(baseline / TEST, candidate=False)
    after = canonical(candidate / TEST, candidate=True)
    if before != after:
        raise ValueError("candidate changed probes, assertions, skip state, or unrelated test behavior")

    help_result = subprocess.run(
        [sys.executable, "-m", "ephy_worker", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=10, check=False,
    )
    if help_result.returncode != 0 or "research" not in help_result.stdout or "doctor" not in help_result.stdout:
        raise ValueError("CLI help probe failed or lost expected content")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "設定 空白.yaml"
        config.write_text(
            "search:\n  base_url: http://127.0.0.1:9\n  engine: fixture\n"
            "model_profiles:\n  test:\n    family: qwen\n"
            "    base_url: http://127.0.0.1:9/v1\n    model_id: fixture\n    output_mode: tool\n",
            encoding="utf-8",
        )
        output = root / "out"
        result = subprocess.run(
            [sys.executable, "-m", "ephy_worker", "research", "--config", str(config),
             "--profile", "missing", "--question", "public test", "--output-dir", str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=10, check=False,
        )
        if result.returncode != 1 or output.exists():
            raise ValueError("missing-profile probe did not reject without output")


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} BASELINE_ROOT CANDIDATE_ROOT", file=sys.stderr)
        return 2
    try:
        check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    except (ValueError, OSError, UnicodeError) as error:
        print(f"{DEFINITION_VERSION}: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"{DEFINITION_VERSION}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
