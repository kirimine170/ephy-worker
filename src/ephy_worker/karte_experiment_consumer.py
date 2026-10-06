"""Read-only consumption of Karte's synthetic experiment producer v1.

Inputs are immutable byte snapshots supplied by the caller. This module does not
execute Karte, touch a filesystem, update a Job, or authorize adoption.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_JSON = 256 * 1024
MAX_ARTIFACT = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
PHASES = {"prepared", "pending", "report_accepted", "rejected", "conflict", "invalid"}
TERMINAL_PHASES = PHASES - {"prepared", "pending"}
WORKER_ARTIFACTS = (
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
METADATA_REF = "adapter/metadata.json"
MANIFEST_REF = "worker/evidence-manifest.json"
EXPECTED_REFS = {METADATA_REF, MANIFEST_REF, *(f"worker/artifacts/{name}" for name in WORKER_ARTIFACTS)}


class ConsumerFailure(ValueError):
    """Invalid or conflicting evidence cannot advance consumer state."""


@dataclass(frozen=True)
class ReviewTarget:
    candidate_id: str
    payload_sha256: str
    patch_sha256: str
    experiment_id: str
    run_id: str
    attempt_id: str
    target_commit: str


@dataclass(frozen=True)
class Observation:
    review_target: ReviewTarget
    producer_phase: str
    worker_result: str
    receipt_sha256: str | None
    cancelled: bool = False

    @property
    def adopted(self) -> bool:
        return False

    @property
    def review_ready(self) -> bool:
        return False


@dataclass(frozen=True)
class Consumption:
    observation: Observation
    duplicate: bool


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _object(value: Any, required: set[str], optional: set[str] | frozenset[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ConsumerFailure("Object fields do not match the supported contract")
    return value


def _decode(raw: bytes) -> dict:
    if type(raw) is not bytes or len(raw) > MAX_JSON:
        raise ConsumerFailure("JSON must be bounded immutable bytes")

    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConsumerFailure("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ConsumerFailure(f"Invalid JSON constant: {value}")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ConsumerFailure("Invalid JSON") from exc
    if not isinstance(result, dict):
        raise ConsumerFailure("JSON object required")
    return result


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ConsumerFailure("Lowercase SHA256 required")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConsumerFailure("Nonempty text required")
    return value


def _timestamp(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value
    ):
        raise ConsumerFailure("RFC3339 timestamp required")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConsumerFailure("Invalid timestamp") from exc


def _payload(raw: bytes, artifacts: Mapping[str, bytes], candidate: str, pin: str) -> ReviewTarget:
    if _digest(raw) != _sha(pin):
        raise ConsumerFailure("Payload differs from the independently retained pin")
    binding = _object(_decode(raw), {"schema_version", "record", "proposal", "entries"})
    if binding["schema_version"] != "karte.experiment-payload.v1":
        raise ConsumerFailure("Unsupported payload schema")
    record = _object(
        binding["record"],
        {
            "schema_version",
            "candidate_id",
            "experiment_id",
            "run_id",
            "attempt_id",
            "target_commit",
            "patch_sha256",
            "environment",
            "model",
            "checker",
            "observations",
            "interpretation",
            "halt_reason",
            "evidence",
            "verification",
            "state",
            "project",
            "title",
            "reported_at",
        },
    )
    proposal = _object(
        binding["proposal"],
        {
            "schema_version",
            "candidate_id",
            "operation",
            "target_doc_id",
            "target_relative_path",
            "base_sha256",
            "append_position",
            "proposed_frontmatter",
            "proposed_body",
            "placement",
            "source_refs",
            "sensitivity",
            "created_at",
        },
    )
    if (record["schema_version"], proposal["schema_version"]) != ("0.1", "1.1"):
        raise ConsumerFailure("Unsupported record/proposal schema")
    if record["candidate_id"] != candidate or proposal["candidate_id"] != candidate:
        raise ConsumerFailure("Payload candidate identity mismatch")
    if (record["state"], record["verification"], proposal["operation"], proposal["sensitivity"]) != (
        "experiment",
        "unverified",
        "create",
        "internal",
    ) or any(
        proposal[key] is not None
        for key in ("target_doc_id", "target_relative_path", "base_sha256", "append_position")
    ):
        raise ConsumerFailure("Payload exceeds the experiment transport boundary")
    placement = proposal["placement"]
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "report"
        or (placement.get("project") != record["project"])
    ):
        raise ConsumerFailure("Report placement required")
    for key, pattern in (
        ("project", r"[a-z0-9][a-z0-9._-]{0,63}"),
        ("year_month", r"[0-9]{4}-(?:0[1-9]|1[0-2])"),
        ("preferred_filename", r"[a-z0-9][a-z0-9._-]{0,127}\.md"),
    ):
        if not isinstance(placement.get(key), str) or not re.fullmatch(pattern, placement[key]):
            raise ConsumerFailure("Invalid report placement")
    entries = binding["entries"]
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_REFS) or set(artifacts) != EXPECTED_REFS:
        raise ConsumerFailure("Complete immutable artifact set required")
    hashes = {}
    total = 0
    for entry in entries:
        _object(entry, {"logical_ref", "sha256", "size_bytes"})
        ref = entry["logical_ref"]
        if not isinstance(ref, str) or ref not in EXPECTED_REFS or ref in hashes:
            raise ConsumerFailure("Invalid or duplicate evidence reference")
        data = artifacts[ref]
        limit = MAX_JSON if ref in {METADATA_REF, MANIFEST_REF} else MAX_ARTIFACT
        if type(data) is not bytes or len(data) > limit or type(entry["size_bytes"]) is not int:
            raise ConsumerFailure("Artifact must be bounded immutable bytes with an integer size")
        sha = _sha(entry["sha256"])
        if len(data) != entry["size_bytes"] or _digest(data) != sha:
            raise ConsumerFailure(f"Missing or changed artifact: {ref}")
        total += len(data)
        hashes[ref] = sha
    if total > MAX_TOTAL:
        raise ConsumerFailure("Evidence exceeds total byte limit")
    evidence = record["evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(hashes):
        raise ConsumerFailure("Record evidence is incomplete")
    observed = {}
    for entry in evidence:
        _object(entry, {"logical_ref", "sha256"})
        ref = entry["logical_ref"]
        if not isinstance(ref, str) or ref in observed:
            raise ConsumerFailure("Duplicate record evidence")
        observed[ref] = entry["sha256"]
    if observed != hashes:
        raise ConsumerFailure("Record evidence differs from payload entries")
    manifest = _object(
        _decode(artifacts[MANIFEST_REF]), {"schema_version", "audit_id", "job_id", "created_at", "artifacts"}
    )
    if manifest["schema_version"] != "ephy.evidence-manifest.v1":
        raise ConsumerFailure("Unsupported Worker manifest schema")
    _timestamp(manifest["created_at"])
    if manifest["audit_id"] != record["experiment_id"] or manifest["job_id"] != record["run_id"]:
        raise ConsumerFailure("Worker manifest identity mismatch")
    inventory = manifest["artifacts"]
    if not isinstance(inventory, list) or len(inventory) != len(WORKER_ARTIFACTS):
        raise ConsumerFailure("Incomplete Worker inventory")
    for name, entry in zip(WORKER_ARTIFACTS, inventory, strict=True):
        _object(entry, {"artifact_id", "path", "size_bytes", "media_type", "producer", "sha256"})
        ref = f"worker/artifacts/{name}"
        if (
            entry["artifact_id"] != name
            or entry["sha256"] != hashes[ref]
            or (type(entry["size_bytes"]) is not int or entry["size_bytes"] != len(artifacts[ref]))
        ):
            raise ConsumerFailure("Worker inventory binding mismatch")
    if record["patch_sha256"] != hashes["worker/artifacts/candidate_patch"]:
        raise ConsumerFailure("Candidate patch binding mismatch")
    return ReviewTarget(
        candidate,
        pin,
        record["patch_sha256"],
        *(_text(record[field]) for field in ("experiment_id", "run_id", "attempt_id", "target_commit")),
    )


def _status_observation(
    status_raw: bytes,
    payload_raw: bytes,
    target: ReviewTarget,
    worker_result: str,
    *,
    cancelled: bool = False,
) -> Observation:
    """Reconstruct receipt semantics for an already bound payload and target."""
    if type(cancelled) is not bool:
        raise ConsumerFailure("Cancellation must be explicit boolean")
    candidate_id, payload_sha256 = target.candidate_id, target.payload_sha256
    status = _object(
        _decode(status_raw),
        {"candidate_id", "phase", "payload_sha256", "state", "verification", "adopted"},
        {"receipt"},
    )
    phase = status["phase"]
    if not isinstance(phase, str) or phase not in PHASES:
        raise ConsumerFailure("Unsupported producer phase")
    if (status["candidate_id"], status["payload_sha256"], status["state"], status["verification"]) != (
        candidate_id,
        payload_sha256,
        "experiment",
        "unverified",
    ) or status["adopted"] is not False:
        raise ConsumerFailure("Status identity or authority mismatch")
    receipt_hash = None
    if phase in TERMINAL_PHASES:
        receipt = _object(
            status.get("receipt"),
            {
                "schema_version",
                "candidate_id",
                "result",
                "doc_id",
                "relative_path",
                "resulting_sha256",
                "processed_at",
                "error_code",
                "message",
            },
        )
        expected = "accepted" if phase == "report_accepted" else phase
        if (receipt["schema_version"], receipt["candidate_id"], receipt["result"]) != (
            "1.1",
            candidate_id,
            expected,
        ):
            raise ConsumerFailure("Receipt schema, candidate or result mismatch")
        _timestamp(receipt["processed_at"])
        doc_id = _digest(("karte-ephy-v1.1:" + candidate_id).encode("ascii"))
        if receipt["doc_id"] is not None and receipt["doc_id"] != doc_id:
            raise ConsumerFailure("Receipt document identity mismatch")
        if receipt["resulting_sha256"] is not None:
            _sha(receipt["resulting_sha256"])
        if receipt["relative_path"] is not None:
            # Match the original proposal placement, including Karte's collision suffix.
            placement = _decode(payload_raw)["proposal"]["placement"]
            directory = f"content/{placement['project']}/report/{placement['year_month']}/"
            filename = placement["preferred_filename"]
            allowed = {
                directory + filename,
                *(directory + filename[:-3] + "--" + doc_id[:length] + ".md" for length in range(8, 65, 4)),
            }
            if not isinstance(receipt["relative_path"], str) or receipt["relative_path"] not in allowed:
                raise ConsumerFailure("Receipt placement mismatch")
        if receipt["error_code"] is not None and (
            not isinstance(receipt["error_code"], str)
            or not receipt["error_code"].strip()
            or len(receipt["error_code"]) > 128
        ):
            raise ConsumerFailure("Invalid receipt error code")
        if receipt["message"] is not None and (
            not isinstance(receipt["message"], str) or len(receipt["message"]) > 2048
        ):
            raise ConsumerFailure("Invalid receipt message")
        if phase == "report_accepted":
            _text(receipt["doc_id"])
            _text(receipt["relative_path"])
            _sha(receipt["resulting_sha256"])
            if receipt["error_code"] is not None:
                raise ConsumerFailure("Accepted receipt contains an error")
        receipt_hash = _digest(json.dumps(receipt, sort_keys=True, ensure_ascii=True).encode("utf-8"))
    elif "receipt" in status:
        raise ConsumerFailure("Nonterminal phase cannot contain a receipt")
    return Observation(target, phase, worker_result, receipt_hash, cancelled)


def consume_result(
    status_raw: bytes,
    payload_raw: bytes,
    artifacts: Mapping[str, bytes],
    *,
    candidate_id: str,
    payload_sha256: str,
    previous: Observation | None = None,
    cancelled: bool = False,
) -> Consumption:
    """Consume prepare/publish/status snapshots using pins retained before receipt.

    Persist ``observation`` in the caller's existing Job storage for retries.
    A ReviewTarget identifies evidence for later independent review; it is never
    a review decision or permission to apply the patch.
    """
    if not isinstance(candidate_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", candidate_id
    ):
        raise ConsumerFailure("Invalid candidate ID")
    if type(cancelled) is not bool:
        raise ConsumerFailure("Cancellation must be explicit boolean")
    if not isinstance(artifacts, Mapping):
        raise ConsumerFailure("Artifact byte mapping required")
    artifacts = dict(artifacts)
    target = _payload(payload_raw, artifacts, candidate_id, payload_sha256)
    metadata = _object(
        _decode(artifacts[METADATA_REF]),
        {
            "adapter_version",
            "synthetic_only",
            "candidate_id",
            "experiment_id",
            "run_id",
            "attempt_id",
            "target_commit",
            "environment",
            "model",
            "checker",
            "worker_result",
            "observations",
            "interpretation",
            "halt_reason",
            "project",
            "title",
            "reported_at",
        },
    )
    if (
        metadata.get("adapter_version") != "karte.worker-experiment.v1"
        or metadata.get("synthetic_only") is not True
    ):
        raise ConsumerFailure("Only the synthetic Worker adapter v1 is supported")
    worker_result = metadata.get("worker_result")
    if not isinstance(worker_result, str) or worker_result not in {
        "strict_pass",
        "halted",
        "external_review_pending",
    }:
        raise ConsumerFailure("Unsupported Worker result")
    record = _decode(payload_raw)["record"]
    shared = metadata.keys() & record.keys() - {"observations"}
    if any(not isinstance(metadata[field], str) or metadata[field] != record[field] for field in shared):
        raise ConsumerFailure("Adapter metadata differs from the bound experiment record")
    observations = metadata["observations"]
    if (
        not isinstance(observations, list)
        or not observations
        or any(not isinstance(value, str) or not value.strip() for value in observations)
        or record["observations"] != ["Worker result (unverified): " + worker_result, *observations]
    ):
        raise ConsumerFailure("Experiment observations differ from the producer metadata transform")
    incoming = _status_observation(status_raw, payload_raw, target, worker_result)
    phase, receipt_hash = incoming.producer_phase, incoming.receipt_sha256
    if previous is not None:
        if not isinstance(previous, Observation) or previous.review_target != target:
            raise ConsumerFailure("Retry differs from the original immutable review target")
        if previous.producer_phase in TERMINAL_PHASES and (
            previous.producer_phase != phase or previous.receipt_sha256 != receipt_hash
        ):
            raise ConsumerFailure("A terminal receipt cannot be replaced or regressed")
        if previous.producer_phase == "pending" and phase == "prepared":
            raise ConsumerFailure("Producer state regressed")
    observation = Observation(
        target, phase, worker_result, receipt_hash, cancelled or bool(previous and previous.cancelled)
    )
    return Consumption(observation, observation == previous)
