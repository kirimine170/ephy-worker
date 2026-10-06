"""Explicit post-run Karte receipt connection for an existing Worker Job.

Only a separate sidecar is written. The model runner, Job status, audit bundle
and integration gates are unchanged, and this adapter never grants adoption.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from .formal_artifacts import (
    CONTROL_PATHS,
    FINAL_BINDINGS,
    GateFailure,
    digest,
    encode,
    now,
    read_json,
    safe_path,
    write_json,
)
from .formal_runtime import exclusive_lock, validate_contract
from .karte_experiment_consumer import (
    MANIFEST_REF,
    MAX_ARTIFACT,
    MAX_JSON,
    MAX_TOTAL,
    METADATA_REF,
    TERMINAL_PHASES,
    ConsumerFailure,
    Observation,
    ReviewTarget,
    _status_observation,
    _timestamp,
    consume_result,
)
from .store import output_root

SCHEMA = "ephy.karte-consumer-job.v1"
STOPPED = {
    "external_review_pending",
    "review_ready",
    "verification_failed",
    "failed",
    "cancelled",
    "halted",
    "budget_exhausted",
}


def _read(path: Path, limit: int = MAX_JSON) -> bytes:
    info = path.lstat()
    if path.is_symlink() or path.is_junction() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GateFailure("Linked or nonregular consumer input")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise GateFailure("Consumer input exceeds byte limit")
    return raw


def _retain(directory: Path, name: str, raw: bytes) -> None:
    path = safe_path(directory, name, missing=True)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if _read(path) != raw:
            raise GateFailure("Consumer immutable snapshot cannot be replaced") from None


def _state(observation: Observation, job: dict) -> str:
    if observation.cancelled or job["status"] == "cancelled":
        return "cancelled"
    if observation.producer_phase in {"rejected", "conflict", "invalid"} or (
        observation.worker_result == "halted"
        or job["status"] in {"verification_failed", "failed", "halted", "budget_exhausted"}
    ):
        return "result_failed"
    return "review_pending"


def _terminal(observation: Observation, job_pin: str) -> bytes:
    return encode(
        {
            "schema_version": "ephy.karte-consumer-terminal.v1",
            "job_sha256": job_pin,
            "review_target": asdict(observation.review_target),
            "producer_phase": observation.producer_phase,
            "receipt_sha256": observation.receipt_sha256,
        }
    )


def _cancellation(target: ReviewTarget, job_pin: str) -> bytes:
    return encode(
        {
            "schema_version": "ephy.karte-consumer-cancellation.v1",
            "job_sha256": job_pin,
            "review_target": asdict(target),
        }
    )


def _previous(
    record: dict,
    job_pin: str,
    *,
    sidecar: Path,
    job: dict,
    candidate_id: str,
    payload_sha256: str,
    payload_raw: bytes,
    metadata_raw: bytes,
) -> Observation | None:
    """Revalidate every saved field, status semantics and immutable latch."""
    try:
        required = {
            "schema_version",
            "job_id",
            "job_sha256",
            "base_commit",
            "contract_sha256",
            "candidate_id",
            "payload_sha256",
            "status_sha256",
            "state",
            "transitions",
            "adopted",
            "review_ready",
            "observation",
        }
        if (
            not isinstance(record, dict)
            or not required <= record.keys()
            or (record.keys() - required - {"error_type"})
        ):
            raise GateFailure("Saved consumer fields do not match the contract")
        expected = {
            "schema_version": SCHEMA,
            "job_id": job["id"],
            "job_sha256": job_pin,
            "base_commit": job["baseRevision"],
            "contract_sha256": digest(encode(job["contract"])),
            "candidate_id": candidate_id,
            "payload_sha256": payload_sha256,
        }
        if any(record[key] != value for key, value in expected.items()) or (
            record["adopted"] is not False or record["review_ready"] is not False
        ):
            raise GateFailure("Saved consumer identity or authority mismatch")
        pin = record["status_sha256"]
        if not isinstance(pin, str) or len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise GateFailure("Invalid saved status identity")
        status_raw = _read(safe_path(sidecar, "status-" + pin + ".json"))
        if digest(status_raw) != pin:
            raise GateFailure("Saved status snapshot changed")
        if (
            _read(safe_path(sidecar, "payload.json")) != payload_raw
            or digest(payload_raw) != payload_sha256
            or _read(safe_path(sidecar, "metadata.json")) != metadata_raw
        ):
            raise GateFailure("Saved immutable payload or metadata changed")
        value = record["observation"]
        if value is None:
            if (
                record["state"] != "verification_failed"
                or not isinstance(record.get("error_type"), str)
                or not record["error_type"].isidentifier()
            ):
                raise GateFailure("Unobserved saved state cannot claim verification")
            expected_states = ["received", "verification_failed"]
            observation = None
        else:
            if "error_type" in record:
                raise GateFailure("Verified saved state cannot contain a failure claim")
            state_raw = _read(safe_path(sidecar, "state.json"))
            if _read(safe_path(sidecar, "state-" + digest(state_raw) + ".json")) != state_raw:
                raise GateFailure("Saved verified state differs from its immutable snapshot")
            metadata = read_json(sidecar / "metadata.json")
            binding = read_json(sidecar / "payload.json")
            target = ReviewTarget(
                candidate_id,
                payload_sha256,
                binding["record"]["patch_sha256"],
                metadata["experiment_id"],
                metadata["run_id"],
                metadata["attempt_id"],
                metadata["target_commit"],
            )
            if (
                target.run_id != job["id"]
                or target.experiment_id != job["id"] + "-audit"
                or target.target_commit != job["baseRevision"]
            ):
                raise GateFailure("Saved target differs from canonical Job")
            saved_target = ReviewTarget(**value["review_target"])
            observation = Observation(**{**value, "review_target": saved_target})
            if type(observation.cancelled) is not bool:
                raise GateFailure("Invalid saved cancellation state")
            reconstructed = _status_observation(
                status_raw,
                payload_raw,
                target,
                metadata["worker_result"],
                cancelled=observation.cancelled,
            )
            if observation != reconstructed or record["state"] != _state(reconstructed, job):
                raise GateFailure("Saved state or observation contradicts retained evidence")
            terminal = safe_path(sidecar, "terminal.json", missing=True)
            if terminal.exists() and (
                observation.producer_phase not in TERMINAL_PHASES
                or _read(terminal) != _terminal(observation, job_pin)
            ):
                raise GateFailure("Saved terminal observation regressed")
            cancellation = safe_path(sidecar, "cancelled.json", missing=True)
            if cancellation.exists() and (
                not observation.cancelled or _read(cancellation) != _cancellation(target, job_pin)
            ):
                raise GateFailure("Saved cancellation observation regressed")
            expected_states = ["received", "verified", record["state"]]
        transitions = record["transitions"]
        if (
            not isinstance(transitions, list)
            or len(transitions) != len(expected_states)
            or any(not isinstance(event, dict) or event.keys() != {"state", "at"} for event in transitions)
            or [event["state"] for event in transitions] != expected_states
        ):
            raise GateFailure("Saved transition sequence contradicts its state")
        for event in transitions:
            _timestamp(event["at"])
        return observation
    except (ConsumerFailure, OSError, KeyError, TypeError, ValueError) as exc:
        raise GateFailure("Invalid saved consumer state or immutable snapshot") from exc


def _frozen_bindings(directory: Path, bundle: Path, job: dict, artifacts: dict[str, bytes]) -> None:
    """Check retained Job freeze identities without granting audit/adoption authority."""
    audit_raw = _read(safe_path(bundle, "audit-input.json"))
    audit_input = read_json(bundle / "audit-input.json")
    if (
        not isinstance(audit_input, dict)
        or audit_input.get("schema_version") != "ephy.audit-input.v1"
        or audit_input.get("proposal_only") is not True
        or not isinstance(audit_input.get("stage_contract"), dict)
    ):
        raise GateFailure("Invalid frozen Job audit input")
    integrity_raw = _read(safe_path(bundle, "bundle-integrity.json"))
    integrity = read_json(bundle / "bundle-integrity.json")
    manifest = json.loads(artifacts[MANIFEST_REF])
    bindings = {key: digest(artifacts["worker/artifacts/" + name]) for key, name in FINAL_BINDINGS.items()}
    controls = {
        name: digest(artifacts["worker/artifacts/" + name])
        for name in (*CONTROL_PATHS, "environment_contract")
    }
    recomputed = [
        {
            "artifact_id": entry["artifact_id"],
            "path": entry["path"],
            "size_bytes": len(artifacts["worker/artifacts/" + entry["artifact_id"]]),
            "sha256": digest(artifacts["worker/artifacts/" + entry["artifact_id"]]),
        }
        for entry in manifest["artifacts"]
    ]
    expected_integrity = {
        "manifest_schema_valid": True,
        "path_safety_valid": True,
        "all_artifacts_match": True,
        "recomputed_artifacts": recomputed,
        "manifest_sha256": digest(artifacts[MANIFEST_REF]),
    }
    if (
        audit_input["job_id"] != job["id"]
        or audit_input["audit_id"] != job["id"] + "-audit"
        or audit_input["baseline_commit"] != job["baseRevision"]
        or audit_input["evidence_manifest_path"] != "evidence-manifest.json"
        or audit_input["evidence_manifest_sha256"] != digest(artifacts[MANIFEST_REF])
        or audit_input["bundle_integrity_attestation_path"] != "bundle-integrity.json"
        or audit_input["bundle_integrity_attestation_sha256"] != digest(integrity_raw)
        or audit_input["final_bindings"] != bindings
        or audit_input["control_document_hashes"] != controls
        or audit_input["allowed_file_scope"] != job["contract"]["allowed_files"]
        or audit_input["semantic_scope"] != job["contract"]["semantic_scope"]
        or audit_input["required_checks"]
        != [check["id"] for check in job["contract"]["checks"]] + ["scope", "diff"]
        or audit_input["stage_contract"]["expected_models"] != job["model_identities"]
        or audit_input["stage_contract"]["verifier_identity"] != job["verifier_identity"]
        or audit_input["stage_contract"]["max_repair_attempts"] != job["contract"]["max_repairs"]
        or integrity != expected_integrity
    ):
        raise GateFailure("Job evidence differs from its frozen audit input")
    mode = job.get("runtime", {}).get("review_mode", "formal")
    if mode == "external_codex":
        _read(safe_path(directory, "external-review.json"))
        anchor = read_json(directory / "external-review.json")
        if (
            not isinstance(anchor, dict)
            or anchor.get("schema") != "ephy.external-review.v1"
            or anchor.get("job_id") != job["id"]
            or anchor.get("audit_input_sha256") != digest(audit_raw)
            or anchor.get("final_bindings") != bindings
            or anchor.get("formal_audit_executed") is not False
            or anchor.get("adopted") is not False
            or anchor.get("required")
            != ["current-head CI", "independent Codex Review", "no unresolved P0/P1"]
            or job["status"] == "review_ready"
            or (
                job["status"] == "external_review_pending" and job.get("outcome") != "external_review_pending"
            )
        ):
            raise GateFailure("Job external-review freeze binding changed")
    elif mode == "formal":
        result_raw = _read(safe_path(directory, "audit-result.json"))
        result = read_json(directory / "audit-result.json")
        if not isinstance(result, dict) or result.get("schema_version") != "ephy.audit-result.v1":
            raise GateFailure("Invalid frozen Job audit result")
        _read(safe_path(directory, "audit-execution-attestation.json"))
        anchor = read_json(directory / "audit-execution-attestation.json")
        expected = {
            "audit_input_sha256": digest(audit_raw),
            "evidence_manifest_sha256": digest(artifacts[MANIFEST_REF]),
            "prompt_template_sha256": digest(artifacts["worker/artifacts/audit_prompt"]),
            **{key: value for key, value in bindings.items() if key != "changed_files_manifest_sha256"},
        }
        if (
            not isinstance(anchor, dict)
            or anchor.get("audit_input_sha256") != digest(audit_raw)
            or anchor.get("audit_result_sha256") != digest(result_raw)
            or any(
                anchor.get(field) is not True
                for field in (
                    "passed",
                    "schema_valid",
                    "candidate_unchanged",
                    "bundle_unchanged",
                    "proposal_stopped",
                )
            )
            or result["job_id"] != job["id"]
            or result["audit_id"] != job["id"] + "-audit"
            or result["bound_inputs"] != expected
            or job["status"] == "external_review_pending"
            or (
                job["status"] == "review_ready"
                and (job.get("outcome") != "accepted_proposal" or result["decision"] != "ACCEPT_PROPOSAL")
            )
        ):
            raise GateFailure("Job formal-audit freeze binding changed")
    else:
        raise GateFailure("Unsupported stopped Job review mode")


def _artifacts(directory: Path, metadata: bytes, job: dict) -> dict[str, bytes]:
    bundle = safe_path(directory, "audit-bundle")
    manifest_raw = _read(safe_path(bundle, "evidence-manifest.json"))
    manifest = read_json(bundle / "evidence-manifest.json")
    artifacts = {METADATA_REF: metadata, MANIFEST_REF: manifest_raw}
    total = len(metadata) + len(manifest_raw)
    for entry in manifest["artifacts"]:
        name = entry["artifact_id"]
        if not isinstance(name, str) or f"worker/artifacts/{name}" in artifacts:
            raise GateFailure("Duplicate or invalid Job artifact ID")
        raw = _read(safe_path(bundle, entry["path"]), MAX_ARTIFACT)
        total += len(raw)
        if total > MAX_TOTAL:
            raise GateFailure("Job evidence exceeds byte limit")
        artifacts[f"worker/artifacts/{name}"] = raw
    if artifacts.get("worker/artifacts/task_spec") != encode(job["contract"]):
        raise GateFailure("Job contract differs from its frozen task artifact")
    if artifacts.get("worker/artifacts/candidate_patch") != _read(
        safe_path(directory, "candidate.patch"), MAX_ARTIFACT
    ):
        raise GateFailure("Job patch differs from its frozen artifact")
    _frozen_bindings(directory, bundle, job, artifacts)
    return artifacts


def record_result(
    job_file: Path,
    status_raw: bytes,
    payload_raw: bytes,
    metadata_raw: bytes,
    *,
    candidate_id: str,
    payload_sha256: str,
    cancelled: bool = False,
) -> dict:
    """Bind producer-validated snapshots to a stopped Job's original evidence.

    Candidate/payload pins must be retained by the caller before receipt. The
    returned sidecar state is not the Job's formal workflow or review decision.
    """
    job_file = Path(job_file).absolute()
    if job_file.name != "job.json":
        raise GateFailure("Consumer requires the canonical job.json")
    directory = job_file.parent
    if any(parent.is_symlink() or parent.is_junction() for parent in (directory, *directory.parents)):
        raise GateFailure("Linked Job directory")
    output_root(directory)
    job_file = safe_path(directory, job_file.name)
    job_raw = _read(job_file)
    job = read_json(job_file)
    if (
        job.get("schemaVersion") != 2
        or Path(job.get("jobDir", "")).absolute() != directory
        or (not isinstance(job.get("id"), str) or not job["id"] or job.get("status") not in STOPPED)
    ):
        raise GateFailure("Only an existing stopped Worker Job can receive results")
    validate_contract(job["contract"])
    if any(type(raw) is not bytes or len(raw) > MAX_JSON for raw in (status_raw, payload_raw, metadata_raw)):
        raise GateFailure("Bounded immutable consumer inputs required")
    # This preliminary check prevents a live adapter from being retained at all.
    try:
        metadata = json.loads(metadata_raw)
    except (ValueError, UnicodeError) as exc:
        raise GateFailure("Invalid adapter metadata") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("synthetic_only") is not True
        or (metadata.get("adapter_version") != "karte.worker-experiment.v1")
    ):
        raise GateFailure("Only explicit synthetic adapter v1 can connect here")
    if (
        metadata.get("run_id") != job["id"]
        or metadata.get("experiment_id") != job["id"] + "-audit"
        or (metadata.get("target_commit") != job.get("baseRevision"))
    ):
        raise GateFailure("Adapter is not bound to this Job and base")
    sidecar = safe_path(directory, "karte-consumer", missing=True)
    sidecar.mkdir(exist_ok=True)
    lock = safe_path(sidecar, "consumer.lock", missing=True)
    with exclusive_lock(lock):
        job_pin = digest(job_raw)
        state_file = safe_path(sidecar, "state.json", missing=True)
        saved = None
        if state_file.exists():
            try:
                _read(state_file)
                saved = read_json(state_file)
            except (OSError, ValueError, RecursionError) as exc:
                raise GateFailure("Invalid saved consumer JSON") from exc
            if not isinstance(saved, dict):
                raise GateFailure("Saved consumer state must be an object")
        previous = (
            _previous(
                saved,
                job_pin,
                sidecar=sidecar,
                job=job,
                candidate_id=candidate_id,
                payload_sha256=payload_sha256,
                payload_raw=payload_raw,
                metadata_raw=metadata_raw,
            )
            if saved is not None
            else None
        )
        _retain(sidecar, "payload.json", payload_raw)
        _retain(sidecar, "metadata.json", metadata_raw)
        transitions = [{"state": "received", "at": now()}]
        record = {
            "schema_version": SCHEMA,
            "job_id": job["id"],
            "job_sha256": job_pin,
            "base_commit": job["baseRevision"],
            "contract_sha256": digest(encode(job["contract"])),
            "candidate_id": candidate_id,
            "payload_sha256": payload_sha256,
            "status_sha256": digest(status_raw),
            "state": "received",
            "transitions": transitions,
            "adopted": False,
            "review_ready": False,
            "observation": asdict(previous) if previous else None,
        }
        try:
            cancellation = safe_path(directory, "cancel.request", missing=True).exists()
            result = consume_result(
                status_raw,
                payload_raw,
                _artifacts(directory, metadata_raw, job),
                candidate_id=candidate_id,
                payload_sha256=payload_sha256,
                previous=previous,
                cancelled=cancelled
                or cancellation
                or job["status"] == "cancelled"
                or safe_path(sidecar, "cancelled.json", missing=True).exists(),
            )
            if _read(job_file) != job_raw:
                raise GateFailure("Job changed while consuming its result")
            terminal = safe_path(sidecar, "terminal.json", missing=True)
            if terminal.exists() or result.observation.producer_phase in TERMINAL_PHASES:
                _retain(sidecar, "terminal.json", _terminal(result.observation, job_pin))
            if result.observation.cancelled:
                _retain(sidecar, "cancelled.json", _cancellation(result.observation.review_target, job_pin))
            if result.duplicate:
                return {**saved, "duplicate": True}
            observation = result.observation
            record["observation"] = asdict(observation)
            transitions.append({"state": "verified", "at": now()})
            state = _state(observation, job)
            record["state"] = state
            transitions.append({"state": state, "at": now()})
        except (ConsumerFailure, GateFailure, OSError, KeyError, TypeError, ValueError) as exc:
            record["state"] = "verification_failed"
            record["error_type"] = type(exc).__name__
            transitions.append({"state": "verification_failed", "at": now()})
            _retain(sidecar, "status-" + digest(status_raw) + ".json", status_raw)
            if previous is None:
                write_json(safe_path(sidecar, "state.json", missing=True), record, exclusive=False)
            else:
                failure = encode(
                    {
                        "schema_version": "ephy.karte-consumer-verification-failure.v1",
                        "state": "verification_failed",
                        "job_sha256": job_pin,
                        "status_sha256": digest(status_raw),
                        "previous_state_sha256": digest(_read(state_file)),
                        "error_type": type(exc).__name__,
                        "error_sha256": digest(str(exc).encode("utf-8")),
                        "adopted": False,
                        "review_ready": False,
                    }
                )
                _retain(sidecar, "verification-failure-" + digest(failure) + ".json", failure)
            raise GateFailure("Karte result verification failed; see consumer sidecar") from exc
        _retain(sidecar, "status-" + digest(status_raw) + ".json", status_raw)
        snapshot = encode(record)
        _retain(sidecar, "state-" + digest(snapshot) + ".json", snapshot)
        write_json(safe_path(sidecar, "state.json", missing=True), record, exclusive=False)
        return {**record, "duplicate": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--payload-sha256", required=True)
    parser.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    result = record_result(
        args.job,
        _read(args.status),
        _read(args.payload),
        _read(args.metadata),
        candidate_id=args.candidate_id,
        payload_sha256=args.payload_sha256,
        cancelled=args.cancel,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
