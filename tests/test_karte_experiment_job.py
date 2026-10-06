from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import test_karte_experiment_consumer as consumer_fixtures
from test_formal_runtime import accepted_result, contract

from ephy_worker.formal_artifacts import (
    CONTROL_PATHS,
    GateFailure,
    digest,
    encode,
    freeze_bundle,
    read_json,
    write_json,
)
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
        "model_identities": {
            role: {
                "model_id": "synthetic",
                "model_artifact_manifest_sha256": "a" * 64,
                "runtime_sha256": "b" * 64,
                "invocation_config_sha256": "c" * 64,
            }
            for role in ("planner", "implementer", "auditor")
        },
        "verifier_identity": {
            "executor_id": "synthetic",
            "runtime_sha256": "d" * 64,
            "invocation_config_sha256": "e" * 64,
        },
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
    for name, path in CONTROL_PATHS.items():
        fixture.artifacts["worker/artifacts/" + name] = (
            Path(__file__).resolve().parents[1] / path
        ).read_bytes()
    fixture.artifacts["worker/artifacts/task_spec"] = encode(job["contract"])
    fixture.artifacts["worker/artifacts/verification_results"] = encode(
        {"verifier_identity": job["verifier_identity"], "synthetic_only": True}
    )
    fixture.artifacts["worker/artifacts/workflow_events"] = encode([])
    (job_file.parent / "candidate.patch").write_bytes(fixture.artifacts["worker/artifacts/candidate_patch"])
    bundle = job_file.parent / "audit-bundle"
    audit_input = freeze_bundle(
        bundle,
        job,
        {name: fixture.artifacts["worker/artifacts/" + name] for name in consumer_fixtures.WORKER_ARTIFACTS},
    )
    fixture.manifest = read_json(bundle / "evidence-manifest.json")
    fixture.artifacts["adapter/metadata.json"] = encode(fixture.metadata)
    fixture.artifacts["worker/evidence-manifest.json"] = (bundle / "evidence-manifest.json").read_bytes()
    fixture.rebind()
    write_json(
        job_file.parent / "external-review.json",
        {
            "schema": "ephy.external-review.v1",
            "job_id": job["id"],
            "audit_input_sha256": digest((bundle / "audit-input.json").read_bytes()),
            "final_bindings": audit_input["final_bindings"],
            "workflow": [],
            "required": ["current-head CI", "independent Codex Review", "no unresolved P0/P1"],
            "formal_audit_executed": False,
            "adopted": False,
        },
    )
    return job_file, fixture


def formal_stop(job_file, fixture):
    """Synthetic frozen bindings only; no model audit or approval is executed."""
    job = read_json(job_file)
    job["runtime"]["review_mode"] = "formal"
    job.update(status="review_ready", outcome="accepted_proposal")
    write_json(job_file, job, exclusive=False)
    bundle = job_file.parent / "audit-bundle"
    result = accepted_result(bundle, read_json(bundle / "audit-input.json"))
    write_json(job_file.parent / "audit-result.json", result)
    write_json(
        job_file.parent / "audit-execution-attestation.json",
        {
            "passed": True,
            "schema_valid": True,
            "candidate_unchanged": True,
            "bundle_unchanged": True,
            "proposal_stopped": True,
            "audit_input_sha256": digest((bundle / "audit-input.json").read_bytes()),
            "audit_result_sha256": digest((job_file.parent / "audit-result.json").read_bytes()),
        },
    )


def replace_bundle(job_file, fixture, artifact, refreeze):
    """Consistently substitute evidence while leaving terminal Job anchors intact."""
    ref = "worker/artifacts/" + artifact
    if artifact == "verification_results":
        value = json.loads(fixture.artifacts[ref])
        value["substituted"] = True
        fixture.artifacts[ref] = encode(value)
    else:
        fixture.artifacts[ref] += b"changed synthetic evidence\n"
    bundle = job_file.parent / "audit-bundle"
    if refreeze:
        replacement = job_file.parent / "replacement-bundle"
        freeze_bundle(
            replacement,
            read_json(job_file),
            {
                name: fixture.artifacts["worker/artifacts/" + name]
                for name in consumer_fixtures.WORKER_ARTIFACTS
            },
        )
        for path in replacement.iterdir():
            (bundle / path.name).write_bytes(path.read_bytes())
        fixture.manifest = read_json(bundle / "evidence-manifest.json")
    else:
        for entry in fixture.manifest["artifacts"]:
            raw = fixture.artifacts["worker/artifacts/" + entry["artifact_id"]]
            entry.update(size_bytes=len(raw), sha256=digest(raw))
            (bundle / entry["path"]).write_bytes(raw)
        write_json(bundle / "evidence-manifest.json", fixture.manifest, exclusive=False)
    patch = fixture.artifacts["worker/artifacts/candidate_patch"]
    (job_file.parent / "candidate.patch").write_bytes(patch)
    fixture.record["patch_sha256"] = digest(patch)
    fixture.artifacts["worker/evidence-manifest.json"] = (bundle / "evidence-manifest.json").read_bytes()
    fixture.rebind()


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
    assert result["observation"]["cancelled"] is (state == "cancelled")
    assert job_file.read_bytes() == before
    assert not result["adopted"]


@pytest.mark.parametrize("mutation", ["missing", "changed", "manifest", "schema", "contract", "job-patch"])
def test_failed_verification_records_failure_without_job_or_bundle_mutation(connected_job, mutation):
    job_file, fixture = connected_job
    bundle = job_file.parent / "audit-bundle"
    path = bundle / "candidate_patch.txt"
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
    artifact = bundle / "candidate_patch.txt"
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
    before_failed_retry = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        record_result(
            job_file,
            status_raw,
            fixture.payload,
            fixture.artifacts["adapter/metadata.json"],
            candidate_id=fixture.candidate,
            payload_sha256=fixture.pin,
        )
    assert snapshot(job_file.parent) == before_failed_retry
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
    fixture.record["interpretation"] = fixture.metadata["interpretation"]
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


@pytest.mark.parametrize("state", ["running", "audit_pending", "external_review_pending"])
def test_alternate_job_json_cannot_replace_canonical_stopped_state(connected_job, state):
    job_file, fixture = connected_job
    alias = job_file.parent / "stale-job.json"
    alias.write_bytes(job_file.read_bytes())
    job = read_json(job_file)
    job["status"] = state
    write_json(job_file, job, exclusive=False)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(alias, fixture)
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize("mode", ["external", "formal"])
@pytest.mark.parametrize("artifact", ["candidate_patch", "lead_plan", "verification_results"])
@pytest.mark.parametrize("refreeze", [False, True])
def test_consistent_replacement_cannot_discard_original_job_freeze(connected_job, mode, artifact, refreeze):
    job_file, fixture = connected_job
    if mode == "formal":
        formal_stop(job_file, fixture)
    replace_bundle(job_file, fixture, artifact, refreeze)
    # The transport is valid on its own; the original Job's frozen bindings are not.
    assert fixture.consume().observation.review_target.patch_sha256 == fixture.record["patch_sha256"]
    before = {
        name: raw for name, raw in snapshot(job_file.parent).items() if not name.startswith("karte-consumer/")
    }
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert {
        name: raw for name, raw in snapshot(job_file.parent).items() if not name.startswith("karte-consumer/")
    } == before
    assert read_json(job_file.parent / "karte-consumer/state.json")["state"] == "verification_failed"


def test_formal_stopped_job_binding_does_not_grant_audit_or_adoption_authority(connected_job):
    job_file, fixture = connected_job
    formal_stop(job_file, fixture)
    before = {
        name: raw for name, raw in snapshot(job_file.parent).items() if not name.startswith("karte-consumer/")
    }
    result = receive(job_file, fixture)
    assert result["state"] == "review_pending"
    assert not result["adopted"] and not result["review_ready"]
    assert {
        name: raw for name, raw in snapshot(job_file.parent).items() if not name.startswith("karte-consumer/")
    } == before


@pytest.mark.parametrize(
    "path", ["audit-bundle/audit-input.json", "audit-bundle/bundle-integrity.json", "external-review.json"]
)
def test_missing_frozen_job_anchor_is_refused(connected_job, path):
    job_file, fixture = connected_job
    (job_file.parent / path).unlink()
    original_job = job_file.read_bytes()
    original_bundle = snapshot(job_file.parent / "audit-bundle")
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert job_file.read_bytes() == original_job
    assert snapshot(job_file.parent / "audit-bundle") == original_bundle


SAVED_MUTATIONS = [
    ("schema_version", "unknown"),
    ("job_id", "another-job"),
    ("job_sha256", "0" * 64),
    ("base_commit", "b" * 40),
    ("contract_sha256", "0" * 64),
    ("candidate_id", "another-candidate"),
    ("payload_sha256", "0" * 64),
    ("status_sha256", "0" * 64),
    ("state", "other-terminal"),
    ("state", "received"),
    ("state", "verified"),
    ("state", "verification_failed"),
    ("adopted", True),
    ("review_ready", True),
    ("unexpected", True),
    ("error_type", "GateFailure"),
    ("observation", None),
    ("observation.producer_phase", "earlier-phase"),
    ("observation.worker_result", "halted"),
    ("observation.receipt_sha256", "0" * 64),
    ("observation.cancelled", "flip"),
    ("observation.cancelled", 1),
    *[
        ("observation.review_target." + field, "changed")
        for field in (
            "candidate_id",
            "payload_sha256",
            "patch_sha256",
            "experiment_id",
            "run_id",
            "attempt_id",
            "target_commit",
        )
    ],
    ("transitions", []),
    ("transition_state", "review_pending"),
    ("transition_time", "invalid"),
    ("transition_extra", True),
]


def alter_saved(record, field, value):
    if field == "state" and value == "other-terminal":
        value = "cancelled" if record["state"] == "review_pending" else "review_pending"
    if field == "observation.producer_phase" and value == "earlier-phase":
        value = "prepared" if record["observation"]["producer_phase"] == "pending" else "pending"
    if field == "observation.cancelled" and value == "flip":
        value = not record["observation"]["cancelled"]
    if field.startswith("transition_"):
        key = {"transition_state": "state", "transition_time": "at", "transition_extra": "unexpected"}[field]
        record["transitions"][0][key] = value
        return
    target = record
    keys = field.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value


@pytest.mark.parametrize(
    "phase,cancelled", [("pending", True), ("rejected", False), ("report_accepted", False)]
)
@pytest.mark.parametrize("field,value", SAVED_MUTATIONS)
def test_every_saved_state_field_is_bound_to_retained_evidence(connected_job, phase, cancelled, field, value):
    job_file, fixture = connected_job
    receive(job_file, fixture, phase, cancelled=cancelled)
    sidecar = job_file.parent / "karte-consumer"
    record = read_json(sidecar / "state.json")
    alter_saved(record, field, value)
    write_json(sidecar / "state.json", record, exclusive=False)
    # Keep a self-consistent checksum to exercise semantic validation, not only byte identity.
    raw = (sidecar / "state.json").read_bytes()
    (sidecar / ("state-" + digest(raw) + ".json")).write_bytes(raw)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture, phase)
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize(
    "mutation", ["valid-timestamp", "whitespace", "missing-checkpoint", "changed-status"]
)
def test_saved_snapshot_bytes_and_status_are_immutable(connected_job, mutation):
    job_file, fixture = connected_job
    receive(job_file, fixture)
    sidecar = job_file.parent / "karte-consumer"
    state_file = sidecar / "state.json"
    original = state_file.read_bytes()
    if mutation == "missing-checkpoint":
        (sidecar / ("state-" + digest(original) + ".json")).unlink(missing_ok=True)
    elif mutation == "whitespace":
        state_file.write_bytes(original + b"\n")
    elif mutation == "changed-status":
        record = read_json(state_file)
        (sidecar / ("status-" + record["status_sha256"] + ".json")).write_bytes(
            encode(fixture.status("pending"))
        )
    else:
        record = read_json(state_file)
        record["transitions"][0]["at"] = "2000-01-01T00:00:00Z"
        write_json(state_file, record, exclusive=False)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize("latch", ["terminal", "cancelled"])
def test_coherent_saved_state_rollback_cannot_erase_latches(connected_job, latch):
    job_file, fixture = connected_job
    receive(job_file, fixture, "pending")
    state_file = job_file.parent / "karte-consumer/state.json"
    earlier = state_file.read_bytes()
    if latch == "terminal":
        receive(job_file, fixture)
    else:
        receive(job_file, fixture, "pending", cancelled=True)
    state_file.write_bytes(earlier)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture, "pending")
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "review_pending"),
        ("job_id", "other"),
        ("candidate_id", "other"),
        ("error_type", ""),
        ("transitions", []),
        ("observation", {}),
    ],
)
def test_initial_failure_state_cannot_claim_validated_progress(connected_job, field, value):
    job_file, fixture = connected_job
    artifact = job_file.parent / "audit-bundle/candidate_patch.txt"
    raw = artifact.read_bytes()
    artifact.unlink()
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    artifact.write_bytes(raw)
    state_file = job_file.parent / "karte-consumer/state.json"
    record = read_json(state_file)
    record[field] = value
    write_json(state_file, record, exclusive=False)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before


def test_initial_missing_evidence_can_recover_without_substituting_saved_fields(connected_job):
    job_file, fixture = connected_job
    artifact = job_file.parent / "audit-bundle/candidate_patch.txt"
    original = artifact.read_bytes()
    artifact.unlink()
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    artifact.write_bytes(original)
    recovered = receive(job_file, fixture)
    assert recovered["state"] == "review_pending"
    assert not recovered["adopted"] and not recovered["review_ready"]
    before = snapshot(job_file.parent)
    assert receive(job_file, fixture)["duplicate"]
    assert snapshot(job_file.parent) == before


@pytest.mark.parametrize(
    "raw",
    [b"{", b"null", b"[]", b"{}", b" " * (256 * 1024 + 1)],
    ids=["malformed", "null", "list", "empty", "oversized"],
)
def test_saved_json_cannot_be_malformed_nonobject_empty_or_unbounded(connected_job, raw):
    job_file, fixture = connected_job
    receive(job_file, fixture)
    (job_file.parent / "karte-consumer/state.json").write_bytes(raw)
    before = snapshot(job_file.parent)
    with pytest.raises(GateFailure):
        receive(job_file, fixture)
    assert snapshot(job_file.parent) == before
