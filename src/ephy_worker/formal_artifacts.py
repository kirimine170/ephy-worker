"""Runner-owned audit binding. Model assertions never replace these checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ARTIFACTS = (
    "system_development_policy",
    "task_spec",
    "required_skill",
    "evaluation_contract",
    "environment_contract",
    "audit_contract",
    "audit_prompt",
    "audit_input_schema",
    "evidence_manifest_schema",
    "audit_result_schema",
    "preflight_result",
    "lead_plan",
    "workflow_events",
    "model_provenance",
    "candidate_patch",
    "candidate_changed_files",
    "candidate_snapshot_manifest",
    "verification_plan",
    "verification_results",
    "checker_source",
    "checker_control_results",
    "command_transcripts",
)
CONTROL_PATHS = {
    "system_development_policy": "docs/system-development-governance.md",
    "required_skill": ".agents/skills/ephy-worker-self-improvement/SKILL.md",
    "evaluation_contract": ".agents/skills/ephy-worker-self-improvement/references/eval-contract.md",
    "audit_contract": ".agents/skills/ephy-worker-self-improvement/references/audit-contract.md",
    "audit_prompt": ".pi/prompts/audit-ephy-worker.md",
    **{
        name: f".agents/skills/ephy-worker-self-improvement/references/{filename}.schema.json"
        for name, filename in (
            ("audit_input_schema", "audit-input"),
            ("evidence_manifest_schema", "evidence-manifest"),
            ("audit_result_schema", "audit-result"),
        )
    },
}
FINAL_BINDINGS = {
    "candidate_patch_sha256": "candidate_patch",
    "candidate_snapshot_manifest_sha256": "candidate_snapshot_manifest",
    "changed_files_manifest_sha256": "candidate_changed_files",
    "verification_results_sha256": "verification_results",
    "workflow_events_sha256": "workflow_events",
    "model_provenance_sha256": "model_provenance",
}
SEQUENCE = [
    "preflight",
    "gpt-oss lead plan",
    "qwen attempt loop",
    "freeze final candidate and audit bundle",
    "fresh gpt-oss audit",
    "proposal stop",
]

# One fixed ID per mandatory bullet in audit-contract.md, sections A/B/C.
AUDIT_CHECKS = {
    "integrity": (
        "I01_ENVELOPE_IDENTITY",
        "I02_ARTIFACT_INTEGRITY",
        "I03_CANDIDATE_BINDING",
        "I04_VERIFICATION_BINDING",
        "I05_FROZEN_CONTROLS",
        "I06_PROVENANCE",
        "I07_COMPLETE_FILE_COVERAGE",
    ),
    "candidate": (
        "C01_ACCEPTANCE",
        "C02_SCOPE",
        "C03_NO_UNRELATED_CHANGES",
        "C04_TEST_MEANING",
        "C05_NO_WEAKENING",
        "C06_REQUIRED_CHECKS",
        "C07_ENVIRONMENT_BINDING",
    ),
    "workflow": (
        "W01_PREFLIGHT",
        "W02_CONTEXT_ACK",
        "W03_READONLY_PLANNER",
        "W04_QWEN_IMPLEMENTATION",
        "W05_MODEL_IDENTITIES",
        "W06_INDEPENDENT_VERIFICATION",
        "W07_BOUNDED_REPAIRS",
        "W08_INFRASTRUCTURE_STOP",
        "W09_FINAL_FREEZE",
        "W10_EXACT_AUDITED_STATE",
        "W11_PROPOSAL_STOP",
    ),
}

# Minimum relevant artifact coverage for a PASS, independent of model assertions.
# FAIL/INCONCLUSIVE may cite the available subset without inventing absent evidence.
AUDIT_EVIDENCE = {
    "I01_ENVELOPE_IDENTITY": ("audit_contract", "audit_input_schema", "evidence_manifest_schema"),
    "I02_ARTIFACT_INTEGRITY": ("evidence_manifest_schema", "audit_contract"),
    "I03_CANDIDATE_BINDING": ("candidate_patch", "candidate_changed_files", "candidate_snapshot_manifest"),
    "I04_VERIFICATION_BINDING": ("verification_results", "candidate_patch", "candidate_snapshot_manifest"),
    "I05_FROZEN_CONTROLS": (*CONTROL_PATHS, "checker_source", "environment_contract"),
    "I06_PROVENANCE": ("workflow_events", "model_provenance"),
    "I07_COMPLETE_FILE_COVERAGE": ("candidate_changed_files", "candidate_snapshot_manifest"),
    "C01_ACCEPTANCE": ("task_spec", "evaluation_contract", "candidate_patch", "verification_results"),
    "C02_SCOPE": ("task_spec", "candidate_patch", "candidate_changed_files"),
    "C03_NO_UNRELATED_CHANGES": ("candidate_patch", "candidate_changed_files"),
    "C04_TEST_MEANING": ("candidate_patch", "checker_source", "verification_plan"),
    "C05_NO_WEAKENING": ("candidate_patch", "checker_source", "required_skill"),
    "C06_REQUIRED_CHECKS": ("verification_results", "checker_control_results", "command_transcripts"),
    "C07_ENVIRONMENT_BINDING": ("environment_contract", "verification_results", "command_transcripts"),
    "W01_PREFLIGHT": ("preflight_result", "workflow_events"),
    "W02_CONTEXT_ACK": ("workflow_events", "system_development_policy", "required_skill"),
    "W03_READONLY_PLANNER": ("workflow_events", "model_provenance", "lead_plan"),
    "W04_QWEN_IMPLEMENTATION": ("workflow_events", "model_provenance"),
    "W05_MODEL_IDENTITIES": ("model_provenance", "preflight_result"),
    "W06_INDEPENDENT_VERIFICATION": ("workflow_events", "verification_results", "command_transcripts"),
    "W07_BOUNDED_REPAIRS": ("task_spec", "workflow_events", "verification_results"),
    "W08_INFRASTRUCTURE_STOP": ("preflight_result", "workflow_events", "verification_results"),
    "W09_FINAL_FREEZE": ("workflow_events", "verification_results", "candidate_snapshot_manifest"),
    "W10_EXACT_AUDITED_STATE": ("candidate_patch", "candidate_snapshot_manifest", "verification_results"),
    "W11_PROPOSAL_STOP": ("workflow_events", "system_development_policy", "audit_contract"),
}


class GateFailure(RuntimeError):
    """An infrastructure/identity failure is never a candidate repair request."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_json(path: Path, value: Any, *, exclusive: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open("xb") as stream:
            stream.write(encode(value))
    else:
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with temp.open("xb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)


def read_json(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise GateFailure(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def normalized(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or "//" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise GateFailure(f"Unsafe relative path: {value!r}")
    return PurePosixPath(value).as_posix()


def safe_path(root: Path, value: str, *, missing: bool = False) -> Path:
    root = root.resolve(strict=True)
    parts = normalized(value).split("/")
    current = root
    for part in parts:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise GateFailure(f"Link/reparse point prohibited: {current}")
            if current.is_file() and info.st_nlink != 1:
                raise GateFailure(f"Hard link prohibited: {current}")
        elif not missing:
            raise GateFailure(f"Missing artifact: {current}")
    if not current.resolve().is_relative_to(root):
        raise GateFailure(f"Path escapes root: {current}")
    return current


def validate_schema(value: Any, schema: dict) -> None:
    Draft202012Validator.check_schema(schema)
    # Contract schemas are self-contained; do not resolve network references.
    for match in re.finditer(r'"\$ref"\s*:\s*"([^"]+)"', json.dumps(schema)):
        if not match[1].startswith("#/"):
            raise GateFailure("External schema reference prohibited")
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        raise GateFailure("Schema validation: " + "; ".join(error.message for error in errors[:5]))


def inspect_bundle(bundle: Path, manifest: dict, schema: dict) -> dict:
    validate_schema(manifest, schema)
    seen = set()
    entries = []
    for entry in manifest["artifacts"]:
        name = normalized(entry["path"])
        folded = name.casefold()
        if folded in seen:
            raise GateFailure("Duplicate artifact path")
        seen.add(folded)
        target = safe_path(bundle, name)
        if not target.is_file():
            raise GateFailure("Artifact is not a regular file")
        size, sha = target.stat().st_size, file_hash(target)
        if (size, sha) != (entry["size_bytes"], entry["sha256"]):
            raise GateFailure(f"Bundle artifact changed: {entry['artifact_id']}")
        entries.append({"artifact_id": entry["artifact_id"], "path": name, "size_bytes": size, "sha256": sha})
    return {
        "manifest_schema_valid": True,
        "path_safety_valid": True,
        "all_artifacts_match": True,
        "recomputed_artifacts": entries,
    }


def freeze_bundle(bundle: Path, job: dict, artifacts: dict[str, bytes]) -> dict:
    if set(artifacts) != set(ARTIFACTS):
        raise GateFailure("Incomplete canonical audit artifact set")
    verification = json.loads(artifacts["verification_results"])
    observed_verifier = verification.get("verifier_identity")
    if not observed_verifier or observed_verifier != job["verifier_identity"]:
        raise GateFailure("Frozen verifier identity has no matching observed independent verification")
    bundle.mkdir()  # never reuse an old bundle
    entries = []
    for name in ARTIFACTS:
        suffix = ".json" if name.endswith("schema") else ".txt"
        filename = name + suffix
        (bundle / filename).write_bytes(artifacts[name])
        entries.append(
            {
                "artifact_id": name,
                "path": filename,
                "size_bytes": len(artifacts[name]),
                "media_type": "application/json" if suffix == ".json" else "text/plain",
                "producer": "runner" if name not in ("lead_plan",) else "gpt-oss planner",
                "sha256": digest(artifacts[name]),
            }
        )
    manifest = {
        "schema_version": "ephy.evidence-manifest.v1",
        "audit_id": job["id"] + "-audit",
        "job_id": job["id"],
        "created_at": now(),
        "artifacts": entries,
    }
    manifest_schema = json.loads(artifacts["evidence_manifest_schema"])
    attestation = inspect_bundle(bundle, manifest, manifest_schema)
    write_json(bundle / "evidence-manifest.json", manifest)
    attestation["manifest_sha256"] = file_hash(bundle / "evidence-manifest.json")
    write_json(bundle / "bundle-integrity.json", attestation)
    hashes = {entry["artifact_id"]: entry["sha256"] for entry in entries}
    audit_input = {
        "schema_version": "ephy.audit-input.v1",
        "audit_id": manifest["audit_id"],
        "job_id": job["id"],
        "created_at": now(),
        "proposal_only": True,
        "baseline_commit": job["baseRevision"],
        "evidence_manifest_path": "evidence-manifest.json",
        "evidence_manifest_sha256": attestation["manifest_sha256"],
        "bundle_integrity_attestation_path": "bundle-integrity.json",
        "bundle_integrity_attestation_sha256": file_hash(bundle / "bundle-integrity.json"),
        "control_document_hashes": {name: hashes[name] for name in (*CONTROL_PATHS, "environment_contract")},
        "final_bindings": {key: hashes[name] for key, name in FINAL_BINDINGS.items()},
        "allowed_file_scope": job["contract"]["allowed_files"],
        "semantic_scope": job["contract"]["semantic_scope"],
        "forbidden_actions": ["apply", "commit", "push", "merge", "network", "shell"],
        "required_checks": [check["id"] for check in job["contract"]["checks"]] + ["scope", "diff"],
        "stage_contract": {
            "required_sequence": SEQUENCE,
            "max_repair_attempts": job["contract"]["max_repairs"],
            "expected_models": job["model_identities"],
            "verifier_identity": observed_verifier,
        },
    }
    validate_schema(audit_input, json.loads(artifacts["audit_input_schema"]))
    write_json(bundle / "audit-input.json", audit_input)
    return audit_input


def validate_audit_result(
    result: dict, bundle: Path, audit_input: dict, observed_artifacts: set[str]
) -> None:
    manifest = read_json(bundle / "evidence-manifest.json")
    entries = {entry["artifact_id"]: entry for entry in manifest["artifacts"]}
    schema_path = safe_path(bundle, entries["audit_result_schema"]["path"])
    validate_schema(result, read_json(schema_path))
    if result["job_id"] != audit_input["job_id"] or result["audit_id"] != audit_input["audit_id"]:
        raise GateFailure("Audit result belongs to another job")
    expected = {
        "audit_input_sha256": file_hash(bundle / "audit-input.json"),
        "evidence_manifest_sha256": file_hash(bundle / "evidence-manifest.json"),
        "prompt_template_sha256": entries["audit_prompt"]["sha256"],
        **{
            key: audit_input["final_bindings"][key]
            for key in FINAL_BINDINGS
            if key != "changed_files_manifest_sha256"
        },
    }
    if result["bound_inputs"] != expected:
        raise GateFailure("Audit result binding mismatch")
    for document in result["documents_read"]:
        if document["sha256"] != entries[document["artifact_id"]]["sha256"]:
            raise GateFailure("Document identity mismatch")
        if document["artifact_id"] not in observed_artifacts:
            raise GateFailure("Claimed audit document has no observed delivery")
    for domain in ("integrity", "candidate", "workflow"):
        # No empty-domain or invented citations may authorize review_ready.
        ids = [check["check_id"] for check in result[domain]["checks"]]
        if len(ids) != len(set(ids)) or set(ids) != set(AUDIT_CHECKS[domain]):
            raise GateFailure("Missing, duplicate or invented mandatory audit check: " + domain)
        for check in result[domain]["checks"]:
            for ref in check["evidence"]:
                if (
                    ref["artifact_id"] not in entries
                    or ref["sha256"] != entries[ref["artifact_id"]]["sha256"]
                ):
                    raise GateFailure("Audit evidence citation mismatch")
                if ref["artifact_id"] not in observed_artifacts:
                    raise GateFailure("Audit citation has no observed delivery")
            cited = {ref["artifact_id"] for ref in check["evidence"]}
            if check["status"] == "PASS" and not set(AUDIT_EVIDENCE[check["check_id"]]) <= cited:
                raise GateFailure("Missing relevant audit evidence coverage: " + check["check_id"])
    for finding in result["findings"]:
        for ref in finding["evidence"]:
            if ref["artifact_id"] not in entries or ref["sha256"] != entries[ref["artifact_id"]]["sha256"]:
                raise GateFailure("Audit finding citation mismatch")
            if ref["artifact_id"] not in observed_artifacts:
                raise GateFailure("Audit finding has no observed delivery")
