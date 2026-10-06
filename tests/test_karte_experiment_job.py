from __future__ import annotations

import copy
from pathlib import Path

import pytest
import test_karte_experiment_consumer as consumer_fixtures
from test_formal_runtime import contract

from ephy_worker.formal_artifacts import GateFailure, digest, encode, read_json, write_json
from ephy_worker.formal_runtime import submit
from ephy_worker.karte_experiment_job import record_result


@pytest.fixture
def connected_job(tmp_path):
    fixture = consumer_fixtures.KarteExperimentConsumerTests()
    fixture.setUp()
    spec = {
        "repoRoot": str(tmp_path / "repository"),
        "baseRevision": "a" * 40,
        "contract": contract(),
        "runtime": {"review_mode": "external_codex"},
        "controls": {},
        "model_identities": {},
        "verifier_identity": {"executor_id": "synthetic"},
    }
    job_file = submit(spec, tmp_path / "jobs-root")
    job = read_json(job_file)
    job.update(status="external_review_pending", outcome="external_review_pending")
    write_json(job_file, job, exclusive=False)
    fixture.metadata.update(
        run_id=job["id"], experiment_id=job["id"] + "-audit", target_commit=job["baseRevision"]
    )
    fixture.record.update(
        run_id=job["id"], experiment_id=job["id"] + "-audit", target_commit=job["baseRevision"]
    )
    fixture.manifest.update(job_id=job["id"], audit_id=job["id"] + "-audit")
    fixture.artifacts["worker/artifacts/task_spec"] = encode(job["contract"])
    (job_file.parent / "candidate.patch").write_bytes(fixture.artifacts["worker/artifacts/candidate_patch"])
    for entry in fixture.manifest["artifacts"]:
        raw = fixture.artifacts["worker/artifacts/" + entry["artifact_id"]]
        entry.update(size_bytes=len(raw), sha256=digest(raw))
    fixture.artifacts["adapter/metadata.json"] = encode(fixture.metadata)
    fixture.artifacts["worker/evidence-manifest.json"] = encode(fixture.manifest)
    fixture.rebind()
    bundle = job_file.parent / "audit-bundle"
    bundle.mkdir()
    (bundle / "evidence-manifest.json").write_bytes(fixture.artifacts["worker/evidence-manifest.json"])
    for entry in fixture.manifest["artifacts"]:
        path = bundle / entry["path"]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(fixture.artifacts["worker/artifacts/" + entry["artifact_id"]])
    return job_file, fixture


def receive(job_file, fixture, phase="report_accepted", **kwargs):
    return record_result(
        job_file,
        encode(fixture.status(phase)),
        fixture.payload,
        fixture.artifacts["adapter/metadata.json"],
        candidate_id=fixture.candidate,
        payload_sha256=fixture.pin,
        **kwargs,
    )


def snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_existing_submit_created_job_receives_verified_review_pending_sidecar(connected_job):
    job_file, fixture = connected_job
    original_job = job_file.read_bytes()
    original_bundle = snapshot(job_file.parent / "audit-bundle")
    result = receive(job_file, fixture)
    assert result["state"] == "review_pending"
    assert [event["state"] for event in result["transitions"]] == ["received", "verified", "review_pending"]
    assert result["job_id"] == read_json(job_file)["id"]
    assert result["contract_sha256"] == digest(encode(read_json(job_file)["contract"]))
    assert result["observation"]["review_target"]["patch_sha256"] == fixture.record["patch_sha256"]
    assert not result["adopted"] and not result["review_ready"]
    assert job_file.read_bytes() == original_job
    assert snapshot(job_file.parent / "audit-bundle") == original_bundle


def test_duplicate_receipt_after_reopen_is_read_only(connected_job):
    job_file, fixture = connected_job
    first = receive(job_file, fixture)
    before = snapshot(job_file.parent)
    retry = receive(job_file, fixture)
    assert retry["duplicate"]
    assert {key: value for key, value in retry.items() if key != "duplicate"} == {
        key: value for key, value in first.items() if key != "duplicate"
    }
    assert snapshot(job_file.parent) == before


def test_cancel_latches_across_pending_and_late_receipt(connected_job):
    job_file, fixture = connected_job
    cancelled = receive(job_file, fixture, "pending", cancelled=True)
    assert cancelled["state"] == "cancelled"
    late = receive(job_file, fixture)
    assert late["state"] == "cancelled"
    assert late["observation"]["producer_phase"] == "report_accepted"
    assert late["observation"]["cancelled"]
    assert not late["adopted"] and not late["review_ready"]


def test_existing_cancel_request_is_respected(connected_job):
    job_file, fixture = connected_job
    (job_file.parent / "cancel.request").write_text("synthetic cancel", encoding="utf-8")
    assert receive(job_file, fixture)["state"] == "cancelled"


@pytest.mark.parametrize("phase", ["rejected", "conflict", "invalid"])
def test_failed_result_never_advances_to_review_ready(connected_job, phase):
    job_file, fixture = connected_job
    result = receive(job_file, fixture, phase)
    assert result["state"] == "result_failed"
    assert [event["state"] for event in result["transitions"]] == ["received", "verified", "result_failed"]
    assert not result["adopted"] and not result["review_ready"]


@pytest.mark.parametrize(
    "state", ["failed", "verification_failed", "halted", "budget_exhausted", "cancelled"]
)
def test_worker_failed_or_cancelled_job_is_not_promoted(connected_job, state):
    job_file, fixture = connected_job
    job = read_json(job_file)
    job["status"] = state
    write_json(job_file, job, exclusive=False)
    before = job_file.read_bytes()
    result = receive(job_file, fixture)
    assert result["state"] == ("cancelled" if state == "cancelled" else "result_failed")
    assert job_file.read_bytes() == before
    assert not result["adopted"]


@pytest.mark.parametrize("mutation", ["missing", "changed", "manifest", "schema", "contract", "job-patch"])
def test_failed_verification_records_failure_without_job_or_bundle_mutation(connected_job, mutation):
    job_file, fixture = connected_job
    bundle = job_file.parent / "audit-bundle"
    path = bundle / "artifacts/candidate_patch.txt"
    if mutation == "missing":
        path.unlink()
    elif mutation == "changed":
        path.write_bytes(b"modified synthetic patch")
    elif mutation == "manifest":
        manifest = read_json(bundle / "evidence-manifest.json")
        manifest["job_id"] = "another-job"
        write_json(bundle / "evidence-manifest.json", manifest, exclusive=False)
    elif mutation == "contract":
        job = read_json(job_file)
        job["contract"]["task"] = "a different synthetic requirement"
        write_json(job_file, job, exclusive=False)
    elif mutation == "job-patch":
        (job_file.parent / "candidate.patch").write_bytes(b"a different synthetic patch")
    else:
        fixture.binding["schema_version"] = "karte.experiment-payload.v2"
        fixture.payload = encode(fixture.binding)
        fixture.pin = digest(fixture.payload)
    before_job = job_file.read_bytes()
    before_bundle = snapshot(bundle)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    result = read_json(job_file.parent / "karte-consumer/state.json")
    assert result["state"] == "verification_failed"
    assert [event["state"] for event in result["transitions"]] == ["received", "verification_failed"]
    assert not result["adopted"] and not result["review_ready"]
    assert job_file.read_bytes() == before_job
    assert snapshot(bundle) == before_bundle


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "another-job"),
        ("experiment_id", "another-audit"),
        ("target_commit", "b" * 40),
        ("synthetic_only", False),
    ],
)
def test_wrong_job_base_or_live_metadata_refused_before_sidecar_write(connected_job, field, value):
    job_file, fixture = connected_job
    metadata = copy.deepcopy(fixture.metadata)
    metadata[field] = value
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        record_result(
            job_file,
            encode(fixture.status()),
            fixture.payload,
            encode(metadata),
            candidate_id=fixture.candidate,
            payload_sha256=fixture.pin,
        )
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize("state", ["queued", "running", "preparing", "audit_pending"])
def test_active_job_is_not_touched(connected_job, state):
    job_file, fixture = connected_job
    job = read_json(job_file)
    job["status"] = state
    write_json(job_file, job, exclusive=False)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize(
    "phase,cancelled", [("pending", True), ("report_accepted", False), ("rejected", False)]
)
@pytest.mark.parametrize("mutation", ["missing", "malformed"])
def test_invalid_retry_preserves_verified_state_and_recovers_idempotently(
    connected_job, phase, cancelled, mutation
):
    job_file, fixture = connected_job
    first = receive(job_file, fixture, phase, cancelled=cancelled)
    sidecar = job_file.parent / "karte-consumer"
    state_raw = (sidecar / "state.json").read_bytes()
    job_raw = job_file.read_bytes()
    bundle = job_file.parent / "audit-bundle"
    artifact = bundle / "artifacts/candidate_patch.txt"
    original_artifact = artifact.read_bytes()
    status_raw = encode(fixture.status(phase))
    if mutation == "missing":
        artifact.unlink()
    else:
        status_raw = b"{malformed retry"
    before_bundle = snapshot(bundle)
    with pytest.raises(GateFailure):
        record_result(
            job_file,
            status_raw,
            fixture.payload,
            fixture.artifacts["adapter/metadata.json"],
            candidate_id=fixture.candidate,
            payload_sha256=fixture.pin,
        )
    assert (sidecar / "state.json").read_bytes() == state_raw
    failures = list(sidecar.glob("verification-failure-*.json"))
    assert len(failures) == 1
    failure = read_json(failures[0])
    assert failure["state"] == "verification_failed"
    assert failure["previous_state_sha256"] == digest(state_raw)
    assert failure["status_sha256"] == digest(status_raw)
    assert failure["adopted"] is False and failure["review_ready"] is False
    assert job_file.read_bytes() == job_raw
    assert snapshot(bundle) == before_bundle
    if mutation == "missing":
        artifact.write_bytes(original_artifact)
    before_retry = snapshot(job_file.parent)
    retry = receive(job_file, fixture, phase)
    assert retry["duplicate"]
    assert retry["state"] == first["state"]
    assert retry["observation"]["cancelled"] == cancelled
    assert (sidecar / "state.json").read_bytes() == state_raw
    assert snapshot(job_file.parent) == before_retry


def test_changed_payload_cannot_overwrite_immutable_snapshot(connected_job):
    job_file, fixture = connected_job
    receive(job_file, fixture)
    before = snapshot(job_file.parent)
    fixture.metadata["interpretation"] = "different synthetic result"
    fixture.artifacts["adapter/metadata.json"] = encode(fixture.metadata)
    fixture.rebind()
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize("field,value", [("adopted", True), ("state", "review_ready"), ("state", "adopted")])
def test_state_authority_tampering_is_refused(connected_job, field, value):
    job_file, fixture = connected_job
    receive(job_file, fixture)
    path = job_file.parent / "karte-consumer/state.json"
    state = read_json(path)
    state[field] = value
    write_json(path, state, exclusive=False)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before


def test_consumer_hardlink_and_concurrent_lock_are_refused(connected_job):
    from ephy_worker.formal_runtime import exclusive_lock

    job_file, fixture = connected_job
    receive(job_file, fixture)
    root = job_file.parent / "karte-consumer"
    with exclusive_lock(root / "consumer.lock"), pytest.raises(GateFailure):
        receive(job_file, fixture)
    alias = job_file.parent / "payload-alias.json"
    alias.hardlink_to(root / "payload.json")
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before
