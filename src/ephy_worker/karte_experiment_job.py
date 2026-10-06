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

from .formal_artifacts import GateFailure, digest, encode, now, read_json, safe_path, write_json
from .formal_runtime import exclusive_lock, validate_contract
from .karte_experiment_consumer import (
    MANIFEST_REF,
    MAX_ARTIFACT,
    MAX_JSON,
    MAX_TOTAL,
    METADATA_REF,
    ConsumerFailure,
    Observation,
    ReviewTarget,
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


def _previous(record: dict, job_pin: str) -> Observation | None:
    if (
        record.get("schema_version") != SCHEMA
        or record.get("job_sha256") != job_pin
        or (record.get("adopted") is not False or record.get("review_ready") is not False)
    ):
        raise GateFailure("Saved consumer state identity or authority mismatch")
    if record.get("state") not in {
        "received",
        "verified",
        "review_pending",
        "cancelled",
        "result_failed",
        "verification_failed",
    }:
        raise GateFailure("Saved consumer state exceeds the receipt boundary")
    value = record.get("observation")
    if value is None:
        return None
    try:
        target = ReviewTarget(**value["review_target"])
        observation = Observation(**{**value, "review_target": target})
    except (KeyError, TypeError) as exc:
        raise GateFailure("Invalid saved consumer observation") from exc
    if type(observation.cancelled) is not bool:
        raise GateFailure("Invalid saved cancellation state")
    return observation


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
        saved = read_json(state_file) if state_file.exists() else None
        previous = _previous(saved, job_pin) if saved else None
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
                cancelled=cancelled or cancellation,
            )
            if _read(job_file) != job_raw:
                raise GateFailure("Job changed while consuming its result")
            if result.duplicate:
                return {**saved, "duplicate": True}
            observation = result.observation
            record["observation"] = asdict(observation)
            transitions.append({"state": "verified", "at": now()})
            state = "review_pending"
            if observation.cancelled or job["status"] == "cancelled":
                state = "cancelled"
            elif observation.producer_phase in {"rejected", "conflict", "invalid"} or (
                observation.worker_result == "halted"
                or job["status"] in {"verification_failed", "failed", "halted", "budget_exhausted"}
            ):
                state = "result_failed"
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
                        "at": now(),
                        "adopted": False,
                        "review_ready": False,
                    }
                )
                _retain(sidecar, "verification-failure-" + digest(failure) + ".json", failure)
            raise GateFailure("Karte result verification failed; see consumer sidecar") from exc
        _retain(sidecar, "status-" + digest(status_raw) + ".json", status_raw)
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
