"""Offline adversarial controls for saved planner-only source bindings."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker import plan_completion_adapter as adapter
from ephy_worker.formal_artifacts import encode as runtime_encode


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def case():
    checks = [{"id": name, "argv": ["python", "-m", "pytest", "-q"]}
              for name in ("target", "regression", "lint", "repository", "fixed")]
    completion = {
        "schema": "ephy.plan-completion-contract.v1",
        "requirements": [{"id": "R1", "text": "欠落を拒否"}],
        "allowed_files": ["docs/checklist.md", "docs/other.md"],
        "semantic_scope": "計画確認の文書のみ",
        "prohibitions": [{"id": "P1", "text": "実装・採用しない"}],
        "checks": checks,
    }
    runtime = {
        "planner_only": True, "max_repairs": 0,
        "allowed_files": completion["allowed_files"].copy(),
        "semantic_scope": completion["semantic_scope"],
        "checks": [{**value, "baseline_exit_code": 0} for value in copy.deepcopy(checks)],
        "task": "Original prose is not an automatically extracted requirement.",
        "checker": "../never-read-checker", "other_pinned_value": {"key": True},
    }
    pins = {
        "expected_contract_sha256": digest(encode(completion)),
        "expected_job_id": "saved-planner-fixture",
        "expected_job_contract_sha256": digest(runtime_encode(runtime)),
    }
    plan = {
        "schema": "ephy.plan.v1", "contract_sha256": pins["expected_contract_sha256"],
        "hypothesis": "構造検査で欠落を検出",
        "scope": {"files": ["docs/checklist.md"], "semantic_scope": completion["semantic_scope"]},
        "prohibitions": ["P1"],
        "acceptance": [{"id": "A1", "requirement_ids": ["R1"], "criterion": "欠落を拒否"}],
        "verification": [{"check_id": value["id"], "acceptance_ids": ["A1"]} for value in checks],
    }
    text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    message = {
        "role": "assistant", "stopReason": "stop", "model": "synthetic", "provider": "offline",
        "usage": {"cost": {"total": 0.125}}, "timestamp": 123,
        "content": [{"type": "thinking", "thinking": "synthetic reasoning", "signature": "sig"},
                    {"type": "text", "text": text[:15], "textSignature": "one"},
                    {"type": "text", "text": text[15:], "textSignature": "two"}],
    }
    records = [
        {"type": "session", "id": "pi-session-identity-is-not-job-identity"},
        {"type": "model_change", "modelId": "synthetic"},
        {"type": "thinking_level_change", "thinkingLevel": "medium"},
        {"type": "message", "message": {"role": "user", "content": "synthetic task"}},
        {"type": "message", "id": "terminal", "parentId": None, "message": message},
    ]
    job = {
        "schemaVersion": 2, "id": pins["expected_job_id"], "status": "planner_stopped",
        "outcome": "planned_only", "contract": runtime,
        "jobDir": "../never-follow-jobdir", "worktreePath": "../never-follow-worktree",
        "runtime": {"resource_lock": "../never-follow-lock"},
    }
    stop = {
        "job_id": job["id"], "contract_sha256": pins["expected_job_contract_sha256"],
        "plan_sha256": digest(text.encode()), "snapshot_sha256": "a" * 64,
        "implementation_started": False, "formal_audit_executed": False, "automatic_adoption": False,
    }
    return SimpleNamespace(completion=completion, runtime=runtime, plan=plan, text=text, message=message,
                           records=records, job=job, stop=stop, pins=pins, lead=text.encode())


def inputs(case):
    return {
        "contract_bytes": encode(case.completion),
        "job_bytes": encode(case.job),
        "session_bytes": b"\n".join(encode(record) for record in case.records) + b"\n",
        "plan_bytes": case.lead,
        "stop_bytes": encode(case.stop),
    }


def evaluate(case, **overrides):
    return adapter.check_planner_only_output(**{**inputs(case), **overrides}, **case.pins)


def repin_runtime(case):
    case.pins["expected_job_contract_sha256"] = digest(runtime_encode(case.runtime))
    case.stop["contract_sha256"] = case.pins["expected_job_contract_sha256"]


def set_text(case, text):
    case.text = text
    case.message["content"] = [{"type": "text", "text": text}]
    case.stop["plan_sha256"] = digest(text.encode())
    case.lead = text.encode()
    case.job["outcome"] = "no_hypothesis" if text.strip() == "NO_HYPOTHESIS" else "planned_only"


def reject(result, code=None, status=None):
    assert result["completed"] is False
    assert result["checks_executed"] is False
    assert result["assurance"] == "structural_only"
    for field in ("semantic_correctness", "workflow_validity", "provenance", "formal_adoption"):
        assert result[field] == "not_assessed"
    assert result["diagnostics"]
    if code is not None:
        assert result["diagnostics"][0]["code"] == code
    if status is not None:
        assert result["status"] == status


def test_concise_unicode_structured_plan_completes_with_separate_hashes(case):
    result = evaluate(case)
    assert result["schema"] == "ephy.planner-completion-evidence.v1"
    assert result["completed"] is True and result["source_bindings_valid"] is True
    assert result["checker"]["passed"] is True
    assert result["status"] == "complete"
    assert result["terminal_line"] == 5
    assert result["terminal_record_sha256"] == digest(encode(case.records[-1]))
    checker_input = json.dumps(case.message, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert result["checker_input_sha256"] == digest(checker_input) == result["checker"]["response_sha256"]
    assert result["original_text_sha256"] == digest(case.text.encode()) == result["checker"]["plan_sha256"]
    assert result["lead_plan_representation"] == "exact_utf8"
    assert result["snapshot_sha256"] == "a" * 64
    raw = inputs(case)
    for name, argument in zip(adapter.SOURCE_LIMITS, raw, strict=True):
        assert result["source_files"][name] == {"sha256": digest(raw[argument]), "bytes": len(raw[argument])}
    assert len({result["source_files"]["planner-session.jsonl"]["sha256"],
                result["terminal_record_sha256"], result["checker_input_sha256"]}) == 3
    assert result == evaluate(case)


def test_canonical_encoding_matches_current_runtime_and_raw_job_hash_is_separate(case):
    assert adapter._canonical_contract(case.runtime) == runtime_encode(case.runtime)
    raw = inputs(case)
    pretty = runtime_encode(case.job)
    result = evaluate(case, job_bytes=pretty)
    assert result["completed"] is True
    assert result["job_contract_sha256"] == digest(runtime_encode(case.runtime))
    assert result["source_files"]["job.json"]["sha256"] == digest(pretty)
    assert digest(pretty) != digest(raw["job_bytes"])


def test_complete_final_json_without_newline_and_blank_physical_lines(case):
    raw = inputs(case)["session_bytes"].rstrip(b"\n")
    assert evaluate(case, session_bytes=raw)["completed"] is True
    prefixed = b"\n \t\r\n" + raw + b"\n\n \t\r\n"
    result = evaluate(case, session_bytes=prefixed)
    assert result["completed"] is True
    assert result["terminal_line"] == 7
    assert result["terminal_record_sha256"] == digest(encode(case.records[-1]))


def test_crlf_jsonl_record_hash_includes_cr_but_not_lf_separator(case):
    raw = b"\r\n".join(encode(record) for record in case.records) + b"\r\n"
    result = evaluate(case, session_bytes=raw)
    assert result["completed"] is True
    assert result["terminal_record_sha256"] == digest(encode(case.records[-1]) + b"\r")


@pytest.mark.parametrize("tail", [
    {"type": "message", "message": {"role": "toolResult", "content": []}},
    {"type": "message", "message": {"role": "user", "content": []}},
    {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
    {"type": "session"}, {"type": "custom", "value": "done"}, {},
    {"type": "message", "message": None}, [], True, "message",
])
def test_last_record_itself_must_qualify_no_reverse_fallback(case, tail):
    raw = inputs(case)["session_bytes"] + encode(tail) + b"\n"
    reject(evaluate(case, session_bytes=raw), status="invalid_source")


@pytest.mark.parametrize("raw,code", [
    (b'{"x":1,"x":2}\n', "duplicate_key"),
    (b'{"metadata":{"x":1,"x":2}}\n', "duplicate_key"),
    (b'{"x":NaN}\n', "nonfinite_number"),
    (b'{"x":1e999}\n', "nonfinite_number"),
    (b"\xff\n", "invalid_utf8"), (b"{\n", "invalid_json"),
    (b"[]\n", "expected_object"), (b"null\n", "expected_object"),
])
@pytest.mark.parametrize("position", ["prefix", "tail"])
def test_all_nonblank_records_are_strictly_parsed(case, raw, code, position):
    valid = inputs(case)["session_bytes"]
    invalid = raw + valid if position == "prefix" else valid + raw
    reject(evaluate(case, session_bytes=invalid), code, "invalid_source")


@pytest.mark.parametrize("raw", [b"", b"\n \t\r\n"])
def test_empty_sessions_rejected(case, raw):
    reject(evaluate(case, session_bytes=raw), "missing_terminal_record", "invalid_source")


@pytest.mark.parametrize("stop", ["length", "max_tokens", "toolUse", "error", None, True])
def test_nonterminal_original_message_is_not_repaired(case, stop):
    case.message["stopReason"] = stop
    result = evaluate(case)
    reject(result, "nonterminal_response", "incomplete")
    assert result["source_bindings_valid"] is True


@pytest.mark.parametrize("block", [
    {"type": "toolCall", "name": "read", "arguments": {"path": "README.md"}},
    {"type": "image", "data": "image"},
    {"type": "thinking", "thinking": 1},
])
def test_every_original_content_block_reaches_core(case, block):
    case.message["content"].append(block)
    reject(evaluate(case))
    assert evaluate(case)["checker"]["passed"] is False


def test_whole_message_including_ignored_fields_preserved(case, monkeypatch):
    case.message["foreign_tool_calls"] = [{"data": "not a Pi content block"}]
    seen = []
    original = adapter.core.check_plan_completion

    def observe(contract, raw, **kwargs):
        seen.append(raw)
        return original(contract, raw, **kwargs)

    monkeypatch.setattr(adapter.core, "check_plan_completion", observe)
    result = evaluate(case)
    assert result["completed"] is True
    assert len(seen) == 1 and json.loads(seen[0]) == case.message
    assert result["checker_input_sha256"] == digest(seen[0])
    assert "synthetic reasoning" not in json.dumps(result)


@pytest.mark.parametrize("text", ["ACK", "README を読みました", "Read the frozen checklist and stop.", "{}"])
def test_legacy_prose_ack_and_incomplete_plans_never_complete(case, text):
    set_text(case, text)
    result = evaluate(case)
    reject(result)
    assert result["source_bindings_valid"] is True


def test_read_tool_only_and_thinking_only_never_complete(case):
    for block in ({"type": "toolCall", "name": "read", "arguments": {}},
                  {"type": "thinking", "thinking": case.text}):
        case.message["content"] = [block]
        reject(evaluate(case), "missing_plan_text", "invalid_source")


@pytest.mark.parametrize("text", ["NO_HYPOTHESIS", "\nNO_HYPOTHESIS\t"])
def test_no_hypothesis_remains_non_success_with_source_bindings(case, text):
    set_text(case, text)
    result = evaluate(case)
    reject(result, "no_hypothesis", "no_hypothesis")
    assert result["source_bindings_valid"] is True
    assert result["original_text_sha256"] == digest(text.encode())


@pytest.mark.parametrize("outcome", ["no_hypothesis", "completed", None, True])
def test_planned_only_outcome_must_match_exact_text_rule(case, outcome):
    case.job["outcome"] = outcome
    reject(evaluate(case), "job_outcome", "invalid_source")


def test_no_hypothesis_outcome_cannot_be_relabelled(case):
    set_text(case, " NO_HYPOTHESIS\n")
    case.job["outcome"] = "planned_only"
    reject(evaluate(case), "job_outcome", "invalid_source")


@pytest.mark.parametrize("value", [False, 1, "true", None, [], {}])
def test_planner_only_requires_true_boolean(case, value):
    case.runtime["planner_only"] = value
    repin_runtime(case)
    reject(evaluate(case), "not_planner_only", "invalid_source")


@pytest.mark.parametrize("value", [False, True, 0.0, "0", 1, None, []])
def test_zero_repairs_requires_exact_integer(case, value):
    case.runtime["max_repairs"] = value
    repin_runtime(case)
    reject(evaluate(case), "repairs_authorized", "invalid_source")


@pytest.mark.parametrize("value", ["running", "review_ready", None, True])
def test_saved_planner_stopped_status_required(case, value):
    case.job["status"] = value
    reject(evaluate(case), "job_status", "invalid_source")


@pytest.mark.parametrize("field", ["implementation_started", "formal_audit_executed", "automatic_adoption"])
@pytest.mark.parametrize("value", [0, 0.0, True, "false", None, []])
def test_stop_control_flags_require_false_boolean(case, field, value):
    case.stop[field] = value
    reject(evaluate(case), "stop_flag", "invalid_source")


@pytest.mark.parametrize("field", sorted(adapter.STOP_FIELDS))
def test_every_stop_field_required(case, field):
    del case.stop[field]
    reject(evaluate(case), "missing_field", "invalid_source")


def test_unknown_stop_fields_and_invalid_snapshot_hash_rejected(case):
    case.stop["approved"] = True
    reject(evaluate(case), "unknown_field", "invalid_source")
    del case.stop["approved"]
    case.stop["snapshot_sha256"] = "unknown"
    reject(evaluate(case), "invalid_hash_pin", "invalid_source")


@pytest.mark.parametrize("field", ["expected_contract_sha256", "expected_job_contract_sha256"])
@pytest.mark.parametrize("value", ["", "A" * 64, "0" * 63, None, True])
def test_required_external_hash_pin_types(case, field, value):
    case.pins[field] = value
    reject(evaluate(case), "invalid_hash_pin")


@pytest.mark.parametrize("value", ["", " x", "x\ny", None, True])
def test_expected_job_id_is_nonblank_exact_string(case, value):
    case.pins["expected_job_id"] = value
    reject(evaluate(case), status="invalid_input")


@pytest.mark.parametrize("change,code", [
    ("expected_job", "job_id_mismatch"), ("job_id", "job_id_mismatch"),
    ("stop_job", "stop_job_mismatch"), ("runtime", "job_contract_hash_mismatch"),
    ("stop_contract", "stop_contract_mismatch"), ("completion_bytes", "contract_hash_mismatch"),
])
def test_independent_caller_source_bindings(case, change, code):
    overrides = {}
    if change == "expected_job":
        case.pins["expected_job_id"] = "another-job"
    elif change == "job_id":
        case.job["id"] = case.stop["job_id"] = "another-job"
    elif change == "stop_job":
        case.stop["job_id"] = "another-job"
    elif change == "runtime":
        case.runtime["other_pinned_value"]["key"] = False
    elif change == "stop_contract":
        case.stop["contract_sha256"] = "0" * 64
    else:
        overrides["contract_bytes"] = encode(case.completion) + b" "
    reject(evaluate(case, **overrides), code)


@pytest.mark.parametrize("change,code", [
    ("add_file", "file_scope_mismatch"), ("remove_file", "file_scope_mismatch"),
    ("semantic", "semantic_scope_mismatch"), ("add_check", "checks_mismatch"),
    ("remove_check", "checks_mismatch"), ("argv", "checks_mismatch"),
    ("argv_tokens", "checks_mismatch"), ("duplicate_check", "duplicate_id"),
])
def test_runtime_projection_must_equal_completion_contract(case, change, code):
    if change == "add_file":
        case.runtime["allowed_files"].append("docs/new.md")
    elif change == "remove_file":
        case.runtime["allowed_files"].pop()
    elif change == "semantic":
        case.runtime["semantic_scope"] += " "
    elif change == "add_check":
        case.runtime["checks"].append({"id": "another", "argv": ["check"]})
    elif change == "remove_check":
        case.runtime["checks"].pop()
    elif change == "argv":
        case.runtime["checks"][0]["argv"].reverse()
    elif change == "argv_tokens":
        case.runtime["checks"][0]["argv"] = ["python -m pytest -q"]
    else:
        case.runtime["checks"].append(copy.deepcopy(case.runtime["checks"][0]))
    repin_runtime(case)
    reject(evaluate(case), code, "invalid_source")


def test_projection_order_ignores_file_and_check_order_but_not_argv_order(case):
    case.runtime["allowed_files"].reverse()
    case.runtime["checks"].reverse()
    repin_runtime(case)
    assert evaluate(case)["completed"] is True


def test_requirements_are_not_inferred_from_runtime_task_prose(case):
    case.runtime["task"] = "This says nothing about R1, and is not parsed by the adapter."
    repin_runtime(case)
    result = evaluate(case)
    assert result["completed"] is True
    assert result["semantic_correctness"] == "not_assessed"


@pytest.mark.parametrize("text", ["one\ntwo\n", "one\r\ntwo\n", "one\rtwo\n"])
def test_exact_windows_write_text_representation_preserves_original_hash(case, text):
    set_text(case, text)
    case.lead = text.replace("\n", "\r\n").encode()
    result = evaluate(case)
    reject(result)  # Legacy prose remains structurally invalid.
    assert result["source_bindings_valid"] is True
    assert result["lead_plan_representation"] == "windows_write_text"
    assert result["original_text_sha256"] == case.stop["plan_sha256"] == digest(text.encode())
    assert result["source_files"]["lead-plan.txt"]["sha256"] == digest(case.lead)


def test_structured_plan_windows_representation_can_complete(case):
    case.lead = case.text.replace("\n", "\r\n").encode()
    result = evaluate(case)
    assert result["completed"] is True
    assert result["lead_plan_representation"] == "windows_write_text"


@pytest.mark.parametrize("change", ["trim", "append", "bom", "partial_translation", "invalid_utf8"])
def test_no_general_normalization_of_lead_plan(case, change):
    if change == "trim":
        case.lead = case.lead.rstrip()
    elif change == "append":
        case.lead += b" "
    elif change == "bom":
        case.lead = b"\xef\xbb\xbf" + case.lead
    elif change == "partial_translation":
        case.lead = case.lead.replace(b"\n", b"\r\n", 1)
    else:
        case.lead += b"\xff"
    reject(evaluate(case), "lead_plan_mismatch", "invalid_source")


def test_stop_hash_binds_original_text_not_windows_file_bytes(case):
    case.lead = case.text.replace("\n", "\r\n").encode()
    case.stop["plan_sha256"] = digest(case.lead)
    reject(evaluate(case), "stop_plan_mismatch", "invalid_source")


@pytest.mark.parametrize("field", ["contract_bytes", "job_bytes", "session_bytes", "plan_bytes", "stop_bytes"])
@pytest.mark.parametrize("value", [None, True, "{}", bytearray(b"{}")])
def test_all_api_inputs_require_bytes_without_coercion(case, field, value):
    reject(evaluate(case, **{field: value}), "expected_bytes")


@pytest.mark.parametrize("field,limit", [
    ("job_bytes", adapter.MAX_JOB_BYTES), ("session_bytes", adapter.MAX_SESSION_BYTES),
    ("plan_bytes", adapter.MAX_LEAD_PLAN_BYTES), ("stop_bytes", adapter.MAX_STOP_BYTES),
])
def test_source_byte_limits(case, field, limit):
    reject(evaluate(case, **{field: b" " * (limit + 1)}), "byte_limit")


def test_jsonl_line_and_line_count_limits(case):
    reject(evaluate(case, session_bytes=b" " * (adapter.MAX_SESSION_LINE_BYTES + 1)),
           "byte_limit", "invalid_source")
    reject(evaluate(case, session_bytes=b"\n" * (adapter.MAX_SESSION_LINES + 1)),
           "session_line_limit", "invalid_source")


def test_jsonl_depth_control_and_truncated_tail(case):
    deep = b'{"ignored":' + b"[" * 33 + b"0" + b"]" * 33 + b"}\n"
    reject(evaluate(case, session_bytes=deep + inputs(case)["session_bytes"]),
           "json_depth_limit", "invalid_source")
    reject(evaluate(case, session_bytes=inputs(case)["session_bytes"] + b'{"type":'),
           "invalid_json", "invalid_source")


def test_pure_api_performs_no_file_process_or_network_actions(case, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Adapter API attempted an external action")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    assert evaluate(case)["completed"] is True


def write_fixture(tmp_path, case):
    raw = inputs(case)
    paths = {}
    for name, argument in zip(adapter.SOURCE_LIMITS, raw, strict=True):
        path = tmp_path / (name + ".json" if name == "completion-contract" else name)
        path.write_bytes(raw[argument])
        paths[name] = path
    return paths


def cli_arguments(tmp_path, case, paths):
    return [
        "--job-dir", str(tmp_path), "--contract", str(paths["completion-contract"]),
        "--contract-sha256", case.pins["expected_contract_sha256"],
        "--job-id", case.pins["expected_job_id"],
        "--job-contract-sha256", case.pins["expected_job_contract_sha256"],
    ]


@pytest.mark.parametrize("mode,code,status", [
    ("valid", 0, "complete"), ("incomplete", 1, "incomplete"),
    ("sentinel", 1, "no_hypothesis"), ("legacy", 2, "invalid_input"),
    ("binding", 2, "invalid_source"),
])
def test_cli_exit_codes_and_no_source_writes(tmp_path, case, mode, code, status):
    if mode == "incomplete":
        set_text(case, "{}")
    elif mode == "sentinel":
        set_text(case, "NO_HYPOTHESIS")
    elif mode == "legacy":
        set_text(case, "ACK")
    elif mode == "binding":
        case.stop["job_id"] = "another"
    paths = write_fixture(tmp_path, case)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = subprocess.run(
        [sys.executable, "-m", "ephy_worker.plan_completion_adapter", *cli_arguments(tmp_path, case, paths)],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == code
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == status
    assert report["completed"] is (code == 0)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_cli_reads_only_fixed_inputs_never_job_specified_paths(tmp_path, case, monkeypatch, capsys):
    paths = write_fixture(tmp_path, case)
    original = adapter._read_one
    seen = []

    def observe(path, limit, location):
        seen.append(path)
        return original(path, limit, location)

    monkeypatch.setattr(adapter, "_read_one", observe)
    assert adapter.main(cli_arguments(tmp_path, case, paths)) == 0
    assert json.loads(capsys.readouterr().out)["completed"] is True
    assert set(seen) == set(paths.values()) and len(seen) == 10


@pytest.mark.parametrize("change", ["bytes", "replacement"])
def test_cli_detects_source_changes_without_retry(tmp_path, case, monkeypatch, capsys, change):
    paths = write_fixture(tmp_path, case)
    original = adapter._read_one
    calls = 0

    def mutate_after_first_pass(path, limit, location):
        nonlocal calls
        result = original(path, limit, location)
        calls += 1
        if calls == 5:
            changed = paths["job.json"]
            if change == "bytes":
                changed.write_bytes(changed.read_bytes() + b" ")
            else:
                replacement = tmp_path / "replacement"
                replacement.write_bytes(changed.read_bytes())
                replacement.replace(changed)
        return result

    monkeypatch.setattr(adapter, "_read_one", mutate_after_first_pass)
    assert adapter.main(cli_arguments(tmp_path, case, paths)) == 2
    reject(json.loads(capsys.readouterr().out), "source_changed", "invalid_input")
    assert calls < 10


def test_cli_byte_comparison_detects_changes_even_with_same_mock_identity(tmp_path, case, monkeypatch, capsys):
    paths = write_fixture(tmp_path, case)
    original = adapter._read_one
    calls = 0

    def alter_second_observation(path, limit, location):
        nonlocal calls
        raw, identity = original(path, limit, location)
        calls += 1
        if calls == 7:
            raw = raw[:-1] + b" "
        return raw, identity

    monkeypatch.setattr(adapter, "_read_one", alter_second_observation)
    assert adapter.main(cli_arguments(tmp_path, case, paths)) == 2
    reject(json.loads(capsys.readouterr().out), "source_changed", "invalid_input")


@pytest.mark.parametrize("change", ["missing", "directory", "oversize"])
def test_cli_source_read_errors_are_json(tmp_path, case, capsys, change):
    paths = write_fixture(tmp_path, case)
    path = paths["lead-plan.txt"]
    if change == "oversize":
        path.write_bytes(b" " * (adapter.MAX_LEAD_PLAN_BYTES + 1))
        expected = "byte_limit"
    else:
        path.unlink()
        expected = "io_error"
        if change == "directory":
            path.mkdir()
            expected = "non_regular_source"
    assert adapter.main(cli_arguments(tmp_path, case, paths)) == 2
    reject(json.loads(capsys.readouterr().out), expected, "invalid_input")


def test_cli_missing_and_unknown_arguments_are_machine_json(case, tmp_path, capsys):
    assert adapter.main([]) == 2
    reject(json.loads(capsys.readouterr().out), "cli_arguments", "invalid_input")
    paths = write_fixture(tmp_path, case)
    assert adapter.main(cli_arguments(tmp_path, case, paths) + ["--execute"]) == 2
    reject(json.loads(capsys.readouterr().out), "cli_arguments", "invalid_input")


def test_adapter_import_does_not_load_formal_runtime():
    result = subprocess.run(
        [sys.executable, "-c",
         ("import sys; import ephy_worker.plan_completion_adapter; "
          "assert 'ephy_worker.formal_runtime' not in sys.modules")],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert result.returncode == 0 and result.stderr == ""


def test_ctime_is_compared_within_same_api_not_across_windows_stat_apis(tmp_path, case, monkeypatch):
    paths = write_fixture(tmp_path, case)
    original = adapter._file_stat

    def path_stat_with_distinct_ctime(path, location):
        value = original(path, location)
        fields = {name: getattr(value, name) for name in
                  ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
        fields["st_ctime_ns"] += 1000000
        return SimpleNamespace(**fields)

    monkeypatch.setattr(adapter, "_file_stat", path_stat_with_distinct_ctime)
    assert adapter._read_inputs(paths)["job.json"] == encode(case.job)


def test_ctime_change_within_path_stat_api_still_rejected(tmp_path, case, monkeypatch):
    paths = write_fixture(tmp_path, case)
    original = adapter._file_stat
    calls = 0

    def changing_ctime(path, location):
        nonlocal calls
        value = original(path, location)
        calls += 1
        fields = {name: getattr(value, name) for name in
                  ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
        fields["st_ctime_ns"] += calls
        return SimpleNamespace(**fields)

    monkeypatch.setattr(adapter, "_file_stat", changing_ctime)
    with pytest.raises(adapter.core._Rejected) as caught:
        adapter._read_inputs(paths)
    assert caught.value.diagnostic["code"] == "source_changed"