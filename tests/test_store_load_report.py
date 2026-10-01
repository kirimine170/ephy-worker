"""Tests for JobStore.load_report：re-read the canonical report.json of a recorded job．"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from ephy_worker.schema import Candidate, Claim, Report, Source, utc_now
from ephy_worker.store import JobStore


def build_report(job_id: str) -> Report:
    return Report(
        job_id=job_id,
        question="Version 2 の上限を確認してください",
        profile_id="test-profile",
        subquestions=["上限は何件か．"],
        candidates=[Candidate(url="https://example.test/doc", title="doc")],
        sources=[
            Source(
                source_id="S1",
                requested_url="https://example.test/doc",
                final_url="https://example.test/doc",
                status="ok",
                kind="html",
            )
        ],
        claims=[Claim(claim_id="C1", text="上限は2件．", status="supported_primary", checked=True)],
        metrics={"source_count": 1},
    )


def test_load_report_round_trip(tmp_path):
    store = JobStore(tmp_path)
    report = build_report(store.job_id)
    store.save(report)

    loaded = store.load_report()
    assert isinstance(loaded, Report)
    assert loaded.job_id == report.job_id
    assert loaded.question == report.question
    assert loaded.profile_id == report.profile_id
    assert loaded.subquestions == report.subquestions
    assert [s.source_id for s in loaded.sources] == ["S1"]
    assert [c.claim_id for c in loaded.claims] == ["C1"]
    assert loaded.claims[0].status == "supported_primary"
    assert loaded.metrics == {"source_count": 1}


def test_load_report_after_reopen_round_trip(tmp_path):
    store = JobStore(tmp_path)
    store.save(build_report(store.job_id))

    reopened = JobStore.open_existing(tmp_path, store.job_id)
    loaded = reopened.load_report()
    assert loaded.job_id == store.job_id

    reopened.event("resumed")
    store.save(build_report(store.job_id))


def test_load_report_missing_file(tmp_path):
    store = JobStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_report()


def replace_payload_with_matching_manifest(store: JobStore, payload: bytes) -> None:
    """Keep the saved manifest valid so tests reach payload validation."""
    path = store.directory / "report.json"
    manifest_path = store.directory / "artifacts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["job_id"] == store.job_id
    path.write_bytes(payload)
    manifest["sha256"]["report.json"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_load_report_rejects_missing_manifest(tmp_path):
    store = JobStore(tmp_path)
    store.save(build_report(store.job_id))
    (store.directory / "artifacts.json").unlink()
    with pytest.raises(ValueError, match="^report artifact manifest is missing$"):
        store.load_report()


def test_load_report_rejects_hash_mismatch(tmp_path):
    store = JobStore(tmp_path)
    store.save(build_report(store.job_id))
    (store.directory / "report.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="^report hash mismatch$"):
        store.load_report()


def test_load_report_rejects_corrupted_payload(tmp_path):
    store = JobStore(tmp_path)
    store.save(build_report(store.job_id))
    replace_payload_with_matching_manifest(store, b"{not json")
    with pytest.raises(ValidationError) as caught:
        store.load_report()
    assert any(error["type"] == "json_invalid" for error in caught.value.errors())


def test_load_report_rejects_foreign_job_id(tmp_path):
    store = JobStore(tmp_path)
    store.save(build_report(store.job_id))
    payload = build_report("research-otherjob000000000000000000000a").model_dump_json()
    replace_payload_with_matching_manifest(store, payload.encode("utf-8"))
    with pytest.raises(ValueError, match="^report job_id mismatch:"):
        store.load_report()


def test_load_report_rejects_unknown_fields(tmp_path):
    store = JobStore(tmp_path)
    store.save(build_report(store.job_id))
    payload = build_report(store.job_id).model_dump(mode="json")
    payload["unexpected_field"] = 1
    replace_payload_with_matching_manifest(store, json.dumps(payload).encode("utf-8"))
    with pytest.raises(ValidationError) as caught:
        store.load_report()
    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == ("unexpected_field",)
        for error in caught.value.errors()
    )


def test_saved_report_is_reusable_without_stale_timestamps(tmp_path):
    store = JobStore(tmp_path)
    report = build_report(store.job_id)
    report.finished_at = utc_now()
    store.save(report)

    loaded = store.load_report()
    assert loaded.finished_at == report.finished_at
