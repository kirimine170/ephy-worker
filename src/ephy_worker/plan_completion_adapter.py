"""Read-only binding of saved planner-only artifacts to the standalone checker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from . import plan_completion as core

RESULT_SCHEMA = "ephy.planner-completion-evidence.v1"
MAX_JOB_BYTES = 1024 * 1024
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_SESSION_LINE_BYTES = 2 * 1024 * 1024
MAX_SESSION_LINES = 10000
MAX_STOP_BYTES = 64 * 1024
MAX_LEAD_PLAN_BYTES = 2 * core.MAX_PLAN_BYTES
SOURCE_LIMITS = {
    "completion-contract": core.MAX_CONTRACT_BYTES,
    "job.json": MAX_JOB_BYTES,
    "planner-session.jsonl": MAX_SESSION_BYTES,
    "lead-plan.txt": MAX_LEAD_PLAN_BYTES,
    "planner-stop.json": MAX_STOP_BYTES,
}
STOP_FIELDS = {
    "job_id", "contract_sha256", "plan_sha256", "snapshot_sha256",
    "implementation_started", "formal_audit_executed", "automatic_adoption",
}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_contract(value: dict[str, Any]) -> bytes:
    # Match formal_artifacts.encode without importing or executing the runtime.
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _message_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _report(inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    inputs = inputs or {}
    return {
        "schema": RESULT_SCHEMA,
        "status": "invalid_input",
        "completed": False,
        "source_bindings_valid": False,
        "assurance": "structural_only",
        "semantic_correctness": "not_assessed",
        "workflow_validity": "not_assessed",
        "provenance": "not_assessed",
        "formal_adoption": "not_assessed",
        "checks_executed": False,
        "job_id": None,
        "job_contract_sha256": None,
        "snapshot_sha256": None,
        "source_files": {
            name: {
                "sha256": core._hash(inputs.get(name), limit),
                "bytes": len(inputs[name]) if type(inputs.get(name)) is bytes else None,
            }
            for name, limit in SOURCE_LIMITS.items()
        },
        "terminal_record_sha256": None,
        "terminal_line": None,
        "checker_input_sha256": None,
        "original_text_sha256": None,
        "lead_plan_representation": None,
        "checker": None,
        "diagnostics": [],
    }


def _failure(report: dict[str, Any], status: str, error: core._Rejected) -> dict[str, Any]:
    report["status"] = status
    report["completed"] = False
    report["diagnostics"] = [error.diagnostic]
    return report


def _pin(value: Any, path: str) -> str:
    if type(value) is not str or core._SHA256.fullmatch(value) is None:
        core._reject("invalid_hash_pin", path, "Expected a caller-supplied lowercase SHA-256 digest.")
    return value


def _bounded(raw: Any, path: str, limit: int) -> bytes:
    if type(raw) is not bytes:
        core._reject("expected_bytes", path, "The API requires raw bytes.")
    if len(raw) > limit:
        core._reject("byte_limit", path, "Input exceeds the fixed byte limit.")
    return raw


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        core._reject("expected_object", path, "Expected an object.")
    return value


def _job_identity(value: Any, path: str) -> str:
    value = core._string(value, path, 256)
    if value != value.strip() or any(char in value for char in "\t\r\n"):
        core._reject("invalid_job_id", path, "Job identity must be a nonblank single-line exact string.")
    return value


def _projection(contract: dict[str, Any], runtime: dict[str, Any]) -> None:
    files = core._paths(runtime.get("allowed_files"), "job/contract/allowed_files")
    if files != set(contract["allowed_files"]):
        core._reject("file_scope_mismatch", "job/contract/allowed_files", "Frozen file scopes differ.")
    scope = core._string(runtime.get("semantic_scope"), "job/contract/semantic_scope")
    if scope != contract["semantic_scope"]:
        core._reject("semantic_scope_mismatch", "job/contract/semantic_scope", "Frozen semantic scopes differ.")
    checks: dict[str, list[str]] = {}
    for i, value in enumerate(core._array(runtime.get("checks"), "job/contract/checks")):
        path = f"job/contract/checks/{i}"
        value = _mapping(value, path)
        identity = core._identifier(value.get("id"), path + "/id")
        if identity in checks:
            core._reject("duplicate_id", path, "Runtime check IDs must be unique.")
        arguments = core._array(value.get("argv"), path + "/argv", core.MAX_ARGV_ITEMS)
        for j, argument in enumerate(arguments):
            core._string(argument, f"{path}/argv/{j}", core.MAX_ARGUMENT_CHARS)
        checks[identity] = arguments
    if checks != {value["id"]: value["argv"] for value in contract["checks"]}:
        core._reject("checks_mismatch", "job/contract/checks", "Check IDs and exact argv token arrays differ.")


def _terminal(session_bytes: bytes) -> tuple[dict[str, Any], bytes, int]:
    raw = _bounded(session_bytes, "planner-session.jsonl", MAX_SESSION_BYTES)
    lines = raw.split(b"\n")
    # A final delimiter does not add another physical record.
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) > MAX_SESSION_LINES:
        core._reject("session_line_limit", "planner-session.jsonl", "Too many physical JSONL lines.")
    terminal = None
    for ordinal, line in enumerate(lines, start=1):
        path = f"planner-session.jsonl/{ordinal}"
        _bounded(line, path, MAX_SESSION_LINE_BYTES)
        if not line.strip(b" \t\r"):
            continue
        value = _mapping(core._load(line, path, MAX_SESSION_LINE_BYTES), path)
        terminal = (value, line, ordinal)
    if terminal is None:
        core._reject("missing_terminal_record", "planner-session.jsonl", "No nonblank JSONL record was supplied.")
    record, raw_record, ordinal = terminal
    if record.get("type") != "message":
        core._reject("terminal_record_type", "planner-session.jsonl", "Last nonblank record must be a message.")
    message = _mapping(record.get("message"), f"planner-session.jsonl/{ordinal}/message")
    if message.get("role") != "assistant":
        core._reject("terminal_role", "planner-session.jsonl", "Last nonblank record must contain an assistant.")
    return message, raw_record, ordinal


def _original_text(message: dict[str, Any]) -> str:
    texts = []
    for i, block in enumerate(core._array(message.get("content"), "message/content")):
        block = _mapping(block, f"message/content/{i}")
        if block.get("type") == "text":
            if type(block.get("text")) is not str:
                core._reject("expected_string", f"message/content/{i}/text", "Text must be a string.")
            texts.append(block["text"])
    # Only reproduce the runtime's text concatenation here. The whole unfiltered
    # message, including every non-text block, is independently checked by core.
    text = "".join(texts)
    if not text.strip():
        core._reject("missing_plan_text", "message/content", "Saved planner-only output needs nonblank text.")
    if len(text.encode("utf-8")) > core.MAX_PLAN_BYTES:
        core._reject("byte_limit", "message/content", "Original plan text exceeds the fixed byte limit.")
    return text


def check_planner_only_output(
    contract_bytes: bytes,
    job_bytes: bytes,
    session_bytes: bytes,
    plan_bytes: bytes,
    stop_bytes: bytes,
    *,
    expected_contract_sha256: str,
    expected_job_id: str,
    expected_job_contract_sha256: str,
) -> dict[str, Any]:
    """Check local byte bindings and structure, with no I/O or provenance claim."""
    inputs = dict(zip(SOURCE_LIMITS, (contract_bytes, job_bytes, session_bytes, plan_bytes, stop_bytes), strict=True))
    report = _report(inputs)
    try:
        _pin(expected_contract_sha256, "expected_contract_sha256")
        _bounded(contract_bytes, "completion-contract", core.MAX_CONTRACT_BYTES)
        if _digest(contract_bytes) != expected_contract_sha256:
            core._reject("contract_hash_mismatch", "completion-contract", "Contract bytes differ from caller pin.")
        contract = core._contract(core._load(contract_bytes, "completion-contract", core.MAX_CONTRACT_BYTES))
    except core._Rejected as error:
        return _failure(report, "invalid_contract", error)
    try:
        _job_identity(expected_job_id, "expected_job_id")
        _pin(expected_job_contract_sha256, "expected_job_contract_sha256")
    except core._Rejected as error:
        return _failure(report, "invalid_input", error)
    try:
        job = _mapping(core._load(job_bytes, "job.json", MAX_JOB_BYTES), "job.json")
        runtime = _mapping(job.get("contract"), "job/contract")
        if job.get("id") != expected_job_id:
            core._reject("job_id_mismatch", "job/id", "Job identity differs from the caller pin.")
        report["job_id"] = expected_job_id
        runtime_hash = _digest(_canonical_contract(runtime))
        report["job_contract_sha256"] = runtime_hash
        if runtime_hash != expected_job_contract_sha256:
            core._reject("job_contract_hash_mismatch", "job/contract", "Canonical runtime contract differs from pin.")
        if runtime.get("planner_only") is not True:
            core._reject("not_planner_only", "job/contract/planner_only", "Explicit Boolean true is required.")
        if type(runtime.get("max_repairs")) is not int or runtime["max_repairs"] != 0:
            core._reject("repairs_authorized", "job/contract/max_repairs", "Planner-only max_repairs must be integer zero.")
        if job.get("status") != "planner_stopped":
            core._reject("job_status", "job/status", "Job must be at the saved planner_stopped boundary.")
        _projection(contract, runtime)
        stop = core._object(core._load(stop_bytes, "planner-stop.json", MAX_STOP_BYTES), STOP_FIELDS, "planner-stop")
        if stop["job_id"] != expected_job_id:
            core._reject("stop_job_mismatch", "planner-stop/job_id", "Stop identity differs from the caller pin.")
        if stop["contract_sha256"] != expected_job_contract_sha256:
            core._reject("stop_contract_mismatch", "planner-stop/contract_sha256", "Stop contract differs from pin.")
        for field in ("implementation_started", "formal_audit_executed", "automatic_adoption"):
            if stop[field] is not False:
                core._reject("stop_flag", "planner-stop/" + field, "Control flags must be exactly Boolean false.")
        _pin(stop["plan_sha256"], "planner-stop/plan_sha256")
        report["snapshot_sha256"] = _pin(stop["snapshot_sha256"], "planner-stop/snapshot_sha256")
        message, terminal_raw, ordinal = _terminal(session_bytes)
        report["terminal_line"] = ordinal
        report["terminal_record_sha256"] = _digest(terminal_raw)
        checker_input = _message_bytes(message)
        report["checker_input_sha256"] = _digest(checker_input)
        verdict = core.check_plan_completion(
            contract_bytes, checker_input, expected_contract_sha256=expected_contract_sha256,
        )
        report["checker"] = verdict
        text = _original_text(message)
        original = text.encode("utf-8")
        report["original_text_sha256"] = _digest(original)
        if stop["plan_sha256"] != report["original_text_sha256"]:
            core._reject("stop_plan_mismatch", "planner-stop/plan_sha256", "Stop hash differs from original text.")
        _bounded(plan_bytes, "lead-plan.txt", MAX_LEAD_PLAN_BYTES)
        if plan_bytes == original:
            report["lead_plan_representation"] = "exact_utf8"
        elif plan_bytes == text.replace("\n", "\r\n").encode("utf-8"):
            report["lead_plan_representation"] = "windows_write_text"
        else:
            core._reject("lead_plan_mismatch", "lead-plan.txt", "Saved plan does not match either exact writer form.")
        outcome = "no_hypothesis" if text.strip() == "NO_HYPOTHESIS" else "planned_only"
        if job.get("outcome") != outcome:
            core._reject("job_outcome", "job/outcome", "Outcome disagrees with the original text sentinel rule.")
        report["source_bindings_valid"] = True
        report["status"] = verdict["status"]
        report["completed"] = verdict["passed"] is True
        report["diagnostics"] = verdict["diagnostics"]
        return report
    except core._Rejected as error:
        return _failure(report, "invalid_source", error)


def _identity(value: os.stat_result) -> tuple[int, ...]:
    # Python 3.12 Windows lstat/fstat may expose different ctime semantics.
    # Compare ctime only within one API, while retaining it across read passes.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (*_identity(value), value.st_ctime_ns)


def _file_stat(path: Path, location: str) -> os.stat_result:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        core._reject("non_regular_source", location, "Input must be a regular non-reparse file.")
    return value


def _read_one(path: Path, limit: int, location: str) -> tuple[bytes, tuple[int, ...]]:
    try:
        before = _file_stat(path, location)
        if before.st_size > limit:
            core._reject("byte_limit", location, "Input file exceeds the fixed byte limit.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(before) != _identity(opened):
                core._reject("source_changed", location, "Input identity changed before its read.")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if _stamp(opened) != _stamp(after) or _stamp(before) != _stamp(_file_stat(path, location)):
            core._reject("source_changed", location, "Input changed during its read.")
        _bounded(raw, location, limit)
        if len(raw) != before.st_size:
            core._reject("source_changed", location, "Input length changed during its read.")
        return raw, _stamp(before)
    except (OSError, ValueError):
        core._reject("io_error", location, "Could not read the fixed input file.")


def _read_inputs(paths: dict[str, Path]) -> dict[str, bytes]:
    first = {name: _read_one(paths[name], limit, name) for name, limit in SOURCE_LIMITS.items()}
    for name, limit in SOURCE_LIMITS.items():
        second = _read_one(paths[name], limit, name)
        if first[name] != second:
            core._reject("source_changed", name, "Input differs between the two observation passes.")
    try:
        for name in SOURCE_LIMITS:
            if first[name][1] != _stamp(_file_stat(paths[name], name)):
                core._reject("source_changed", name, "Input identity changed across the observation window.")
    except (OSError, ValueError):
        core._reject("io_error", "sources", "Could not recheck the fixed input files.")
    return {name: value[0] for name, value in first.items()}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        core._reject(
            "cli_arguments", "cli",
            "Required: --job-dir, --contract, --contract-sha256, --job-id, --job-contract-sha256.",
        )


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    for flag in ("job-dir", "contract", "contract-sha256", "job-id", "job-contract-sha256"):
        parser.add_argument("--" + flag, required=True)
    try:
        args = parser.parse_args(argv)
        root = Path(args.job_dir)
        paths = {
            name: Path(args.contract) if name == "completion-contract" else root / name
            for name in SOURCE_LIMITS
        }
        inputs = _read_inputs(paths)
        result = check_planner_only_output(
            *inputs.values(), expected_contract_sha256=args.contract_sha256,
            expected_job_id=args.job_id, expected_job_contract_sha256=args.job_contract_sha256,
        )
    except core._Rejected as error:
        result = _failure(_report(), "invalid_input", error)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    if result["completed"]:
        return 0
    return 1 if result["status"] in {"incomplete", "no_hypothesis"} else 2


if __name__ == "__main__":
    sys.exit(main())