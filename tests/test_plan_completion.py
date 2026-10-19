"""Adversarial controls for the standalone structural checker, without live models."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ephy_worker import plan_completion as checker


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def contract():
    return {
        "schema": "ephy.plan-completion-contract.v1",
        "requirements": [{"id": "R1", "text": "欠落を拒否する"}, {"id": "R2", "text": "短い計画を受理する"}],
        "allowed_files": ["src/checker.py", "tests/test_checker.py"],
        "semantic_scope": "計画の構造確認のみ",
        "prohibitions": [{"id": "P1", "text": "本番を変更しない"}, {"id": "P2", "text": "実行しない"}],
        "checks": [
            {"id": "C1", "argv": ["python", "-m", "pytest", "-q"]},
            {"id": "C2", "argv": ["ruff", "check", "src/checker.py"]},
        ],
    }


@pytest.fixture
def plan(contract):
    return {
        "schema": "ephy.plan.v1",
        "contract_sha256": digest(encode(contract)),
        "hypothesis": "構造検査で欠落を検出",
        "scope": {"files": ["src/checker.py"], "semantic_scope": contract["semantic_scope"]},
        "prohibitions": ["P1", "P2"],
        "acceptance": [
            {"id": "A1", "requirement_ids": ["R1"], "criterion": "欠落は拒否"},
            {"id": "A2", "requirement_ids": ["R2"], "criterion": "短い計画は受理"},
        ],
        "verification": [
            {"check_id": "C1", "acceptance_ids": ["A1", "A2"]},
            {"check_id": "C2", "acceptance_ids": ["A2"]},
        ],
    }


def response(text, **metadata):
    return encode({
        "role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": text}], **metadata,
    })


def inspect(contract, plan):
    raw = encode(contract)
    return checker.check_plan_completion(raw, response(encode(plan).decode()), expected_contract_sha256=digest(raw))


def inspect_text(contract, text, **metadata):
    raw = encode(contract)
    return checker.check_plan_completion(raw, response(text, **metadata), expected_contract_sha256=digest(raw))


def assert_rejected(result, status=None, code=None):
    assert result["passed"] is False
    assert result["assurance"] == "structural_only"
    assert result["semantic_correctness"] == "not_assessed"
    assert result["checks_executed"] is False
    assert result["diagnostics"]
    if status is not None:
        assert result["status"] == status
    if code is not None:
        assert result["diagnostics"][0]["code"] == code


def test_concise_japanese_complete_and_deterministic(contract, plan):
    raw_contract = encode(contract)
    raw_plan = encode(plan)
    raw_response = response(raw_plan.decode())
    result = inspect(contract, plan)
    assert result == inspect(contract, plan)
    assert result == {
        "schema": "ephy.plan-completion-result.v1",
        "status": "complete", "passed": True,
        "assurance": "structural_only", "semantic_correctness": "not_assessed",
        "checks_executed": False,
        "contract_sha256": digest(raw_contract),
        "response_sha256": digest(raw_response),
        "plan_sha256": digest(raw_plan), "diagnostics": [],
    }


def test_concise_english_and_no_substantive_claim(contract, plan):
    contract["requirements"] = [{"id": "R1", "text": "Reject missing fields."}]
    contract["semantic_scope"] = "Check structure."
    plan["contract_sha256"] = digest(encode(contract))
    plan["hypothesis"] = "Check fields."
    plan["scope"]["semantic_scope"] = "Check structure."
    plan["acceptance"] = [{"id": "A1", "requirement_ids": ["R1"], "criterion": "done"}]
    for verification in plan["verification"]:
        verification["acceptance_ids"] = ["A1"]
    result = inspect(contract, plan)
    assert result["passed"] is True
    # Structurally present prose does not establish a useful or correct criterion.
    assert result["semantic_correctness"] == "not_assessed"


def test_many_to_many_requirement_mapping_is_valid(contract, plan):
    plan["acceptance"][0]["requirement_ids"] = ["R1", "R2"]
    assert inspect(contract, plan)["passed"] is True


def test_prose_whitespace_and_normal_unicode_are_not_length_proxies(contract, plan):
    plan["hypothesis"] = "  確認\nします\t🙂  "
    assert inspect(contract, plan)["passed"] is True


@pytest.mark.parametrize("field", [
    "schema", "contract_sha256", "hypothesis", "scope", "prohibitions", "acceptance", "verification",
])
def test_each_required_plan_field_missing(contract, plan, field):
    del plan[field]
    assert_rejected(inspect(contract, plan), "incomplete", "missing_field")


@pytest.mark.parametrize("field", [
    "schema", "requirements", "allowed_files", "semantic_scope", "prohibitions", "checks",
])
def test_each_required_contract_field_missing(contract, plan, field):
    del contract[field]
    assert_rejected(inspect(contract, plan), "invalid_contract", "missing_field")


@pytest.mark.parametrize("container,field", [
    ("scope", "files"), ("scope", "semantic_scope"),
    ("acceptance", "id"), ("acceptance", "requirement_ids"), ("acceptance", "criterion"),
    ("verification", "check_id"), ("verification", "acceptance_ids"),
])
def test_required_nested_plan_field_missing(contract, plan, container, field):
    target = plan[container] if container == "scope" else plan[container][0]
    del target[field]
    assert_rejected(inspect(contract, plan), "incomplete", "missing_field")


@pytest.mark.parametrize("text", ["", " ", "ACK", "read complete", "了解", "NO_HYPOTHESIS extra",
                                 "```json\n{}\n```", '{"schema":', "{} commentary", "{}{}"])
def test_ack_read_empty_truncated_and_embedded_prose_never_pass(contract, text):
    assert_rejected(inspect_text(contract, text), "invalid_input", "invalid_json")


@pytest.mark.parametrize("value", [None, True, 1, [], "ACK", {"ack": True}, {"read": "README.md"}])
def test_non_plan_json_never_passes(contract, value):
    assert_rejected(inspect_text(contract, encode(value).decode()), "incomplete")


@pytest.mark.parametrize("stop", ["length", "max_tokens", "toolUse", "error", "aborted", "", None, True, 1, {}])
def test_complete_looking_nonterminal_message_is_incomplete(contract, plan, stop):
    assert_rejected(inspect_text(contract, encode(plan).decode(), stopReason=stop),
                    "incomplete", "nonterminal_response")


def test_missing_stop_and_metadata_cannot_supply_it(contract, plan):
    envelope = json.loads(response(encode(plan).decode()))
    del envelope["stopReason"]
    envelope["metadata"] = {"stopReason": "stop"}
    raw = encode(contract)
    result = checker.check_plan_completion(raw, encode(envelope), expected_contract_sha256=digest(raw))
    assert_rejected(result, "incomplete", "nonterminal_response")


@pytest.mark.parametrize("role", ["toolResult", "user", "system", "", None, True])
def test_nonassistant_rejected(contract, plan, role):
    assert_rejected(inspect_text(contract, encode(plan).decode(), role=role), "incomplete", "response_role")


@pytest.mark.parametrize("block", [
    {"type": "toolCall", "name": "governance_ack", "arguments": {}},
    {"type": "toolCall", "name": "read", "arguments": {"path": "README.md"}},
    {"type": "image", "data": ""},
    {"type": "unknown", "text": "{}"},
])
@pytest.mark.parametrize("include_plan", [False, True])
def test_any_non_text_content_rejected(contract, plan, block, include_plan):
    blocks = [{"type": "text", "text": encode(plan).decode()}] if include_plan else []
    blocks.append(block)
    assert_rejected(inspect_text(contract, "", content=blocks), "incomplete", "non_text_content")


@pytest.mark.parametrize("content", [None, [], {}, True, ["read"], [{"type": "text"}],
                                     [{"type": "text", "text": 7}]])
def test_malformed_content_is_invalid_input(contract, content):
    assert_rejected(inspect_text(contract, "", content=content), "invalid_input")


def test_known_thinking_is_ignored_as_evidence_but_bound_by_response_hash(contract, plan):
    text = encode(plan).decode()
    blocks = [{"type": "thinking", "thinking": "synthetic reasoning"},
              {"type": "text", "text": text}]
    raw = response("", content=blocks)
    result = checker.check_plan_completion(encode(contract), raw,
                                          expected_contract_sha256=digest(encode(contract)))
    assert result["passed"] is True
    assert result["plan_sha256"] == digest(text.encode())
    assert result["response_sha256"] == digest(raw)
    assert "synthetic reasoning" not in json.dumps(result)
    assert_rejected(inspect_text(contract, "", content=blocks[:1]),
                    "incomplete", "no_text_content")
    # A complete-looking plan in thinking cannot supply missing final evidence.
    blocks[0]["thinking"] = text
    blocks[1]["text"] = "ACK"
    assert_rejected(inspect_text(contract, "", content=blocks), "invalid_input", "invalid_json")


@pytest.mark.parametrize("block", [{"type": "thinking"}, {"type": "thinking", "thinking": None},
                                  {"type": "thinking", "thinking": 1}])
def test_malformed_thinking_blocks_are_invalid(contract, plan, block):
    assert_rejected(inspect_text(contract, "", content=[
        block, {"type": "text", "text": encode(plan).decode()},
    ]), "invalid_input", "expected_string")


def test_exact_multiblock_join_with_ignored_pi_metadata(contract, plan):
    text = encode(plan).decode()
    blocks = [
        {"type": "text", "text": text[:7], "textSignature": "signature"},
        {"type": "text", "text": text[7:], "metadata": {"ignored": True}},
    ]
    result = inspect_text(
        contract, "", content=blocks, model="model", provider="provider", timestamp=1,
        usage={"cost": {"total": 0.1}}, metadata={"role": "user", "stopReason": "length"},
    )
    assert result["passed"] is True
    assert result["plan_sha256"] == digest(text.encode())


def test_no_separator_is_inserted_and_no_last_object_is_selected(contract, plan):
    blocks = [{"type": "text", "text": encode(plan).decode()}, {"type": "text", "text": "{}"}]
    assert_rejected(inspect_text(contract, "", content=blocks), "invalid_input", "invalid_json")


@pytest.mark.parametrize("text", ["NO_HYPOTHESIS", " \nNO_HYPOTHESIS\t", "\u3000NO_HYPOTHESIS\u3000"])
def test_no_hypothesis_is_distinct_non_success(contract, text):
    result = inspect_text(contract, text)
    assert_rejected(result, "no_hypothesis", "no_hypothesis")
    assert result["plan_sha256"] == digest(text.encode())


def test_no_hypothesis_requires_terminal_valid_contract(contract):
    assert_rejected(inspect_text(contract, "NO_HYPOTHESIS", stopReason="length"),
                    "incomplete", "nonterminal_response")
    contract["checks"] = []
    assert_rejected(inspect_text(contract, "NO_HYPOTHESIS"), "invalid_contract", "empty_array")
    assert_rejected(inspect_text({
        **contract, "checks": [{"id": "C1", "argv": ["check"]}],
    }, '"NO_HYPOTHESIS"'), "incomplete", "expected_object")


@pytest.mark.parametrize("pin", [None, "", "a" * 63, "a" * 65, "A" * 64, True, 1, {}, "g" * 64])
def test_caller_hash_pin_is_required_and_strict(contract, plan, pin):
    result = checker.check_plan_completion(encode(contract), response(encode(plan).decode()),
                                           expected_contract_sha256=pin)
    assert_rejected(result, "invalid_contract", "expected_hash_invalid")


def test_raw_contract_hash_binding_not_reserialized(contract, plan):
    raw = encode(contract)
    spaced = json.dumps(contract, ensure_ascii=False, indent=2).encode()
    result = checker.check_plan_completion(spaced, response(encode(plan).decode()),
                                           expected_contract_sha256=digest(raw))
    assert_rejected(result, "invalid_contract", "contract_hash_mismatch")
    result = checker.check_plan_completion(spaced, response(encode(plan).decode()),
                                           expected_contract_sha256=digest(spaced))
    assert_rejected(result, "incomplete", "contract_hash_mismatch")
    plan["contract_sha256"] = digest(spaced)
    assert checker.check_plan_completion(spaced, response(encode(plan).decode()),
                                          expected_contract_sha256=digest(spaced))["passed"] is True


@pytest.mark.parametrize("value", ["0" * 64, True, None, {}, [], 1])
def test_plan_hash_rebinding_is_rejected(contract, plan, value):
    plan["contract_sha256"] = value
    assert_rejected(inspect(contract, plan), "incomplete", "contract_hash_mismatch")


@pytest.mark.parametrize("where", ["plan", "scope", "acceptance", "verification"])
def test_unknown_plan_fields_rejected(contract, plan, where):
    target = plan if where == "plan" else plan[where] if where == "scope" else plan[where][0]
    target["extra"] = "ignored?"
    assert_rejected(inspect(contract, plan), "incomplete", "unknown_field")


@pytest.mark.parametrize("where", ["contract", "requirements", "prohibitions", "checks"])
def test_unknown_contract_fields_rejected(contract, plan, where):
    target = contract if where == "contract" else contract[where][0]
    target["optional"] = True
    assert_rejected(inspect(contract, plan), "invalid_contract", "unknown_field")


@pytest.mark.parametrize("field", ["requirements", "prohibitions", "checks"])
def test_duplicate_contract_ids_rejected(contract, plan, field):
    contract[field].append(copy.deepcopy(contract[field][0]))
    assert_rejected(inspect(contract, plan), "invalid_contract", "duplicate_id")


@pytest.mark.parametrize("field", ["acceptance", "verification"])
def test_duplicate_plan_record_ids_rejected(contract, plan, field):
    plan[field].append(copy.deepcopy(plan[field][0]))
    assert_rejected(inspect(contract, plan), "incomplete", "duplicate_id")


@pytest.mark.parametrize("field", ["requirements", "prohibitions", "checks", "allowed_files"])
@pytest.mark.parametrize("value", [[], None, True, {}, "R1"])
def test_contract_collections_must_be_nonempty_arrays(contract, plan, field, value):
    contract[field] = value
    assert_rejected(inspect(contract, plan), "invalid_contract")


@pytest.mark.parametrize("field", ["prohibitions", "acceptance", "verification"])
@pytest.mark.parametrize("value", [[], None, True, {}, "A1"])
def test_plan_collections_must_be_nonempty_arrays(contract, plan, field, value):
    plan[field] = value
    assert_rejected(inspect(contract, plan), "incomplete")


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "", " \n\t", "x" * (checker.MAX_TEXT_CHARS + 1)])
@pytest.mark.parametrize("field", ["hypothesis", "criterion", "semantic_scope"])
def test_required_prose_is_nonblank_typed_and_bounded(contract, plan, field, value):
    if field == "criterion":
        plan["acceptance"][0][field] = value
    elif field == "semantic_scope":
        plan["scope"][field] = value
    else:
        plan[field] = value
    assert_rejected(inspect(contract, plan), "incomplete")


@pytest.mark.parametrize("value", ["", "bad id", " 日本語", "R/1", "R\n1", "1R", "R" * 65, True, 1])
def test_malformed_ids_are_not_coerced(contract, plan, value):
    plan["acceptance"][0]["id"] = value
    assert_rejected(inspect(contract, plan), "incomplete")


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../x", "src/../x", "src/./x", "./src/checker.py", "src//checker.py",
    "src/", "", "C:x", "C:/x", "//server/share", "src\\checker.py", "src/x:stream",
    "src/x.", "src/x ", " src/x", "src/ x", "src/*", "src/?", "src/<x>",
    "src/NUL", "src/CON.txt", "src/com1.log", "src/lpt².txt", "src/line\nfile",
])
def test_scope_path_escapes_and_noncanonical_paths_rejected(contract, plan, path):
    plan["scope"]["files"] = [path]
    assert_rejected(inspect(contract, plan), "incomplete")


def test_exact_allowed_subset_case_and_semantic_scope(contract, plan):
    plan["scope"]["files"] = ["src/Checker.py"]
    assert_rejected(inspect(contract, plan), "incomplete", "scope_escape")
    plan["scope"]["files"] = ["outside.py"]
    assert_rejected(inspect(contract, plan), "incomplete", "scope_escape")
    plan["scope"]["files"] = ["src/checker.py"]
    plan["scope"]["semantic_scope"] += " "
    assert_rejected(inspect(contract, plan), "incomplete", "semantic_scope_mismatch")


@pytest.mark.parametrize("value,code", [
    ([], "empty_array"), (["src/checker.py", "src/checker.py"], "duplicate_reference"),
    (["src/checker.py", "src/Checker.py"], "path_collision"),
])
def test_scope_list_has_no_empty_or_duplicate_entries(contract, plan, value, code):
    plan["scope"]["files"] = value
    assert_rejected(inspect(contract, plan), "incomplete", code)
    contract["allowed_files"] = value
    assert_rejected(inspect(contract, plan), "invalid_contract", code)


def test_unicode_paths_are_valid_exact_names(contract, plan):
    contract["allowed_files"] = ["src/計画.py"]
    plan["scope"]["files"] = ["src/計画.py"]
    plan["contract_sha256"] = digest(encode(contract))
    assert inspect(contract, plan)["passed"] is True


@pytest.mark.parametrize("value,code", [
    (["P1"], "prohibition_coverage"), (["P1", "P1"], "duplicate_reference"),
    (["P1", "P3"], "unknown_reference"),
])
def test_prohibition_binding(contract, plan, value, code):
    plan["prohibitions"] = value
    assert_rejected(inspect(contract, plan), "incomplete", code)


@pytest.mark.parametrize("value,code", [
    ([], "empty_array"), (["R1", "R1"], "duplicate_reference"), (["R3"], "unknown_reference"),
])
def test_each_acceptance_has_valid_unique_requirement_references(contract, plan, value, code):
    plan["acceptance"][0]["requirement_ids"] = value
    assert_rejected(inspect(contract, plan), "incomplete", code)


def test_uncovered_requirement_is_rejected(contract, plan):
    plan["acceptance"][1]["requirement_ids"] = ["R1"]
    assert_rejected(inspect(contract, plan), "incomplete", "requirement_coverage")


@pytest.mark.parametrize("value,code", [
    ([], "empty_array"), (["A1", "A1"], "duplicate_reference"), (["A3"], "unknown_reference"),
])
def test_each_verification_has_valid_unique_acceptance_references(contract, plan, value, code):
    plan["verification"][0]["acceptance_ids"] = value
    assert_rejected(inspect(contract, plan), "incomplete", code)


def test_all_acceptance_records_must_be_verified(contract, plan):
    for item in plan["verification"]:
        item["acceptance_ids"] = ["A1"]
    assert_rejected(inspect(contract, plan), "incomplete", "acceptance_coverage")


def test_all_mandatory_checks_once_and_only_known_checks(contract, plan):
    plan["verification"].pop()
    assert_rejected(inspect(contract, plan), "incomplete", "check_coverage")
    plan["verification"].append({"check_id": "C3", "acceptance_ids": ["A1"]})
    assert_rejected(inspect(contract, plan), "incomplete", "unknown_check")


@pytest.mark.parametrize("value", [[], [" "], [1], [True], [None], {}, "pytest", ["py\nth on"]])
def test_contract_argv_is_only_a_nonempty_string_array(contract, plan, value):
    contract["checks"][0]["argv"] = value
    assert_rejected(inspect(contract, plan), "invalid_contract")


@pytest.mark.parametrize("where", ["contract", "response", "plan"])
@pytest.mark.parametrize("raw,code", [
    (b'{"x":1,"x":2}', "duplicate_key"),
    (b'{"metadata":{"x":1,"x":2}}', "duplicate_key"),
    (b'{"x":NaN}', "nonfinite_number"),
    (b'{"x":Infinity}', "nonfinite_number"),
    (b'{"x":-Infinity}', "nonfinite_number"),
    (b'{"x":1e999}', "nonfinite_number"),
    (b'{"x":' + b"1" * (checker.MAX_NUMBER_CHARS + 1) + b"}", "number_limit"),
    (b"\xff", "invalid_utf8"),
    (b"\xef\xbb\xbf{}", "invalid_json"),
    (b"{}", None),
])
def test_strict_json_at_every_boundary(contract, where, raw, code):
    contract_raw = raw if where == "contract" else encode(contract)
    if where == "plan":
        if code == "invalid_utf8":
            # Invalid raw UTF-8 is tested at the two byte inputs; a text block is Unicode.
            response_raw = b"\xff"
        else:
            response_raw = response(raw.decode("utf-8"))
    else:
        response_raw = raw if where == "response" else response("{}")
    result = checker.check_plan_completion(contract_raw, response_raw,
                                          expected_contract_sha256=digest(contract_raw))
    # Embedded BOM is rejected by the outer string guard before inner JSON parsing.
    expected_code = "forbidden_character" if where == "plan" and raw.startswith(b"\xef\xbb\xbf") else code
    assert_rejected(result, code=expected_code)


@pytest.mark.parametrize("character", ["\u0000", "\u001b", "\u007f", "\u0085", "\u202e", "\u200b", "\ud800"])
@pytest.mark.parametrize("where", ["hypothesis", "metadata"])
def test_escaped_forbidden_characters_and_surrogates_rejected(contract, plan, character, where):
    if where == "hypothesis":
        plan["hypothesis"] = "x" + character
        text = json.dumps(plan, ensure_ascii=True)
        envelope = response(text)
    else:
        envelope = json.dumps({
            "role": "assistant", "stopReason": "stop",
            "content": [{"type": "text", "text": encode(plan).decode()}], "metadata": character,
        }, ensure_ascii=True).encode()
    raw = encode(contract)
    result = checker.check_plan_completion(raw, envelope, expected_contract_sha256=digest(raw))
    assert_rejected(result, code="forbidden_character")


@pytest.mark.parametrize("where", ["contract", "response", "plan"])
def test_deep_untrusted_json_is_invalid_without_recursion_crash(contract, where):
    nested = "[" * (checker.MAX_JSON_DEPTH + 1) + "0" + "]" * (checker.MAX_JSON_DEPTH + 1)
    raw_contract = nested.encode() if where == "contract" else encode(contract)
    raw_response = nested.encode() if where == "response" else response(nested)
    result = checker.check_plan_completion(raw_contract, raw_response,
                                          expected_contract_sha256=digest(raw_contract))
    assert_rejected(result, code="json_depth_limit")


def test_deep_ignored_metadata_and_large_node_count_rejected(contract, plan):
    text = encode(plan).decode()
    nested = "[" * (checker.MAX_JSON_DEPTH + 1) + "0" + "]" * (checker.MAX_JSON_DEPTH + 1)
    raw = response(text)[:-1] + b',"metadata":' + nested.encode() + b"}"
    result = checker.check_plan_completion(encode(contract), raw,
                                          expected_contract_sha256=digest(encode(contract)))
    assert_rejected(result, "invalid_input", "json_depth_limit")
    result = inspect_text(contract, text, metadata=[0] * checker.MAX_JSON_NODES)
    assert_rejected(result, "invalid_input", "json_node_limit")


@pytest.mark.parametrize("where,limit", [
    ("contract", checker.MAX_CONTRACT_BYTES), ("response", checker.MAX_RESPONSE_BYTES),
    ("plan", checker.MAX_PLAN_BYTES),
])
def test_oversized_raw_inputs_rejected_with_no_prefix_hash(contract, where, limit):
    raw = b" " * (limit + 1)
    contract_raw = raw if where == "contract" else encode(contract)
    response_raw = raw if where == "response" else response(raw.decode())
    result = checker.check_plan_completion(contract_raw, response_raw,
                                          expected_contract_sha256=digest(contract_raw))
    assert_rejected(result, code="byte_limit")
    assert result[where + "_sha256"] is None


@pytest.mark.parametrize("raw", ["{}", None, True, {}, bytearray(b"{}")])
def test_api_requires_bytes_and_never_coerces_inputs(contract, raw):
    result = checker.check_plan_completion(encode(contract), raw,
                                          expected_contract_sha256=digest(encode(contract)))
    assert_rejected(result, "invalid_input", "expected_bytes")
    result = checker.check_plan_completion(raw, b"{}", expected_contract_sha256="0" * 64)
    assert_rejected(result, "invalid_contract", "expected_bytes")


def test_all_prefix_truncations_fail(contract, plan):
    plan_text = encode(plan).decode()
    for end in range(len(plan_text)):
        assert inspect_text(contract, plan_text[:end])["passed"] is False, end
    raw = response(plan_text)
    for end in range(len(raw)):
        result = checker.check_plan_completion(encode(contract), raw[:end],
                                              expected_contract_sha256=digest(encode(contract)))
        assert result["passed"] is False, end
    assert inspect_text(contract, plan_text + "\n  ")["passed"] is True


def test_json_brackets_inside_strings_do_not_count_as_depth(contract, plan):
    plan["hypothesis"] = "[" * 50 + 'escaped " quote and backslash \\'
    assert inspect(contract, plan)["passed"] is True


def test_untrusted_command_and_metadata_never_execute_or_read(contract, plan, monkeypatch):
    contract["checks"][0]["argv"] = ["python", "-c", "raise RuntimeError('must not run')"]
    plan["contract_sha256"] = digest(encode(contract))

    def forbidden(*args, **kwargs):
        raise AssertionError("Pure checker attempted I/O or execution")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = inspect_text(contract, encode(plan).decode(), metadata={"command": "run", "file": "../secret"})
    assert result["passed"] is True
    assert result["checks_executed"] is False


def run_cli(tmp_path, contract_raw, response_raw, *extra):
    contract_file = tmp_path / "contract.json"
    response_file = tmp_path / "response.json"
    contract_file.write_bytes(contract_raw)
    response_file.write_bytes(response_raw)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = subprocess.run(
        [sys.executable, "-m", "ephy_worker.plan_completion",
         "--contract", str(contract_file), "--contract-sha256", digest(contract_raw),
         "--response", str(response_file), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


@pytest.mark.parametrize("mode,status,exit_code", [
    ("complete", "complete", 0), ("incomplete", "incomplete", 1),
    ("no_hypothesis", "no_hypothesis", 1), ("invalid_input", "invalid_input", 2),
    ("invalid_contract", "invalid_contract", 2),
])
def test_cli_exit_semantics_and_read_only_inputs(tmp_path, contract, plan, mode, status, exit_code):
    if mode == "incomplete":
        del plan["hypothesis"]
    contract_raw = b"{}" if mode == "invalid_contract" else encode(contract)
    raw = response("NO_HYPOTHESIS" if mode == "no_hypothesis" else encode(plan).decode())
    if mode == "invalid_input":
        raw = b"{"
    actual_exit, report = run_cli(tmp_path, contract_raw, raw)
    assert actual_exit == exit_code
    assert report["status"] == status
    assert report["passed"] is (exit_code == 0)


def test_cli_oversize_read_does_not_report_prefix_hash(tmp_path, contract):
    code, report = run_cli(tmp_path, encode(contract), b" " * (checker.MAX_RESPONSE_BYTES + 100))
    assert code == 2
    assert_rejected(report, "invalid_input", "byte_limit")
    assert report["response_sha256"] is None


@pytest.mark.parametrize("arguments,diagnostic", [
    ([], "cli_arguments"),
    (["--contract", "missing", "--contract-sha256", "0" * 64, "--response", "missing"], "io_error"),
])
def test_cli_argument_and_io_errors_are_json(tmp_path, arguments, diagnostic):
    result = subprocess.run([sys.executable, "-m", "ephy_worker.plan_completion", *arguments],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 2
    assert result.stderr == ""
    assert_rejected(json.loads(result.stdout), "invalid_input", diagnostic)


def test_unknown_cli_argument_is_rejected(tmp_path, contract, plan):
    code, report = run_cli(tmp_path, encode(contract), response(encode(plan).decode()), "--execute")
    assert code == 2
    assert_rejected(report, "invalid_input", "cli_arguments")


@pytest.mark.parametrize("field", ["requirements", "prohibitions", "checks", "allowed_files"])
def test_contract_array_limits_are_enforced_before_record_processing(contract, plan, field):
    contract[field] = [contract[field][0]] * (checker.MAX_ITEMS + 1)
    assert_rejected(inspect(contract, plan), "invalid_contract", "array_limit")


def test_argv_item_and_argument_length_limits(contract, plan):
    contract["checks"][0]["argv"] = ["check"] * (checker.MAX_ARGV_ITEMS + 1)
    assert_rejected(inspect(contract, plan), "invalid_contract", "array_limit")
    contract["checks"][0]["argv"] = ["x" * (checker.MAX_ARGUMENT_CHARS + 1)]
    assert_rejected(inspect(contract, plan), "invalid_contract", "string_limit")


def test_plan_and_response_exact_byte_limits_can_pass(contract, plan):
    raw_plan = encode(plan)
    padded = raw_plan + b" " * (checker.MAX_PLAN_BYTES - len(raw_plan))
    result = inspect_text(contract, padded.decode())
    assert result["passed"] is True
    assert result["plan_sha256"] == digest(padded)
    raw_response = response(encode(plan).decode())
    padded_response = raw_response + b" " * (checker.MAX_RESPONSE_BYTES - len(raw_response))
    result = checker.check_plan_completion(
        encode(contract), padded_response, expected_contract_sha256=digest(encode(contract)),
    )
    assert result["passed"] is True
    assert result["response_sha256"] == digest(padded_response)


def test_contract_exact_byte_limit_can_pass(contract, plan):
    raw = encode(contract)
    raw += b" " * (checker.MAX_CONTRACT_BYTES - len(raw))
    plan["contract_sha256"] = digest(raw)
    result = checker.check_plan_completion(raw, response(encode(plan).decode()), expected_contract_sha256=digest(raw))
    assert result["passed"] is True
    assert result["contract_sha256"] == digest(raw)