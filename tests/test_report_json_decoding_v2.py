"""Control-set tests for the frozen report JSON decoding evaluator."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.check_report_json_decoding_v2 import EvaluationError, ReportReadNormalizer

BASE = '(root / "report.json").read_text()'


def normalize(expression: str, *, candidate: bool = True) -> str:
    tree = ast.parse(expression, mode="eval")
    normalizer = ReportReadNormalizer(candidate=candidate)
    tree = normalizer.visit(tree)
    ast.fix_missing_locations(tree)
    assert normalizer.count == 1
    return ast.dump(tree, include_attributes=False)


@pytest.mark.parametrize(
    "expression",
    [
        '(root / "report.json").read_text(encoding="utf-8")',
        '(root / "report.json").read_text("utf-8")',
        '(root / "report.json").read_text(encoding="utf-8", errors="strict")',
        '(root / "report.json").read_bytes().decode("utf-8")',
        '(root / "report.json").read_bytes().decode(encoding="utf-8", errors="strict")',
    ],
)
def test_strict_positive_controls_normalize_to_baseline(expression: str) -> None:
    assert normalize(expression) == normalize(BASE, candidate=False)


@pytest.mark.parametrize(
    "expression",
    [
        '(root / "report.json").read_text()',
        '(root / "report.json").read_text(encoding="cp932")',
        '(root / "report.json").read_text("utf-8", "ignore")',
        '(root / "report.json").read_text(encoding="utf-8", errors="replace")',
        '(root / "report.json").read_text(encoding="utf-8", errors=mode)',
        '(root / "report.json").read_bytes().decode("utf-8", errors="ignore")',
        '(root / "report.json").read_bytes().decode(codec)',
    ],
)
def test_lossy_or_implicit_negative_controls_are_rejected(expression: str) -> None:
    with pytest.raises(EvaluationError):
        normalize(expression)


def test_unrelated_read_is_not_normalized() -> None:
    tree = ast.parse('(root / "config.yaml").read_text(encoding="utf-8")', mode="eval")
    normalizer = ReportReadNormalizer(candidate=True)
    normalized = normalizer.visit(tree)
    assert normalizer.count == 0
    assert "config.yaml" in ast.dump(normalized)


def test_checker_and_plugin_are_external_files() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts" / "check_report_json_decoding_v2.py").is_file()
    assert (root / "scripts" / "report_json_decoding_probe.py").is_file()
