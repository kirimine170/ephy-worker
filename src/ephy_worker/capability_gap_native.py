"""Pure projection of caller-pinned P0 reports, not source execution evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

MANIFEST_SCHEMA = "ephy.capability-native-manifest.v1"
REPORT_SCHEMA = "ephy.capability-native-gap-report.v1"
RULESET = "native-p0-report-projection.v1"
MAX_MANIFEST_BYTES = 131072
MAX_REPORT_BYTES = 1048576
MAX_ARTIFACTS = 16
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_FAMILIES = {"trial_reconciliation", "plan_completion", "planner_completion"}
_CORE_STATUSES = {"complete", "incomplete", "no_hypothesis", "invalid_contract", "invalid_input"}
_DIAGNOSTIC_CODES = {
    "acceptance_coverage", "array_limit", "byte_limit", "check_coverage", "checks_mismatch",
    "cli_arguments", "contract_hash_mismatch", "duplicate_id", "duplicate_key",
    "duplicate_reference", "empty_array", "empty_string", "expected_array", "expected_bytes",
    "expected_hash_invalid", "expected_object", "expected_string", "file_scope_mismatch",
    "forbidden_character", "invalid_argument", "invalid_hash_pin", "invalid_id", "invalid_job_id",
    "invalid_json", "invalid_path", "invalid_utf8", "io_error", "job_contract_hash_mismatch",
    "job_id_mismatch", "job_outcome", "job_status", "json_depth_limit", "json_node_limit",
    "lead_plan_mismatch", "missing_field", "missing_plan_text", "missing_terminal_record",
    "no_hypothesis", "no_text_content", "non_regular_source", "non_text_content",
    "nonfinite_number", "nonterminal_response", "not_planner_only", "number_limit",
    "path_collision", "prohibition_coverage", "repairs_authorized", "requirement_coverage",
    "response_role", "schema_mismatch", "scope_escape", "semantic_scope_mismatch",
    "session_line_limit", "source_changed", "stop_contract_mismatch", "stop_flag",
    "stop_job_mismatch", "stop_plan_mismatch", "string_limit", "terminal_record_type",
    "terminal_role", "unknown_check", "unknown_field", "unknown_reference",
}
_CORE_FIELDS = {
    "schema", "status", "passed", "assurance", "semantic_correctness", "checks_executed",
    "contract_sha256", "response_sha256", "plan_sha256", "diagnostics",
}
_PLANNER_FIELDS = {
    "schema", "status", "completed", "source_bindings_valid", "assurance", "semantic_correctness",
    "workflow_validity", "provenance", "formal_adoption", "checks_executed", "job_id",
    "job_contract_sha256", "snapshot_sha256", "source_files", "terminal_record_sha256",
    "terminal_line", "checker_input_sha256", "original_text_sha256", "lead_plan_representation",
    "checker", "diagnostics",
}
_TRIAL_LISTS = (
    "missing_run_ids", "unexpected_run_ids", "duplicate_run_ids", "duplicate_trials", "mismatches",
    "missing_execution_record_ids", "unexpected_execution_record_ids", "not_executed_run_ids",
    "missing_raw_result_ids", "unexpected_raw_result_ids", "duplicate_raw_result_ids",
    "evidence_mismatches",
)
_TRIAL_FIELDS = set(_TRIAL_LISTS) | {
    "schema_version", "ok", "plan_id", "plan_sha256", "execution_ledger_sha256", "axis",
    "evidence_kind", "expected_runs", "supplied_runs", "validation_unexecuted_run_ids",
    "record_integrity_verified", "execution_verified", "comparison", "coverage", "interpretation",
}
_SOURCE_LIMITS = {
    "completion-contract": 262144, "job.json": 1048576, "planner-session.jsonl": 16777216,
    "lead-plan.txt": 524288, "planner-stop.json": 65536,
}
_TRIAL_INTERPRETATION = (
    "Integrity relative to trusted plan/recorder commitments only; not proof of chronology, execution, "
    "budget enforcement, model improvement, formal workflow or adoption."
)
_COVERAGE = {
    True: "exact records/raw results match to committed plan and ledger",
    False: "incomplete or mismatched evidence",
}


class NativeGapInputError(ValueError):
    """Rejected input; args and code contain a stable code, never input text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _reject(code: str = "invalid_report") -> None:
    raise NativeGapInputError(code)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _ref(value: Any) -> str:
    return _digest(_canonical(value))


def _object(value: Any, fields: set[str]) -> dict:
    if type(value) is not dict or set(value) != fields:
        _reject("invalid_fields")
    return value


def _text(value: Any, *, nullable: bool = False, maximum: int = 8192) -> Any:
    if value is None and nullable:
        return value
    if type(value) is not str or not value.strip() or len(value) > maximum:
        _reject("invalid_string")
    return value


def _hash(value: Any, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return value
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _reject("invalid_hash")
    return value


def _integer(value: Any, *, minimum: int = 0, maximum: int | None = None,
             nullable: bool = False) -> Any:
    if value is None and nullable:
        return value
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _reject("invalid_integer")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        _reject("invalid_boolean")
    return value


def _list(value: Any) -> list:
    if type(value) is not list:
        _reject("invalid_array")
    return value


def _enum(value: Any, choices: set[str]) -> str:
    if type(value) is not str or value not in choices:
        _reject("unsupported_value")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _reject("duplicate_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    _reject("nonfinite_number")


def _load(raw: Any, limit: int) -> Any:
    if type(raw) is not bytes:
        _reject("expected_bytes")
    if len(raw) > limit:
        _reject("byte_limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except NativeGapInputError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise NativeGapInputError("invalid_json") from None
    stack = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 32768:
            _reject("json_node_limit")
        if depth > 32:
            _reject("json_depth_limit")
        if type(item) is dict:
            stack.extend((key, depth + 1) for key in item)
            stack.extend((entry, depth + 1) for entry in item.values())
        elif type(item) is list:
            stack.extend((entry, depth + 1) for entry in item)
        elif type(item) is str:
            if len(item) > 8192:
                _reject("string_limit")
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise NativeGapInputError("invalid_string") from None
        elif type(item) is float and not math.isfinite(item):
            _reject("nonfinite_number")
    return value


def _diagnostics(value: Any, *, success: bool) -> None:
    values = _list(value)
    if len(values) != (0 if success else 1):
        _reject("contradictory_report")
    for diagnostic in values:
        _object(diagnostic, {"code", "path", "message"})
        _enum(diagnostic["code"], _DIAGNOSTIC_CODES)
        if type(diagnostic["path"]) is not str or type(diagnostic["message"]) is not str:
            _reject("invalid_string")


def _core(value: Any) -> dict:
    value = _object(value, _CORE_FIELDS)
    if value["schema"] != "ephy.plan-completion-result.v1":
        _reject("unsupported_schema")
    _enum(value["status"], _CORE_STATUSES)
    if _boolean(value["passed"]) != (value["status"] == "complete"):
        _reject("contradictory_report")
    if (value["assurance"] != "structural_only"
            or value["semantic_correctness"] != "not_assessed"
            or _boolean(value["checks_executed"])):
        _reject("unsupported_assurance")
    for name in ("contract_sha256", "response_sha256", "plan_sha256"):
        _hash(value[name], nullable=True)
    _diagnostics(value["diagnostics"], success=value["passed"])
    return value


def _planner(value: Any) -> dict:
    value = _object(value, _PLANNER_FIELDS)
    if value["schema"] != "ephy.planner-completion-evidence.v1":
        _reject("unsupported_schema")
    status = _enum(value["status"], _CORE_STATUSES | {"invalid_source"})
    complete, bound = _boolean(value["completed"]), _boolean(value["source_bindings_valid"])
    if complete != (status == "complete"):
        _reject("contradictory_report")
    if (value["assurance"] != "structural_only" or _boolean(value["checks_executed"])
            or any(value[key] != "not_assessed" for key in
                   ("semantic_correctness", "workflow_validity", "provenance", "formal_adoption"))):
        _reject("unsupported_assurance")
    job = _text(value["job_id"], nullable=True, maximum=256)
    if job is not None and (job != job.strip() or any(char in job for char in "\r\n\t")):
        _reject("invalid_string")
    for name in ("job_contract_sha256", "snapshot_sha256", "terminal_record_sha256",
                 "checker_input_sha256", "original_text_sha256"):
        _hash(value[name], nullable=True)
    _integer(value["terminal_line"], minimum=1, maximum=10000, nullable=True)
    representation = value["lead_plan_representation"]
    if representation is not None:
        _enum(representation, {"exact_utf8", "windows_write_text"})
    sources = _object(value["source_files"], set(_SOURCE_LIMITS))
    for name, limit in _SOURCE_LIMITS.items():
        source = _object(sources[name], {"sha256", "bytes"})
        _hash(source["sha256"], nullable=True)
        count = _integer(source["bytes"], nullable=True)
        if source["sha256"] is not None and (count is None or count > limit):
            _reject("contradictory_report")
    checker = None if value["checker"] is None else _core(value["checker"])
    _diagnostics(value["diagnostics"], success=complete)
    if status == "invalid_source":
        if bound or complete:
            _reject("contradictory_report")
    elif status == "invalid_contract":
        if bound or complete or checker is not None:
            _reject("contradictory_report")
    elif status == "invalid_input" and not bound:
        if checker is not None:
            _reject("contradictory_report")
    elif not bound:
        _reject("contradictory_report")
    if bound:
        required = ("job_id", "job_contract_sha256", "snapshot_sha256", "terminal_record_sha256",
                    "terminal_line", "checker_input_sha256", "original_text_sha256",
                    "lead_plan_representation")
        if checker is None or any(value[key] is None for key in required):
            _reject("contradictory_report")
        if any(source["sha256"] is None or source["bytes"] is None for source in sources.values()):
            _reject("contradictory_report")
        if checker["status"] != status or checker["diagnostics"] != value["diagnostics"]:
            _reject("contradictory_report")
    if checker is not None:
        for core_name, outer_name in (("response_sha256", "checker_input_sha256"),
                                     ("plan_sha256", "original_text_sha256")):
            # Earlier failures can legitimately leave original text unavailable.
            if (checker[core_name] is not None
                    and checker[core_name] != value[outer_name]):
                _reject("contradictory_report")
    if (representation == "exact_utf8" and sources["lead-plan.txt"]["sha256"]
            != value["original_text_sha256"]):
        _reject("contradictory_report")
    return value


def _trial(value: Any, expected: dict) -> dict:
    value = _object(value, _TRIAL_FIELDS)
    if _integer(value["schema_version"]) != 1:
        _reject("unsupported_schema")
    for field in ("ok", "record_integrity_verified", "execution_verified"):
        _boolean(value[field])
    if value["execution_verified"]:
        _reject("unsupported_assurance")
    _text(value["plan_id"], maximum=128)
    for field in ("plan_sha256", "execution_ledger_sha256"):
        _hash(value[field])
    _enum(value["axis"], {"model", "variant"})
    _enum(value["evidence_kind"], {"synthetic", "mock", "live"})
    if any(value[field] != expected[field] for field in expected):
        _reject("binding_mismatch")
    _integer(value["expected_runs"], minimum=1, maximum=10000)
    _integer(value["supplied_runs"], maximum=10000)
    for name in (*_TRIAL_LISTS, "validation_unexecuted_run_ids"):
        for item in _list(value[name]):
            if name == "duplicate_trials":
                _object(item, {"group", "task_id", "repeat_index", "count"})
                _text(item["group"])
                _text(item["task_id"])
                _integer(item["repeat_index"], minimum=1, maximum=100)
                _integer(item["count"], minimum=2)
            elif name in {"mismatches", "evidence_mismatches"}:
                _object(item, {"run_id", "fields"})
                _text(item["run_id"])
                if not _list(item["fields"]):
                    _reject("invalid_array")
                for field in item["fields"]:
                    _text(field)
            else:
                _text(item)
    ok = not any(value[name] for name in _TRIAL_LISTS)
    if value["ok"] != ok or value["record_integrity_verified"] != ok:
        _reject("contradictory_report")
    if value["coverage"] != _COVERAGE[ok] or value["interpretation"] != _TRIAL_INTERPRETATION:
        _reject("unsupported_value")
    if not ok:
        if value["comparison"] is not None:
            _reject("contradictory_report")
    else:
        if value["expected_runs"] != value["supplied_runs"]:
            _reject("contradictory_report")
        comparison = _object(value["comparison"], {
    "schema_version", "axis", "evidence_kind", "coverage", "fixed_conditions", "summaries",
    "run_ids", "interpretation",
})
        if (_integer(comparison["schema_version"]) != 1
                or comparison["axis"] != value["axis"]
                or comparison["evidence_kind"] != value["evidence_kind"]):
            _reject("contradictory_report")
        if type(comparison["fixed_conditions"]) is not dict:
            _reject("invalid_fields")
        _list(comparison["summaries"])
        _list(comparison["run_ids"])
        _text(comparison["coverage"])
        _text(comparison["interpretation"])
    return value


def _expected(entry: dict) -> dict:
    family = entry["source_family"]
    fields = {
        "trial_reconciliation": {"plan_id", "plan_sha256", "execution_ledger_sha256", "axis",
                                 "evidence_kind"},
        "plan_completion": {"contract_sha256"},
        "planner_completion": {"job_id", "job_contract_sha256", "contract_sha256"},
    }[family]
    expected = _object(entry["expected"], fields)
    for name, value in expected.items():
        if name.endswith("sha256"):
            _hash(value)
        elif name == "axis":
            _enum(value, {"model", "variant"})
        elif name == "evidence_kind":
            _enum(value, {"synthetic", "mock", "live"})
        else:
            _text(value, maximum=128 if name == "plan_id" else 256)
            if name == "job_id" and (value != value.strip() or any(c in value for c in "\r\n\t")):
                _reject("invalid_string")
    return expected


def _binding(expected: Any, observed: Any, *, private: bool = False) -> dict:
    return {
        "intended": _ref(expected) if private else expected,
        "observed": _ref(observed) if private and observed is not None else observed,
        "comparison": "unavailable" if observed is None else (
            "match" if expected == observed else "mismatch"),
    }


def _evidence(entry: dict, report: dict) -> dict:
    family, expected = entry["source_family"], entry["expected"]
    evidence = {
        "evidence_id": "e-" + _ref(entry["artifact_id"]),
        "artifact_id_sha256": _ref(entry["artifact_id"]),
        "context_id_sha256": _ref(entry["context_id"]),
        "source_family": family, "source_origin": entry["source_origin"],
        "native_schema": report.get("schema", "trial_reconciliation.schema_version.1"),
        "raw_report_sha256": entry["sha256"], "raw_report_byte_length": entry["byte_length"],
        "evidence_kind": report.get("evidence_kind", "not_reported"),
        "bindings": {},
    }
    if family == "trial_reconciliation":
        for name in expected:
            output_name = name + "_sha256" if name == "plan_id" else name
            evidence["bindings"][output_name] = _binding(expected[name], report[name],
                                                         private=name == "plan_id")
        evidence["reported_commitments"] = {
            "canonical_plan_sha256": report["plan_sha256"],
            "canonical_execution_ledger_sha256": report["execution_ledger_sha256"],
            "comparison_available": report["comparison"] is not None,
            "canonical_comparison_subobject_sha256": (
                _ref(report["comparison"]) if report["comparison"] is not None else None),
        }
    elif family == "plan_completion":
        evidence["bindings"]["raw_completion_contract_sha256"] = _binding(
            expected["contract_sha256"], report["contract_sha256"])
        evidence["reported_commitments"] = {
            "raw_completion_contract_sha256": report["contract_sha256"],
            "raw_response_sha256": report["response_sha256"],
            "raw_plan_text_sha256": report["plan_sha256"],
        }
    else:
        evidence["bindings"] = {
            "job_id_sha256": _binding(expected["job_id"], report["job_id"], private=True),
            "canonical_runtime_contract_sha256": _binding(
                expected["job_contract_sha256"], report["job_contract_sha256"]),
            "raw_completion_contract_sha256": _binding(
                expected["contract_sha256"], report["source_files"]["completion-contract"]["sha256"]),
        }
        evidence["reported_commitments"] = {
            "canonical_runtime_contract_sha256": report["job_contract_sha256"],
            "snapshot_sha256": report["snapshot_sha256"],
            "raw_source_files": {name: dict(value) for name, value in report["source_files"].items()},
            "raw_terminal_record_sha256": report["terminal_record_sha256"],
            "terminal_line": report["terminal_line"],
            "reserialized_checker_input_sha256": report["checker_input_sha256"],
            "raw_original_text_sha256": report["original_text_sha256"],
            "lead_plan_representation": report["lead_plan_representation"],
            "nested_core": None if report["checker"] is None else {
                name: report["checker"][name] for name in
                ("status", "passed", "contract_sha256", "response_sha256", "plan_sha256")
            },
        }
    return evidence


def _observation(evidence: dict, key: str, *, category: str, code: str, stage: str,
                 status: str, diagnostics: list | None = None, details: Any = None) -> dict:
    return {
        "observation_id": "", "evidence_id": evidence["evidence_id"], "check_key": key,
        "category": category, "code": code, "stage": stage, "status": status,
        "observation_basis": "reported", "diagnostic_codes": [
            item["code"] for item in diagnostics or []],
        "detail_refs": [] if details is None else [_ref(details)],
    }


def _project(entry: dict, report: dict, evidence: dict) -> tuple[list, list]:
    observations, unknowns = [], []
    family = entry["source_family"]
    identity = entry["expected"]
    logical = (["trial_accounting", identity] if family == "trial_reconciliation"
               else ["planning_contract", identity["contract_sha256"]])
    key = "k-" + _ref([entry["context_id"], logical])

    def add(category: str, code: str, stage: str, status: str,
            diagnostics: list | None = None, details: Any = None) -> None:
        observation = _observation(evidence, key, category=category, code=code, stage=stage,
                                   status=status, diagnostics=diagnostics, details=details)
        observation["observation_id"] = "o-" + _ref(
            [evidence["evidence_id"], len(observations), observation])
        observations.append(observation)
        if status == "unknown":
            unknowns.append({"code": code, "observation_ids": [observation["observation_id"]],
                             "evidence_ids": [evidence["evidence_id"]],
                             "detail_refs": observation["detail_refs"]})

    if family == "trial_reconciliation":
        add("evidence", "trial_accounting", "trial_reconciliation",
            "passed" if report["ok"] else "failed")
        for name in (*_TRIAL_LISTS, "validation_unexecuted_run_ids"):
            for row in report[name]:
                unknown = name in {"not_executed_run_ids", "validation_unexecuted_run_ids"}
                add("evidence", name, "trial_reconciliation", "unknown" if unknown else "failed",
                    details=row)
        add("evidence", "execution_not_assessed", "trial_reconciliation", "unknown")
    else:
        status = report["status"]
        bound = (report["source_bindings_valid"] if family == "planner_completion"
                 else status in {"complete", "incomplete", "no_hypothesis"})
        if family == "planner_completion":
            add("evidence", "source_bindings", "planner_completion",
                "passed" if bound else "failed", report["diagnostics"])
        if not bound or status in {"invalid_input", "invalid_contract"}:
            add("evidence", status, family, "failed", report["diagnostics"])
            add("workflow", "planning_structure_unknown", "plan_completion", "unknown")
        elif status == "no_hypothesis":
            add("workflow", "no_hypothesis", "plan_completion", "unknown", report["diagnostics"])
        else:
            add("workflow", "planning_contract", "plan_completion",
                "passed" if status == "complete" else "failed", report["diagnostics"])
    for name, binding in evidence["bindings"].items():
        if binding["comparison"] != "match":
            unknowns.append({"code": "binding_" + binding["comparison"],
                             "observation_ids": [], "evidence_ids": [evidence["evidence_id"]],
                             "detail_refs": [_ref(name)]})
    commitments = evidence["reported_commitments"]
    for name, value in commitments.items():
        if value is None:
            unknowns.append({"code": "unavailable_commitment", "observation_ids": [],
                             "evidence_ids": [evidence["evidence_id"]], "detail_refs": [_ref(name)]})
    return observations, unknowns


def _candidates(observations: list, unknowns: list) -> tuple[list, list]:
    groups: dict[str, list] = {}
    for observation in observations:
        if observation["code"] in {"planning_contract", "trial_accounting"}:
            groups.setdefault(observation["check_key"], []).append(observation)
    candidates, hypotheses = [], []
    for key, group in groups.items():
        passed = [row for row in group if row["status"] == "passed"]
        failed = [row for row in group if row["status"] == "failed"]
        if passed and failed:
            unknowns.append({
                "code": "conflicting_observations", "check_key": key,
                "observation_ids": [row["observation_id"] for row in group],
                "evidence_ids": list(dict.fromkeys(row["evidence_id"] for row in group)),
                "detail_refs": [],
            })
            continue
        if not failed:
            continue
        area = failed[0]["code"]
        candidate_id = "g-" + _ref([key, area])
        basis = {
            "status": "unverified",
            "supporting_observation_ids": [row["observation_id"] for row in failed],
            "contradicting_observation_ids": [],
            "evidence_ids": list(dict.fromkeys(row["evidence_id"] for row in failed)),
        }
        candidates.append({"candidate_id": candidate_id, "category": failed[0]["category"],
                           "investigation_area": area, **basis})
        cause = ("planning_contract_not_satisfied" if area == "planning_contract"
                 else "trial_evidence_incomplete_or_mismatched")
        hypotheses.append({"hypothesis_id": "h-" + _ref([candidate_id, cause]),
                           "candidate_id": candidate_id, "cause_code": cause, **basis})
    return candidates, hypotheses


def derive_native_gap_candidates(
    manifest_bytes: bytes, reports_by_id: dict[str, bytes], *, expected_manifest_sha256: str,
) -> dict:
    """Validate exact caller-pinned bytes and project reported evidence only.

    The caller must hold the manifest digest independently. Neither the manifest
    nor a report proves who produced it or that its referenced sources were read.
    Invalid inputs fail atomically with NativeGapInputError; no raw input escapes.
    """
    _hash(expected_manifest_sha256)
    if type(manifest_bytes) is not bytes:
        _reject("expected_bytes")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        _reject("byte_limit")
    if _digest(manifest_bytes) != expected_manifest_sha256:
        _reject("manifest_hash_mismatch")
    manifest = _object(_load(manifest_bytes, MAX_MANIFEST_BYTES), {"schema", "artifacts"})
    if manifest["schema"] != MANIFEST_SCHEMA:
        _reject("unsupported_schema")
    entries = _list(manifest["artifacts"])
    if not 1 <= len(entries) <= MAX_ARTIFACTS:
        _reject("artifact_limit")
    if type(reports_by_id) is not dict or any(type(key) is not str for key in reports_by_id):
        _reject("invalid_reports_mapping")
    ids, digests, prepared = set(), set(), []
    for entry in entries:
        _object(entry, {"artifact_id", "context_id", "source_family", "source_origin",
                        "sha256", "byte_length", "expected"})
        for name in ("artifact_id", "context_id"):
            if type(entry[name]) is not str or _ID.fullmatch(entry[name]) is None:
                _reject("invalid_id")
        _enum(entry["source_family"], _FAMILIES)
        _enum(entry["source_origin"], {"test_fixture", "saved_artifact"})
        _hash(entry["sha256"])
        _integer(entry["byte_length"], maximum=MAX_REPORT_BYTES)
        expected = _expected(entry)
        identity, digest = entry["artifact_id"], entry["sha256"]
        if identity in ids or digest in digests:
            _reject("duplicate_artifact")
        ids.add(identity)
        digests.add(digest)
        if identity not in reports_by_id:
            _reject("artifact_set_mismatch")
        raw = reports_by_id[identity]
        if type(raw) is not bytes:
            _reject("expected_bytes")
        if len(raw) != entry["byte_length"] or _digest(raw) != digest:
            _reject("report_binding_mismatch")
        report = _load(raw, MAX_REPORT_BYTES)
        family = entry["source_family"]
        if family == "trial_reconciliation":
            report = _trial(report, expected)
        elif family == "plan_completion":
            report = _core(report)
            if (report["status"] in {"complete", "incomplete", "no_hypothesis"}
                    and report["contract_sha256"] != expected["contract_sha256"]):
                _reject("binding_mismatch")
        else:
            report = _planner(report)
            if report["source_bindings_valid"] and (
                report["job_id"] != expected["job_id"]
                or report["job_contract_sha256"] != expected["job_contract_sha256"]
                or report["source_files"]["completion-contract"]["sha256"] != expected["contract_sha256"]
                or report["checker"]["contract_sha256"] != expected["contract_sha256"]
            ):
                _reject("binding_mismatch")
        prepared.append((entry, report))
    if set(reports_by_id) != ids:
        _reject("artifact_set_mismatch")
    evidence, observations, unknowns = [], [], []
    for entry, report in prepared:
        item = _evidence(entry, report)
        evidence.append(item)
        projected, unavailable = _project(entry, report, item)
        observations.extend(projected)
        unknowns.extend(unavailable)
    candidates, causes = _candidates(observations, unknowns)
    return {
        "schema": REPORT_SCHEMA, "ruleset": RULESET, "evidence_basis": "caller_pinned_native_reports",
        "assurance": {
            "raw_report_bindings_verified": True, "source_bytes_rechecked": False,
            "producer_provenance": "not_assessed", "execution": "not_assessed",
            "semantic_correctness": "not_assessed", "real_model_improvement": "not_assessed",
            "formal_adoption": "not_assessed",
        },
        "manifest_sha256": expected_manifest_sha256, "assessment_status": "reported_evidence_only",
        "evidence": evidence, "observations": observations, "unknowns": unknowns,
        "gap_candidates": candidates, "cause_hypotheses": causes,
    }
