"""Standalone, deterministic structural plan checks; no runtime integration or execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

CONTRACT_SCHEMA = "ephy.plan-completion-contract.v1"
PLAN_SCHEMA = "ephy.plan.v1"
RESULT_SCHEMA = "ephy.plan-completion-result.v1"
MAX_CONTRACT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PLAN_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 32768
MAX_ITEMS = 256
MAX_TEXT_CHARS = 8192
MAX_PATH_CHARS = 512
MAX_ARGV_ITEMS = 128
MAX_ARGUMENT_CHARS = 1024
MAX_NUMBER_CHARS = 128

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", re.IGNORECASE)


class _Rejected(Exception):
    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.diagnostic = {"code": code, "path": path, "message": message}


def _reject(code: str, path: str, message: str) -> None:
    raise _Rejected(code, path, message)


def _hash(raw: Any, limit: int) -> str | None:
    # Never label the CLI's oversized prefix as a hash of the complete file.
    if type(raw) is bytes and len(raw) <= limit:
        return hashlib.sha256(raw).hexdigest()
    return None


def _report(contract: Any = None, response: Any = None) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "status": "invalid_input",
        "passed": False,
        "assurance": "structural_only",
        "semantic_correctness": "not_assessed",
        "checks_executed": False,
        "contract_sha256": _hash(contract, MAX_CONTRACT_BYTES),
        "response_sha256": _hash(response, MAX_RESPONSE_BYTES),
        "plan_sha256": None,
        "diagnostics": [],
    }


def _failure(report: dict[str, Any], status: str, error: _Rejected) -> dict[str, Any]:
    report["status"] = status
    report["diagnostics"] = [error.diagnostic]
    return report


def _safe_characters(value: str, path: str) -> None:
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\t\n\r"
        for char in value
    ):
        _reject("forbidden_character", path, "Control, format, and surrogate characters are forbidden.")


def _string(value: Any, path: str, limit: int = MAX_TEXT_CHARS) -> str:
    if type(value) is not str:
        _reject("expected_string", path, "Expected a string without coercion.")
    if not value.strip():
        _reject("empty_string", path, "A nonblank string is required.")
    if len(value) > limit:
        _reject("string_limit", path, "String exceeds the field's character limit.")
    _safe_characters(value, path)
    return value


def _identifier(value: Any, path: str) -> str:
    value = _string(value, path, 64)
    if _ID.fullmatch(value) is None:
        _reject("invalid_id", path, "IDs must match [A-Za-z][A-Za-z0-9_.-]{0,63}.")
    return value


def _file_path(value: Any, path: str) -> str:
    value = _string(value, path, MAX_PATH_CHARS)
    if any(char in value for char in '\\:*?"<>|\t\n\r'):
        _reject("invalid_path", path, "Expected an exact portable relative path with forward slashes.")
    for part in value.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part != part.strip()
            or part.endswith(".")
            or _DEVICE.fullmatch(part.split(".", 1)[0]) is not None
        ):
            _reject("invalid_path", path, "Path contains an empty, noncanonical, or reserved component.")
    return value


def _object(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        _reject("expected_object", path, "Expected an object.")
    if fields - value.keys():
        _reject("missing_field", path, "Missing required fields: " + ", ".join(sorted(fields - value.keys())))
    if value.keys() - fields:
        # Do not echo untrusted unknown field names into diagnostics.
        _reject("unknown_field", path, "Object contains fields outside the schema.")
    return value


def _array(value: Any, path: str, limit: int = MAX_ITEMS) -> list[Any]:
    if type(value) is not list:
        _reject("expected_array", path, "Expected an array without coercion.")
    if not value:
        _reject("empty_array", path, "At least one item is required.")
    if len(value) > limit:
        _reject("array_limit", path, "Array exceeds the field's item limit.")
    return value


def _unique(values: list[str], path: str) -> set[str]:
    if len(set(values)) != len(values):
        _reject("duplicate_reference", path, "Values must be unique within this list.")
    return set(values)


def _references(value: Any, known: set[str], path: str) -> set[str]:
    values = [_identifier(item, f"{path}/{i}") for i, item in enumerate(_array(value, path))]
    result = _unique(values, path)
    if result - known:
        _reject("unknown_reference", path, "Reference does not exist in the required namespace.")
    return result


def _paths(value: Any, path: str) -> set[str]:
    values = [_file_path(item, f"{path}/{i}") for i, item in enumerate(_array(value, path))]
    result = _unique(values, path)
    if len({item.casefold() for item in values}) != len(values):
        _reject("path_collision", path, "Paths must not collide under case folding.")
    return result


def _depth_guard(text: str, path: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _reject("json_depth_limit", path, "JSON nesting exceeds the fixed limit.")
        elif char in "]}":
            depth -= 1


def _load(raw: Any, path: str, limit: int) -> Any:
    if type(raw) is not bytes:
        _reject("expected_bytes", path, "The API requires raw bytes.")
    if len(raw) > limit:
        _reject("byte_limit", path, "Input exceeds the fixed byte limit.")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("invalid_utf8", path, "Input must be strict UTF-8.")
    _depth_guard(text, path)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _reject("duplicate_key", path, "Duplicate JSON object keys are forbidden.")
            result[key] = value
        return result

    def number(value: str, *, integer: bool) -> int | float:
        if len(value) > MAX_NUMBER_CHARS:
            _reject("number_limit", path, "JSON number exceeds the fixed character limit.")
        parsed = int(value) if integer else float(value)
        if not integer and not math.isfinite(parsed):
            _reject("nonfinite_number", path, "Nonfinite JSON numbers are forbidden.")
        return parsed

    def constant(value: str) -> None:
        _reject("nonfinite_number", path, "Nonfinite JSON constants are forbidden.")

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_int=lambda token: number(token, integer=True),
            parse_float=lambda token: number(token, integer=False),
        )
    except json.JSONDecodeError:
        _reject("invalid_json", path, "Expected one complete JSON value, without fences or trailing data.")
    # Iterative traversal also validates ignored metadata, without trusting it.
    pending = [value]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            _reject("json_node_limit", path, "JSON node count exceeds the fixed limit.")
        if type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is str:
            _safe_characters(item, path)
    return value


def _records(value: Any, fields: set[str], path: str, *, key: str = "id") -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(_array(value, path)):
        location = f"{path}/{i}"
        item = _object(item, fields, location)
        identity = _identifier(item[key], f"{location}/{key}")
        if identity in records:
            _reject("duplicate_id", location, "Record IDs must be unique within their namespace.")
        records[identity] = item
    return records


def _contract(value: Any) -> dict[str, Any]:
    value = _object(
        value, {"schema", "requirements", "allowed_files", "semantic_scope", "prohibitions", "checks"}, "contract"
    )
    if value["schema"] != CONTRACT_SCHEMA:
        _reject("schema_mismatch", "contract/schema", "Unsupported contract schema.")
    _string(value["semantic_scope"], "contract/semantic_scope")
    _paths(value["allowed_files"], "contract/allowed_files")
    for name in ("requirements", "prohibitions"):
        records = _records(value[name], {"id", "text"}, f"contract/{name}")
        for identity, item in records.items():
            _string(item["text"], f"contract/{name}/{identity}/text")
    checks = _records(value["checks"], {"id", "argv"}, "contract/checks")
    for identity, item in checks.items():
        path = f"contract/checks/{identity}/argv"
        for i, argument in enumerate(_array(item["argv"], path, MAX_ARGV_ITEMS)):
            argument = _string(argument, f"{path}/{i}", MAX_ARGUMENT_CHARS)
            if any(char in argument for char in "\t\n\r"):
                _reject("invalid_argument", f"{path}/{i}", "Arguments cannot contain tabs or line breaks.")
    return value


def _plan(value: Any, contract: dict[str, Any], digest: str) -> None:
    value = _object(
        value,
        {"schema", "contract_sha256", "hypothesis", "scope", "prohibitions", "acceptance", "verification"},
        "plan",
    )
    if value["schema"] != PLAN_SCHEMA:
        _reject("schema_mismatch", "plan/schema", "Unsupported plan schema.")
    if type(value["contract_sha256"]) is not str or value["contract_sha256"] != digest:
        _reject("contract_hash_mismatch", "plan/contract_sha256", "Plan must bind the exact frozen contract bytes.")
    _string(value["hypothesis"], "plan/hypothesis")
    scope = _object(value["scope"], {"files", "semantic_scope"}, "plan/scope")
    files = _paths(scope["files"], "plan/scope/files")
    if files - set(contract["allowed_files"]):
        _reject("scope_escape", "plan/scope/files", "Plan files must be an exact subset of allowed_files.")
    _string(scope["semantic_scope"], "plan/scope/semantic_scope")
    if scope["semantic_scope"] != contract["semantic_scope"]:
        _reject("semantic_scope_mismatch", "plan/scope/semantic_scope", "Scope text must match the frozen value.")
    prohibited = {item["id"] for item in contract["prohibitions"]}
    if _references(value["prohibitions"], prohibited, "plan/prohibitions") != prohibited:
        _reject("prohibition_coverage", "plan/prohibitions", "Every frozen prohibition must be referenced.")
    required = {item["id"] for item in contract["requirements"]}
    acceptance = _records(value["acceptance"], {"id", "requirement_ids", "criterion"}, "plan/acceptance")
    covered: set[str] = set()
    for identity, item in acceptance.items():
        path = f"plan/acceptance/{identity}"
        covered.update(_references(item["requirement_ids"], required, f"{path}/requirement_ids"))
        _string(item["criterion"], f"{path}/criterion")
    if covered != required:
        _reject("requirement_coverage", "plan/acceptance", "Every frozen requirement must be covered.")
    checks = {item["id"] for item in contract["checks"]}
    verification = _records(
        value["verification"], {"check_id", "acceptance_ids"}, "plan/verification", key="check_id"
    )
    if set(verification) - checks:
        _reject("unknown_check", "plan/verification", "Verification contains an unknown check ID.")
    if set(verification) != checks:
        _reject("check_coverage", "plan/verification", "Every mandatory check must be referenced exactly once.")
    verified: set[str] = set()
    for identity, item in verification.items():
        verified.update(
            _references(item["acceptance_ids"], set(acceptance), f"plan/verification/{identity}/acceptance_ids")
        )
    if verified != set(acceptance):
        _reject("acceptance_coverage", "plan/verification", "Every acceptance record must have verification.")


def check_plan_completion(
    contract_bytes: bytes,
    response_bytes: bytes,
    *,
    expected_contract_sha256: str,
) -> dict[str, Any]:
    """Return a deterministic structural report, without I/O, execution, or mutation.

    Hash pinning is caller supplied. This does not establish source provenance,
    instruction compliance, meaningful criteria, or semantic correctness.
    """
    report = _report(contract_bytes, response_bytes)
    try:
        if type(expected_contract_sha256) is not str or _SHA256.fullmatch(expected_contract_sha256) is None:
            _reject("expected_hash_invalid", "contract", "Caller must supply a lowercase SHA-256 digest.")
        if type(contract_bytes) is not bytes:
            _reject("expected_bytes", "contract", "The API requires raw bytes.")
        if len(contract_bytes) > MAX_CONTRACT_BYTES:
            _reject("byte_limit", "contract", "Input exceeds the fixed byte limit.")
        if report["contract_sha256"] != expected_contract_sha256:
            _reject("contract_hash_mismatch", "contract", "Raw contract bytes do not match the caller's pin.")
        contract = _contract(_load(contract_bytes, "contract", MAX_CONTRACT_BYTES))
    except _Rejected as error:
        return _failure(report, "invalid_contract", error)
    try:
        envelope = _load(response_bytes, "response", MAX_RESPONSE_BYTES)
        if type(envelope) is not dict:
            _reject("expected_object", "response", "Expected a final Pi assistant message object.")
    except _Rejected as error:
        return _failure(report, "invalid_input", error)
    try:
        if envelope.get("role") != "assistant":
            _reject("response_role", "response/role", "Only the final assistant message can complete a plan.")
        if envelope.get("stopReason") != "stop":
            _reject("nonterminal_response", "response/stopReason", "Completion requires stopReason exactly stop.")
    except _Rejected as error:
        return _failure(report, "incomplete", error)
    try:
        content = _array(envelope.get("content"), "response/content")
        texts: list[str] = []
        for i, block in enumerate(content):
            path = f"response/content/{i}"
            if type(block) is not dict:
                _reject("expected_object", path, "Expected a text content block.")
            if block.get("type") == "thinking":
                if type(block.get("thinking")) is not str:
                    _reject("expected_string", f"{path}/thinking", "Thinking content requires a string.")
                continue
            if block.get("type") != "text":
                return _failure(
                    report, "incomplete", _Rejected("non_text_content", path, "Only text and known thinking blocks are allowed.")
                )
            if type(block.get("text")) is not str:
                _reject("expected_string", f"{path}/text", "Each text block requires a string.")
            # Envelope and block metadata are ignored after generic strict JSON checks.
            texts.append(block["text"])
        if not texts:
            return _failure(
                report, "incomplete", _Rejected("no_text_content", "response/content", "A text block is required.")
            )
        plan_bytes = "".join(texts).encode("utf-8", errors="strict")
        if len(plan_bytes) > MAX_PLAN_BYTES:
            _reject("byte_limit", "plan", "Extracted plan exceeds the fixed byte limit.")
        report["plan_sha256"] = hashlib.sha256(plan_bytes).hexdigest()
        if plan_bytes.decode("utf-8").strip() == "NO_HYPOTHESIS":
            report["status"] = "no_hypothesis"
            report["diagnostics"] = [
                {"code": "no_hypothesis", "path": "plan", "message": "Planner explicitly declined a hypothesis."}
            ]
            return report
        value = _load(plan_bytes, "plan", MAX_PLAN_BYTES)
    except _Rejected as error:
        return _failure(report, "invalid_input", error)
    try:
        _plan(value, contract, expected_contract_sha256)
    except _Rejected as error:
        return _failure(report, "incomplete", error)
    report["status"] = "complete"
    report["passed"] = True
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _reject("cli_arguments", "cli", "Required arguments: --contract, --contract-sha256, --response.")


def _read(path: str, limit: int, location: str) -> bytes:
    try:
        with Path(path).open("rb") as stream:
            return stream.read(limit + 1)
    except (OSError, ValueError):
        _reject("io_error", location, "Could not read the specified input file.")


def main(argv: list[str] | None = None) -> int:
    """Read bounded files and emit one JSON report. No checks are executed."""
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--response", required=True)
    contract_bytes = None
    response_bytes = None
    try:
        args = parser.parse_args(argv)
        contract_bytes = _read(args.contract, MAX_CONTRACT_BYTES, "contract")
        response_bytes = _read(args.response, MAX_RESPONSE_BYTES, "response")
        result = check_plan_completion(
            contract_bytes, response_bytes, expected_contract_sha256=args.contract_sha256
        )
    except _Rejected as error:
        result = _failure(_report(contract_bytes, response_bytes), "invalid_input", error)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    if result["status"] == "complete":
        return 0
    if result["status"] in {"incomplete", "no_hypothesis"}:
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())