"""Synthetic-only capability-gap preparation; no native artifact adapters.

Contract:
* validate_artifacts takes UTF-8 JSON manifest bytes and an exact dict of artifact
  ID -> bytes. Manifest version 1 binds each ID, job/run ID, byte length, input
  schema and RAW SHA-256. It is not a runner attestation or a canonical result hash.
* Every artifact is ephy.capability-observations.v1, source_family
  synthetic_observations, evidence_kind synthetic. All fields are required;
  unknown fields, coercion, duplicate keys and nonfinite JSON are rejected.
* Observations are ordered, zero-based, contiguous sequences. Each check ID has
  one category/code/stage within a job/run. Conflicting outcomes remain visible.
* Receipts are immutable conveniences, not trusted capabilities: downstream
  entry points revalidate their bytes. Receipts retain raw inputs in memory but
  suppress their repr; keep receipts within the trusted caller. Only projected
  reports are intended for logging/model context and never copy caller IDs or
  free text.
* No BenchmarkRun, Formal, Research, P0, CLI, filesystem, model or network wiring
  is implemented. Native artifacts need separately versioned, reviewed adapters.

Hash agreement only binds supplied bytes. Candidates and causal alternatives are
always unverified; even an all-pass fixture does not establish model capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

INPUT_SCHEMA = "ephy.capability-observations.v1"
REPORT_SCHEMA = "ephy.capability-gap-report.v1"
RULESET = "synthetic-gap-rules.v1"
MAX_ARTIFACT_BYTES = 1_048_576
MAX_ARTIFACTS = 64
MAX_OBSERVATIONS = 1024

# code -> (category, stage, investigation area, possible causes)
_RULES = {
    "validation": (
        "task_performance", "validation", "task_validation",
        ("candidate_requirement_mismatch", "checker_or_fixture_mismatch"),
    ),
    "execution_timeout": (
        "workflow", "execution", "execution_budget",
        ("execution_or_budget_constraint",),
    ),
    "environment_unavailable": (
        "environment", "setup", "execution_environment",
        ("execution_environment_unavailable",),
    ),
    "evidence_missing": (
        "evidence", "verification", "evidence_collection",
        ("evidence_collection_incomplete",),
    ),
    "plan_incomplete": (
        "workflow", "planning", "planning_contract",
        ("planning_requirements_unmet",),
    ),
    "information_missing": (
        "information", "research", "available_information",
        ("available_information_incomplete",),
    ),
}


class GapInputError(ValueError):
    """Stable code only; never include raw input or validation-library messages."""


@dataclass(frozen=True)
class ValidatedInputs:
    """Revalidated on use; construction alone grants no trust."""

    manifest_bytes: bytes = field(repr=False)
    artifacts: tuple[tuple[str, bytes], ...] = field(repr=False)


def _fail(code: str):
    raise GapInputError(code) from None


def _exact_object(value, fields, code="invalid_fields"):
    if type(value) is not dict or set(value) != set(fields):
        _fail(code)


def _choice(value, choices, code):
    if type(value) is not str or value not in choices:
        _fail(code)


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is None:
        _fail("invalid_identifier")


def _json(raw, limit):
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        _fail("invalid_bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        _fail("nonfinite_json")

    # JSON 1e999 also becomes infinity unless parse_float is checked.
    def decimal(value):
        import math

        number = float(value)
        if not math.isfinite(number):
            _fail("nonfinite_json")
        return number

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant, parse_float=decimal)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, GapInputError):
            raise
        _fail("invalid_json")


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _key(*parts):
    return _digest(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii"))


def _decode_inputs(manifest_bytes, bytes_by_id):
    manifest = _json(manifest_bytes, 65_536)
    _exact_object(manifest, ("schema_version", "artifacts"), "invalid_manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        _fail("unsupported_manifest_version")
    entries = manifest["artifacts"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ARTIFACTS:
        _fail("invalid_artifact_count")
    if type(bytes_by_id) is not dict or any(type(key) is not str for key in bytes_by_id):
        _fail("invalid_artifact_map")
    ids = set()
    decoded = []
    checks = {}
    for entry in entries:
        _exact_object(entry, ("artifact_id", "job_id", "run_id", "sha256", "byte_length", "input_schema"))
        for name in ("artifact_id", "job_id", "run_id"):
            _identifier(entry[name])
        artifact_id = entry["artifact_id"]
        if artifact_id in ids:
            _fail("duplicate_artifact_id")
        ids.add(artifact_id)
        if entry["input_schema"] != INPUT_SCHEMA:
            _fail("unsupported_input_schema")
        sha = entry["sha256"]
        if type(sha) is not str or re.fullmatch("[a-f0-9]{64}", sha) is None:
            _fail("invalid_sha256")
        size = entry["byte_length"]
        if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
            _fail("invalid_byte_length")
        if artifact_id not in bytes_by_id:
            _fail("missing_artifact")
        raw = bytes_by_id[artifact_id]
        if type(raw) is not bytes or len(raw) != size:
            _fail("byte_length_mismatch")
        if _digest(raw) != sha:
            _fail("sha256_mismatch")
        payload = _json(raw, MAX_ARTIFACT_BYTES)
        _exact_object(payload, ("schema_version", "source_family", "evidence_kind",
                                "artifact_id", "job_id", "run_id", "observations"))
        if payload["schema_version"] != INPUT_SCHEMA:
            _fail("unsupported_input_schema")
        if payload["source_family"] != "synthetic_observations" or payload["evidence_kind"] != "synthetic":
            _fail("unsupported_evidence_basis")
        for name in ("artifact_id", "job_id", "run_id"):
            if type(payload[name]) is not str or payload[name] != entry[name]:
                _fail("identity_mismatch")
        observations = payload["observations"]
        if type(observations) is not list or len(observations) > MAX_OBSERVATIONS:
            _fail("invalid_observations")
        for index, obs in enumerate(observations):
            _exact_object(obs, ("sequence", "check_id", "category", "code", "stage",
                                "status", "validation_passed"))
            if type(obs["sequence"]) is not int or obs["sequence"] != index:
                _fail("invalid_sequence")
            _identifier(obs["check_id"])
            _choice(obs["code"], _RULES, "unsupported_observation_code")
            category, stage, _, _ = _RULES[obs["code"]]
            if obs["category"] != category or obs["stage"] != stage:
                _fail("observation_binding_mismatch")
            _choice(obs["status"], ("passed", "failed", "unknown"), "invalid_status")
            validation = obs["validation_passed"]
            if validation is not None and type(validation) is not bool:
                _fail("invalid_validation")
            expected = {"passed": True, "failed": False, "unknown": None}[obs["status"]]
            if (obs["code"] == "validation" and validation is not expected) or (
                obs["code"] != "validation" and validation is not None
            ):
                _fail("outcome_mismatch")
            check = (entry["job_id"], entry["run_id"], obs["check_id"])
            binding = (obs["category"], obs["code"], obs["stage"])
            if check in checks and checks[check] != binding:
                _fail("contradictory_check_binding")
            checks[check] = binding
        decoded.append((entry, payload))
    if set(bytes_by_id) != ids:
        _fail("extra_artifact")
    return decoded


def validate_artifacts(manifest_bytes: bytes, bytes_by_id: dict[str, bytes]) -> ValidatedInputs:
    """Validate all bindings atomically; no partial result on malformed input."""
    decoded = _decode_inputs(manifest_bytes, bytes_by_id)
    return ValidatedInputs(manifest_bytes, tuple(
        (entry["artifact_id"], bytes_by_id[entry["artifact_id"]]) for entry, _ in decoded
    ))


def _revalidate(inputs):
    if type(inputs) is not ValidatedInputs or type(inputs.artifacts) is not tuple:
        _fail("invalid_receipt")
    artifacts = {}
    for item in inputs.artifacts:
        if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
            _fail("invalid_receipt")
        if item[0] in artifacts:
            _fail("duplicate_artifact_id")
        artifacts[item[0]] = item[1]
    return _decode_inputs(inputs.manifest_bytes, artifacts)


def _project(decoded):
    evidence, observations, unknowns = [], [], []
    for entry, payload in decoded:
        evidence_id = "e_" + entry["sha256"]
        evidence.append({
            "evidence_id": evidence_id, "raw_sha256": entry["sha256"],
            "byte_length": entry["byte_length"], "input_schema": INPUT_SCHEMA,
            "artifact_key": _key(entry["artifact_id"]),
            "job_key": _key(entry["job_id"]), "run_key": _key(entry["job_id"], entry["run_id"]),
        })
        if not payload["observations"]:
            unknowns.append({"reason": "empty_observations", "evidence_ids": [evidence_id],
                             "observation_ids": []})
        for obs in payload["observations"]:
            observation_id = "o_" + _key(entry["sha256"], obs["sequence"])
            observations.append({
                "observation_id": observation_id, "evidence_id": evidence_id,
                "sequence": obs["sequence"],
                "check_key": _key(entry["job_id"], entry["run_id"], obs["check_id"]),
                **{name: obs[name] for name in ("category", "code", "stage", "status", "validation_passed")},
            })
            if obs["status"] == "unknown":
                unknowns.append({"reason": "status_unknown", "evidence_ids": [evidence_id],
                                 "observation_ids": [observation_id]})
    return evidence, observations, unknowns


def project_observations(inputs: ValidatedInputs) -> dict:
    """Preserve supplied order and unknowns without copying arbitrary text."""
    evidence, observations, unknowns = _project(_revalidate(inputs))
    return {"evidence_basis": "synthetic_caller_supplied", "evidence": evidence,
            "observations": observations, "unknowns": unknowns}


def derive_gap_candidates(inputs: ValidatedInputs, *, ruleset: str = RULESET) -> dict:
    """Produce investigation candidates, never a capability score or causal finding."""
    if type(ruleset) is not str or ruleset != RULESET:
        _fail("unsupported_ruleset")
    projection = project_observations(inputs)
    observations = projection["observations"]
    unknowns = projection["unknowns"]
    groups = {}
    for obs in observations:
        groups.setdefault(obs["check_key"], []).append(obs)
    candidates, hypotheses = [], []
    for check_key, group in groups.items():
        failed = [obs for obs in group if obs["status"] == "failed"]
        passed = [obs for obs in group if obs["status"] == "passed"]
        if failed and passed:
            unknowns.append({
                "reason": "conflicting_observations",
                "evidence_ids": list(dict.fromkeys(obs["evidence_id"] for obs in group)),
                "observation_ids": [obs["observation_id"] for obs in group],
            })
            continue
        if not failed:
            continue
        code = failed[0]["code"]
        category, _, area, causes = _RULES[code]
        supporting = [obs["observation_id"] for obs in failed]
        evidence_ids = list(dict.fromkeys(obs["evidence_id"] for obs in failed))
        candidate_id = "g_" + _key(RULESET, check_key, code, supporting)
        candidates.append({
            "candidate_id": candidate_id, "category": category, "investigation_area": area,
            "status": "unverified", "supporting_observation_ids": supporting,
            "contradicting_observation_ids": [], "evidence_ids": evidence_ids,
        })
        for cause in causes:
            hypotheses.append({
                "hypothesis_id": "h_" + _key(candidate_id, cause), "candidate_id": candidate_id,
                "cause_code": cause, "status": "unverified",
                "supporting_observation_ids": supporting.copy(), "contradicting_observation_ids": [],
                "evidence_ids": evidence_ids.copy(),
            })
    status = ("synthetic_candidates_only" if candidates else
              "insufficient_evidence" if unknowns else "no_rule_match_in_supplied_synthetic_input")
    return {
        "schema_version": REPORT_SCHEMA, "ruleset": RULESET,
        "evidence_basis": "synthetic_caller_supplied", "native_adapters": "not_connected",
        "assessment_status": status,
        "input_findings": [{"code": "supplied_raw_bindings_match",
                            "evidence_ids": [item["evidence_id"] for item in projection["evidence"]]}],
        "evidence": projection["evidence"], "observations": observations, "unknowns": unknowns,
        "gap_candidates": candidates, "cause_hypotheses": hypotheses,
    }
