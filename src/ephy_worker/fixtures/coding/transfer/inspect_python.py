"""Read-only source inspection tool: one JSON request on stdin, no file access."""

from __future__ import annotations

import ast
import json
import sys


def inspect_source(source: str) -> dict:
    if not isinstance(source, str) or len(source.encode("utf-8")) > 65536:
        raise ValueError("source must be a UTF-8 string of at most 65536 bytes")
    tree = ast.parse(source)
    return {
        "functions": [
            {
                "name": node.name,
                "line": node.lineno,
                "arguments": [item.arg for item in node.args.posonlyargs + node.args.args],
            }
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ],
        "branches": [
            {"line": node.lineno, "condition": ast.unparse(node.test)}
            for node in ast.walk(tree)
            if isinstance(node, (ast.If, ast.IfExp, ast.While))
        ],
        "comparisons": [
            {"line": node.lineno, "expression": ast.unparse(node)}
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
        ],
    }


def main() -> int:
    request = json.loads(sys.stdin.read(131073))
    if not isinstance(request, dict) or set(request) != {"source"}:
        raise ValueError("request must contain only source")
    print(json.dumps(inspect_source(request["source"]), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
