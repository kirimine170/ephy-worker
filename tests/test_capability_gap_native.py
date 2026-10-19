"""Negative controls for native report projection, without invoking P0 or a model."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

from ephy_worker.capability_gap_native import NativeGapInputError, derive_native_gap_candidates

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/capability_gap/native-adapters/cases_v1.json").read_bytes()
)
H = {char: char * 64 for char in "abcdef9876543210"}


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _base(family):
    return copy.deepcopy(FIXTURES["base_reports"][family])


def _expected(family):
    report = _base(family)
    if family == "trial_reconciliation":
        return {key: report[key] for key in (
            "plan_id", "plan_sha256", "execution_ledger_sha256", "axis", "evidence_kind")}
    if family == "planner_completion":
        return {"job_id": report["job_id"], "job_contract_sha256": H["d"],
                "contract_sha256": H["a"]}
    return {"contract_sha256": H["a"]}


def _inputs(*items):
    entries, reports = [], {}
    for i, (family, report) in enumerate(items):
        identity = f"artifact-{i}"
        raw = report if type(report) is bytes else _bytes(report)
        entries.append({
            "artifact_id": identity, "context_id": "context-1", "source_family": family,
            "source_origin": "test_fixture", "sha256": _digest(raw), "byte_length": len(raw),
            "expected": _expected(family),
        })
        reports[identity] = raw
    return {"schema": "ephy.capability-native-manifest.v1", "artifacts": entries}, reports


def _call(manifest, reports):
    raw = _bytes(manifest)
    return derive_native_gap_candidates(raw, reports, expected_manifest_sha256=_digest(raw))


def _run(family, report):
    return _call(*_inputs((family, report)))


def _diag(code="invalid_json"):
    return [{"code": code, "path": "private/path-marker", "message": "private-prose-marker"}]


def _core_status(status, code="invalid_json"):
    report = _base("plan_completion")
    report.update(status=status, passed=False, diagnostics=_diag(code))
    return report


def _planner_status(status, code="invalid_json", bound=True):
    report = _base("planner_completion")
    report.update(status=status, completed=False, source_bindings_valid=bound, diagnostics=_diag(code))
    report["checker"] = _core_status(status, code) if bound else None
    return report


def _trial_failure(**changes):
    report = _base("trial_reconciliation")
    report.update(ok=False, record_integrity_verified=False, comparison=None,
                  coverage="incomplete or mismatched evidence", **changes)
    return report


def _assert_no_performance(result):
    assert result["assurance"]["execution"] == "not_assessed"
    assert result["assurance"]["real_model_improvement"] == "not_assessed"
    assert result["assurance"]["source_bytes_rechecked"] is False
    assert not any(row["category"] in {"environment", "task_performance"}
                   for row in result["observations"])
    assert not any(key in json.dumps(result) for key in
                   ('"validation_passed"', '"wall_time"', '"capability_score"', '"ranking"'))


def test_trial_integrity_not_success():
    report = _base("trial_reconciliation")
    report["validation_unexecuted_run_ids"] = ["private-run-a"]
    result = _run("trial_reconciliation", report)
    assert result["observations"][0]["status"] == "passed"
    assert any(row["code"] == "validation_unexecuted_run_ids" for row in result["unknowns"])
    assert result["gap_candidates"] == []
    assert result["evidence"][0]["reported_commitments"]["comparison_available"] is True
    _assert_no_performance(result)
    assert "private-failure-marker" not in json.dumps(result)


def test_trial_missing_all_conditions():
    report = _trial_failure(missing_run_ids=["private-run-a", "private-run-b"], supplied_runs=0)
    result = _run("trial_reconciliation", report)
    assert result["observations"][0]["status"] == "failed"
    assert len([row for row in result["observations"] if row["code"] == "missing_run_ids"]) == 2
    assert result["evidence"][0]["reported_commitments"]["comparison_available"] is False
    assert result["evidence"][0]["reported_commitments"]["canonical_comparison_subobject_sha256"] is None
    assert result["gap_candidates"][0]["category"] == "evidence"
    _assert_no_performance(result)


def test_trial_duplicate_or_mismatch():
    mismatch = {"run_id": "private-run-a", "fields": ["private-field-marker"]}
    report = _trial_failure(
        duplicate_run_ids=["private-run-a"],
        duplicate_trials=[{"group": "private-group-marker", "task_id": "private-task-marker",
                           "repeat_index": 1, "count": 2}],
        mismatches=[mismatch, mismatch],
        evidence_mismatches=[mismatch],
        validation_unexecuted_run_ids=["private-run-a", "private-run-a"],
    )
    result = _run("trial_reconciliation", report)
    rows = result["observations"]
    assert len([row for row in rows if row["code"] == "mismatches"]) == 2
    repeated = [row for row in rows if row["code"] == "mismatches"]
    assert repeated[0]["detail_refs"] == repeated[1]["detail_refs"]
    assert repeated[0]["observation_id"] != repeated[1]["observation_id"]
    assert len([row for row in result["unknowns"]
                if row["code"] == "validation_unexecuted_run_ids"]) == 2
    assert "private-" not in json.dumps(result)


def test_trial_explicit_not_executed():
    report = _trial_failure(not_executed_run_ids=["private-run-a"])
    result = _run("trial_reconciliation", report)
    row = next(row for row in result["observations"] if row["code"] == "not_executed_run_ids")
    assert row["status"] == "unknown"
    assert result["cause_hypotheses"][0]["status"] == "unverified"
    _assert_no_performance(result)


@pytest.mark.parametrize("field", ["missing_execution_record_ids", "missing_raw_result_ids"])
def test_trial_missing_receipt_or_raw(field):
    result = _run("trial_reconciliation", _trial_failure(**{field: ["private-run-a"]}))
    assert any(row["code"] == field and row["status"] == "failed" for row in result["observations"])
    commitments = result["evidence"][0]["reported_commitments"]
    assert commitments["canonical_plan_sha256"] == H["a"]
    assert commitments["canonical_execution_ledger_sha256"] == H["b"]
    _assert_no_performance(result)


@pytest.mark.parametrize("change", [
    {"missing_run_ids": ["private-run-a"]}, {"execution_verified": True},
    {"record_integrity_verified": False}, {"supplied_runs": 1}, {"comparison": None},
])
def test_trial_contradictory_report(change):
    report = _base("trial_reconciliation")
    report.update(change)
    with pytest.raises(NativeGapInputError):
        _run("trial_reconciliation", report)


def test_plan_complete_structural_only():
    result = _run("plan_completion", _base("plan_completion"))
    assert result["observations"][0]["status"] == "passed"
    assert result["observations"][0]["category"] == "workflow"
    assert result["gap_candidates"] == []
    _assert_no_performance(result)


@pytest.mark.parametrize("code", ["invalid_json", "non_text_content", "nonterminal_response"])
def test_plan_incomplete_ack_read_truncation(code):
    result = _run("plan_completion", _core_status("incomplete", code))
    assert result["observations"][0]["diagnostic_codes"] == [code]
    assert result["gap_candidates"][0]["investigation_area"] == "planning_contract"
    assert result["gap_candidates"][0]["status"] == "unverified"
    assert result["cause_hypotheses"][0]["status"] == "unverified"
    assert "private-prose-marker" not in json.dumps(result)


def test_plan_no_hypothesis():
    result = _run("plan_completion", _core_status("no_hypothesis", "no_hypothesis"))
    assert result["observations"][0]["status"] == "unknown"
    assert result["unknowns"][0]["code"] == "no_hypothesis"
    assert result["gap_candidates"] == []


@pytest.mark.parametrize("observed", [None, H["f"]])
def test_plan_invalid_contract(observed):
    report = _core_status("invalid_contract", "contract_hash_mismatch")
    report["contract_sha256"] = observed
    result = _run("plan_completion", report)
    binding = result["evidence"][0]["bindings"]["raw_completion_contract_sha256"]
    assert binding["comparison"] == ("unavailable" if observed is None else "mismatch")
    assert binding["observed"] == observed
    assert result["gap_candidates"] == []
    assert any(row["code"] == "planning_structure_unknown" for row in result["unknowns"])


def test_plan_invalid_input():
    report = _core_status("invalid_input")
    report.update(response_sha256=None, plan_sha256=None)
    result = _run("plan_completion", report)
    assert result["gap_candidates"] == []
    assert result["evidence"][0]["reported_commitments"]["raw_response_sha256"] is None


@pytest.mark.parametrize("change", [
    {"schema": "future"}, {"status": "future"}, {"passed": 1}, {"checks_executed": True},
    {"assurance": "semantic"}, {"semantic_correctness": "passed"}, {"extra": None},
    {"diagnostics": _diag()}, {"response_sha256": "A" * 64},
])
def test_plan_unknown_or_conflicting_assurance(change):
    report = _base("plan_completion")
    report.update(change)
    with pytest.raises(NativeGapInputError):
        _run("plan_completion", report)


@pytest.mark.parametrize("kind", ["live", "mock", "synthetic"])
@pytest.mark.parametrize("origin", ["test_fixture", "saved_artifact"])
def test_cross_evidence_basis(kind, origin):
    report = _base("trial_reconciliation")
    report["evidence_kind"] = report["comparison"]["evidence_kind"] = kind
    manifest, reports = _inputs(("trial_reconciliation", report))
    manifest["artifacts"][0]["expected"]["evidence_kind"] = kind
    manifest["artifacts"][0]["source_origin"] = origin
    result = _call(manifest, reports)
    evidence = result["evidence"][0]
    assert evidence["evidence_kind"] == kind
    assert evidence["source_origin"] == origin
    assert evidence["source_family"] == "trial_reconciliation"
    assert result["evidence_basis"] == "caller_pinned_native_reports"
    assert "synthetic_observations" not in json.dumps(result)


def test_cross_raw_bytes_and_order():
    items = [("plan_completion", _core_status("incomplete")),
             ("trial_reconciliation", _trial_failure(missing_run_ids=["private-run-a"]))]
    manifest, reports = _inputs(*items)
    first = _call(manifest, reports)
    assert first == _call(copy.deepcopy(manifest), copy.deepcopy(reports))
    manifest["artifacts"].reverse()
    reversed_result = _call(manifest, reports)
    assert [row["source_family"] for row in reversed_result["evidence"]] == [
        "trial_reconciliation", "plan_completion"]
    assert reversed_result["evidence"] == first["evidence"][::-1]
    reports["artifact-0"] += b" "
    with pytest.raises(NativeGapInputError, match="report_binding_mismatch"):
        _call(manifest, reports)


@pytest.mark.parametrize("code,change", [
    ("job_id_mismatch", {"job_id": None}),
    ("job_contract_hash_mismatch", {"job_contract_sha256": H["9"]}),
    ("stop_plan_mismatch", {}),
])
def test_saved_output_binding_failure(code, change):
    report = _planner_status("invalid_source", code, bound=False)
    report.update(change)
    # The saved adapter can fail after a successful core checker.
    report["checker"] = _base("plan_completion")
    result = _run("planner_completion", report)
    assert any(row["code"] == "source_bindings" and row["status"] == "failed"
               for row in result["observations"])
    assert any(row["code"] == "planning_structure_unknown" for row in result["unknowns"])
    assert result["gap_candidates"] == []
    assert result["evidence"][0]["reported_commitments"]["nested_core"]["passed"] is True


@pytest.mark.parametrize("code", ["terminal_record_type", "terminal_role", "invalid_json"])
def test_saved_output_last_record(code):
    report = _planner_status("invalid_source", code, bound=False)
    report.update(terminal_record_sha256=None, terminal_line=None, checker_input_sha256=None,
                  original_text_sha256=None, lead_plan_representation=None)
    result = _run("planner_completion", report)
    assert any(code in row["diagnostic_codes"] for row in result["observations"])
    assert result["evidence"][0]["reported_commitments"]["raw_terminal_record_sha256"] is None
    assert result["gap_candidates"] == []


def test_saved_output_windows_encoding():
    report = _base("planner_completion")
    report["lead_plan_representation"] = "windows_write_text"
    report["source_files"]["lead-plan.txt"]["sha256"] = H["0"]
    result = _run("planner_completion", report)
    commits = result["evidence"][0]["reported_commitments"]
    assert commits["lead_plan_representation"] == "windows_write_text"
    assert commits["raw_source_files"]["lead-plan.txt"]["sha256"] == H["0"]
    assert commits["raw_original_text_sha256"] == H["c"]
    assert commits["raw_terminal_record_sha256"] == H["f"]
    assert commits["reserialized_checker_input_sha256"] == H["b"]


@pytest.mark.parametrize("code", ["file_scope_mismatch", "semantic_scope_mismatch", "checks_mismatch"])
def test_saved_output_contract_projection(code):
    result = _run("planner_completion", _planner_status("invalid_source", code, bound=False))
    assert result["gap_candidates"] == []
    assert any(code in row["diagnostic_codes"] for row in result["observations"])


@pytest.mark.parametrize("change", [
    {"checks_executed": True}, {"assurance": "execution"},
    {"workflow_validity": "passed"}, {"provenance": "verified"},
    {"formal_adoption": "accepted"}, {"source_bindings_valid": False},
])
def test_saved_output_assurance(change):
    report = _base("planner_completion")
    report.update(change)
    with pytest.raises(NativeGapInputError):
        _run("planner_completion", report)


@pytest.mark.parametrize("status,code", [
    ("incomplete", "invalid_json"), ("incomplete", "nonterminal_response"),
    ("invalid_input", "non_text_content"), ("no_hypothesis", "no_hypothesis"),
])
def test_saved_output_legacy(status, code):
    result = _run("planner_completion", _planner_status(status, code))
    assert not any(row["status"] == "passed" and row["category"] == "workflow"
                   for row in result["observations"])
    _assert_no_performance(result)


def test_twenty_vector_mapping_names_resolve():
    vectors = FIXTURES["acceptance_vectors"]
    assert len(vectors) == len({row["id"] for row in vectors}) == 20
    assert FIXTURES["source_origin"] == "test_fixture"
    assert all(callable(globals()[row["test"]]) for row in vectors)


@pytest.mark.parametrize("family", ["plan_completion", "planner_completion", "trial_reconciliation"])
def test_all_families_private_id_prose_and_inputs_unchanged(family):
    report = _base(family)
    if family == "plan_completion":
        report = _core_status("incomplete")
    manifest, reports = _inputs((family, report))
    saved_manifest, saved_reports = copy.deepcopy(manifest), copy.deepcopy(reports)
    result = _call(manifest, reports)
    assert manifest == saved_manifest and reports == saved_reports
    assert "private-" not in json.dumps(result)
    assert all(row["observation_basis"] == "reported" for row in result["observations"])


@pytest.mark.parametrize("edit", [
    "missing_report", "extra_report", "duplicate_id", "duplicate_hash", "bad_length",
    "bad_report_hash", "invalid_origin", "invalid_family", "invalid_context", "unexpected_expected",
])
def test_manifest_identity_negative_controls(edit):
    manifest, reports = _inputs(("plan_completion", _base("plan_completion")))
    entry = manifest["artifacts"][0]
    if edit == "missing_report":
        reports.clear()
    elif edit == "extra_report":
        reports["unexpected"] = b"{}"
    elif edit == "duplicate_id":
        manifest["artifacts"].append(copy.deepcopy(entry))
    elif edit == "duplicate_hash":
        duplicate = copy.deepcopy(entry)
        duplicate["artifact_id"] = "new"
        manifest["artifacts"].append(duplicate)
        reports["new"] = reports["artifact-0"]
    elif edit == "bad_length":
        entry["byte_length"] += 1
    elif edit == "bad_report_hash":
        entry["sha256"] = H["0"]
    elif edit == "invalid_origin":
        entry["source_origin"] = "trusted"
    elif edit == "invalid_family":
        entry["source_family"] = "synthetic_observations"
    elif edit == "invalid_context":
        entry["context_id"] = "../private/path-marker"
    else:
        entry["expected"]["extra"] = "private-prose-marker"
    with pytest.raises(NativeGapInputError) as error:
        _call(manifest, reports)
    assert "private" not in str(error.value) and len(error.value.args) == 1


def test_manifest_pin_external_and_raw_byte_sensitive():
    manifest, reports = _inputs(("plan_completion", _base("plan_completion")))
    raw = _bytes(manifest)
    for supplied, pin in ((raw + b" ", _digest(raw)), (raw, H["0"]), (raw, "A" * 64)):
        with pytest.raises(NativeGapInputError):
            derive_native_gap_candidates(supplied, reports, expected_manifest_sha256=pin)


@pytest.mark.parametrize("raw", [
    b'{"schema":1,"schema":2}', b'{"value":NaN}', b'{"value":Infinity}',
    b'{"value":1e999}', b"\xff", b'{"secret":"private-prose-marker"',
    b'{"value":"\\ud800"}', b"\xef\xbb\xbf{}",
])
def test_bad_json_reports_do_not_leak(raw):
    with pytest.raises(NativeGapInputError) as error:
        _call(*_inputs(("plan_completion", raw)))
    assert "private" not in str(error.value)
    assert error.value.args == (error.value.code,)


@pytest.mark.parametrize("kind", ["depth", "nodes", "string", "bytes"])
def test_resource_limits(kind):
    raw = {"depth": b"[" * 40 + b"0" + b"]" * 40,
           "nodes": _bytes([0] * 32768), "string": _bytes("x" * 8193),
           "bytes": b"x" * 1048577}[kind]
    with pytest.raises(NativeGapInputError):
        _call(*_inputs(("plan_completion", raw)))


@pytest.mark.parametrize("family,field,bad", [
    ("trial_reconciliation", "schema_version", True),
    ("trial_reconciliation", "expected_runs", True),
    ("trial_reconciliation", "supplied_runs", 1.0),
    ("trial_reconciliation", "ok", 1),
    ("planner_completion", "terminal_line", True),
    ("planner_completion", "completed", 1),
    ("plan_completion", "passed", 0),
])
def test_no_boolean_integer_coercion(family, field, bad):
    report = _base(family)
    report[field] = bad
    with pytest.raises(NativeGapInputError):
        _run(family, report)


@pytest.mark.parametrize("field", ["plan_id", "plan_sha256", "execution_ledger_sha256", "axis", "evidence_kind"])
def test_trial_caller_pin_binding(field):
    manifest, reports = _inputs(("trial_reconciliation", _base("trial_reconciliation")))
    manifest["artifacts"][0]["expected"][field] = {
        "plan_id": "other", "plan_sha256": H["0"], "execution_ledger_sha256": H["0"],
        "axis": "model", "evidence_kind": "live",
    }[field]
    with pytest.raises(NativeGapInputError, match="binding_mismatch"):
        _call(manifest, reports)


def test_cli_error_envelope_is_not_trial_report():
    with pytest.raises(NativeGapInputError):
        _run("trial_reconciliation", {"ok": False, "error": "ValueError"})


@pytest.mark.parametrize("status", ["invalid_contract", "invalid_input", "invalid_source"])
def test_legal_early_planner_failures_preserve_null_metadata(status):
    report = _planner_status(status, "invalid_json", bound=False)
    for name in ("job_id", "job_contract_sha256", "snapshot_sha256", "terminal_record_sha256",
                 "terminal_line", "checker_input_sha256", "original_text_sha256",
                 "lead_plan_representation"):
        report[name] = None
    report["source_files"]["planner-session.jsonl"] = {"sha256": None, "bytes": 16777217}
    result = _run("planner_completion", report)
    commits = result["evidence"][0]["reported_commitments"]
    assert commits["raw_source_files"]["planner-session.jsonl"] == {"sha256": None, "bytes": 16777217}
    assert result["evidence"][0]["bindings"]["job_id_sha256"]["comparison"] == "unavailable"
    assert result["gap_candidates"] == []


def test_nullable_core_hash_does_not_force_false_mismatch():
    report = _planner_status("invalid_input")
    report["checker"]["plan_sha256"] = None
    result = _run("planner_completion", report)
    commits = result["evidence"][0]["reported_commitments"]
    assert commits["nested_core"]["plan_sha256"] is None
    assert commits["raw_original_text_sha256"] == H["c"]


@pytest.mark.parametrize("key", ["response_sha256", "plan_sha256"])
def test_nested_hash_mismatch_rejected(key):
    report = _base("planner_completion")
    report["checker"][key] = H["0"]
    with pytest.raises(NativeGapInputError, match="contradictory_report"):
        _run("planner_completion", report)


def test_raw_session_and_terminal_hash_can_equal():
    report = _base("planner_completion")
    report["terminal_record_sha256"] = report["source_files"]["planner-session.jsonl"]["sha256"]
    assert _run("planner_completion", report)["gap_candidates"] == []


def test_exact_utf8_hash_disagreement_rejected():
    report = _base("planner_completion")
    report["source_files"]["lead-plan.txt"]["sha256"] = H["0"]
    with pytest.raises(NativeGapInputError, match="contradictory_report"):
        _run("planner_completion", report)


def test_conflict_across_core_and_saved_report_suppresses_candidate():
    manifest, reports = _inputs(
        ("plan_completion", _base("plan_completion")),
        ("planner_completion", _planner_status("incomplete")),
    )
    result = _call(manifest, reports)
    rows = [row for row in result["observations"] if row["code"] == "planning_contract"]
    assert {row["status"] for row in rows} == {"passed", "failed"}
    assert len({row["check_key"] for row in rows}) == 1
    assert result["gap_candidates"] == []
    conflict = next(row for row in result["unknowns"] if row["code"] == "conflicting_observations")
    assert conflict["observation_ids"] == [row["observation_id"] for row in rows]


def test_distinct_contexts_do_not_cancel_failures():
    manifest, reports = _inputs(
        ("plan_completion", _base("plan_completion")),
        ("planner_completion", _planner_status("incomplete")),
    )
    manifest["artifacts"][1]["context_id"] = "context-2"
    result = _call(manifest, reports)
    assert len(result["gap_candidates"]) == 1
    assert not any(row["code"] == "conflicting_observations" for row in result["unknowns"])


def test_conflicting_trial_reports_remain_visible():
    manifest, reports = _inputs(
        ("trial_reconciliation", _base("trial_reconciliation")),
        ("trial_reconciliation", _trial_failure(missing_run_ids=["private-run-a"])),
    )
    result = _call(manifest, reports)
    assert result["gap_candidates"] == []
    assert any(row["code"] == "conflicting_observations" for row in result["unknowns"])


def test_multiple_failure_reports_keep_all_evidence_ids():
    a, b = _core_status("incomplete"), _core_status("incomplete", "acceptance_coverage")
    result = _call(*_inputs(("plan_completion", a), ("plan_completion", b)))
    assert len(result["gap_candidates"]) == 1
    assert len(result["gap_candidates"][0]["supporting_observation_ids"]) == 2
    assert len(result["gap_candidates"][0]["evidence_ids"]) == 2


def test_projection_has_no_io_or_upstream_imports(monkeypatch):
    import ephy_worker.capability_gap_native as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {name.name for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names}
    assert imported <= {"__future__", "typing", "hashlib", "json", "math", "re"}
    manifest, reports = _inputs(("planner_completion", _base("planner_completion")))

    def forbidden(*_args, **_kwargs):
        pytest.fail("unexpected external operation")

    monkeypatch.setattr("builtins.open", forbidden)
    assert _call(manifest, reports)["evidence_basis"] == "caller_pinned_native_reports"

@pytest.mark.parametrize("case", FIXTURES["saved_p0_reports"], ids=lambda case: case["id"])
def test_saved_p0_synthetic_reports(case):
    raw = case["raw_report_utf8"].encode("utf-8")
    assert len(raw) == case["raw_byte_length"]
    assert _digest(raw) == case["raw_sha256"]
    assert case["source_origin"] == "test_fixture"
    manifest, reports = _inputs((case["source_family"], raw))
    manifest["artifacts"][0]["expected"] = case["expected"]
    result = _call(manifest, reports)
    assert len(result["gap_candidates"]) == case["expected_candidate_count"]
    assert result["evidence"][0]["raw_report_sha256"] == case["raw_sha256"]
    _assert_no_performance(result)
    if case["id"] == "planner-27-saved-plan-byte-mismatch":
        assert result["evidence"][0]["reported_commitments"]["nested_core"]["passed"] is True
        assert any(row["code"] == "planning_structure_unknown" for row in result["unknowns"])
