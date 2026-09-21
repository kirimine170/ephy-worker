from __future__ import annotations

import hashlib
import os
import threading
import time

import pytest

from ephy_worker.config import ConfigurationError, ManagerSettings, RemoteConfig
from ephy_worker.contracts import (
    CONTRACT_VERSION,
    JOB_KIND,
    ArtifactRef,
    JobUsage,
    ResultEnvelope,
    SearchPayload,
    SubmitJob,
    WorkerRegistration,
)
from ephy_worker.manager import Manager, ManagerError
from ephy_worker.schema import Limits


def submit(key: str = "submit-0001", *, worker: str = "worker-a", topic: str = "公開仕様") -> SubmitJob:
    return SubmitJob(
        contract=CONTRACT_VERSION,
        submit_key=key,
        parent_job_id=None,
        kind=JOB_KIND,
        input={
            "mode": "search",
            "topic": topic,
            "round_index": 0,
            "queries": [
                {
                    "query_id": "Q1",
                    "text": "public specification",
                    "role": "overview",
                    "reason": "全体像を確認する",
                }
            ],
        },
        target_worker_id=worker,
        budget=Limits(),
        deadline_seconds=600,
    )


def registration(worker: str = "worker-a") -> WorkerRegistration:
    return WorkerRegistration(
        contract=CONTRACT_VERSION,
        worker_id=worker,
        capabilities=["web.collect"],
        implementation_version="test",
        os="test-os",
        architecture="test-arch",
        concurrency=1,
    )


def result(job_id: str, attempt_id: str, *, status: str = "succeeded") -> ResultEnvelope:
    return ResultEnvelope(
        contract=CONTRACT_VERSION,
        job_id=job_id,
        attempt_id=attempt_id,
        status=status,
        stop_reason=None if status == "succeeded" else "fixture_failure",
        usage=JobUsage(search_requests=1, search_credits=1),
        elapsed_seconds=0.1,
        payload=SearchPayload(
            mode="search",
            queries=[
                {
                    "query_id": "Q1",
                    "text": "public specification",
                    "role": "overview",
                    "reason": "全体像を確認する",
                    "status": "ok",
                    "result_count": 0,
                    "diagnostic": {"engine": "fixture"},
                }
            ],
            candidates=[],
            failures=[],
        ),
    )


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("EPHY_TEST_REQUESTER", "requester-secret")
    monkeypatch.setenv("EPHY_TEST_WORKER_A", "worker-a-secret")
    monkeypatch.setenv("EPHY_TEST_WORKER_B", "worker-b-secret")
    settings = ManagerSettings(
        database_path=tmp_path / "manager.db",
        artifact_dir=tmp_path / "artifacts",
        requester_credential_env="EPHY_TEST_REQUESTER",
        worker_credentials_env={
            "worker-a": "EPHY_TEST_WORKER_A",
            "worker-b": "EPHY_TEST_WORKER_B",
        },
        lease_ttl_seconds=2,
        worker_offline_seconds=2,
    )
    value = Manager(settings)
    yield value
    value.close()


def register(manager: Manager, worker: str = "worker-a") -> None:
    manager.upsert_worker(worker, f"{worker}-secret", registration(worker))


def test_model_less_manager_setup_and_worker_auth(manager):
    with pytest.raises(ManagerError, match="unauthorized"):
        manager.upsert_worker("worker-a", "wrong", registration())
    register(manager)
    assert manager.list_workers()[0]["contract_version"] == CONTRACT_VERSION


def test_remote_endpoint_requires_https_or_loopback_tunnel():
    loopback = RemoteConfig(manager_url="http://127.0.0.1:8321", target_worker_id="worker-a")
    assert loopback.endpoint() == "http://127.0.0.1:8321"
    private_http = RemoteConfig(manager_url="http://192.168.1.5:8321", target_worker_id="worker-a")
    with pytest.raises(ConfigurationError, match="requires HTTPS"):
        private_http.endpoint()
    private_https = RemoteConfig(manager_url="https://192.168.1.5:8321", target_worker_id="worker-a")
    assert private_https.endpoint() == "https://192.168.1.5:8321"


def test_submit_is_idempotent_and_conflicting_input_is_rejected(manager):
    first, created = manager.submit(submit())
    again, created_again = manager.submit(submit())
    assert created is True and created_again is False
    assert first["job_id"] == again["job_id"]
    with pytest.raises(ManagerError, match="submit_key_conflict"):
        manager.submit(submit(topic="異なる入力"))


def test_explicit_target_wait_reason_and_capability_check(manager):
    view, _ = manager.submit(submit(worker="worker-b"))
    assert view["wait_reason"] == "target_worker_unregistered"
    register(manager, "worker-b")
    assert manager.job_view(view["job_id"])["wait_reason"] is None
    assert manager.claim("worker-a", "worker-a-secret") is None
    assert manager.claim("worker-b", "worker-b-secret")["job"]["job_id"] == view["job_id"]


def test_claim_is_atomic_and_respects_concurrency(manager):
    register(manager)
    view, _ = manager.submit(submit())
    results: list[dict | None] = []

    def run_claim():
        results.append(manager.claim("worker-a", "worker-a-secret"))

    threads = [threading.Thread(target=run_claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item is not None for item in results) == 1
    assert manager.job_view(view["job_id"])["attempt_number"] == 1


def test_finalize_is_idempotent_only_for_identical_result(manager):
    register(manager)
    view, _ = manager.submit(submit())
    claim = manager.claim("worker-a", "worker-a-secret")
    envelope = result(view["job_id"], claim["attempt"]["attempt_id"])
    manager.finalize(view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["lease_token"], envelope)
    same = manager.finalize(
        view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["lease_token"], envelope
    )
    assert same["state"] == "completed"
    changed = envelope.model_copy(update={"elapsed_seconds": 0.2})
    with pytest.raises(ManagerError, match="result_conflict"):
        manager.finalize(
            view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["lease_token"], changed
        )


def test_artifact_requires_current_worker_lease_and_is_job_scoped(manager):
    register(manager)
    view, _ = manager.submit(submit())
    claim = manager.claim("worker-a", "worker-a-secret")
    body = "日本語 path content".encode()
    ref = ArtifactRef(
        artifact_id="artifact-0001",
        name="source-1.html",
        media_type="text/html",
        bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        schema_version=1,
    )
    with pytest.raises(ManagerError, match="lease_conflict"):
        manager.add_artifact(
            view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["attempt_id"], "bad", ref, body
        )
    saved = manager.add_artifact(
        view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["attempt_id"],
        claim["attempt"]["lease_token"], ref, body
    )
    assert saved == ref
    stored, path = manager.get_artifact(view["job_id"], ref.artifact_id)
    assert stored.sha256 == ref.sha256 and path.read_bytes() == body
    with pytest.raises(ManagerError, match="unknown_job"):
        manager.get_artifact("missing-job", ref.artifact_id)


def test_expired_lease_rejects_late_result_and_sweeper_marks_lost(manager):
    register(manager)
    view, _ = manager.submit(submit())
    claim = manager.claim("worker-a", "worker-a-secret")
    manager._conn.execute(
        "UPDATE attempts SET lease_expires_at=? WHERE attempt_id=?",
        (time.time() - 1, claim["attempt"]["attempt_id"]),
    )
    manager._conn.commit()
    envelope = result(view["job_id"], claim["attempt"]["attempt_id"])
    with pytest.raises(ManagerError, match="lease_expired"):
        manager.finalize(view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["lease_token"], envelope)
    assert manager.sweep_once()["lost"] == 1
    assert manager.job_view(view["job_id"])["state"] == "lost"
    with pytest.raises(ManagerError, match="no_remaining_budget"):
        manager.retry(view["job_id"])


def test_cancel_request_is_not_confirmed_by_lease_expiry(manager):
    register(manager)
    view, _ = manager.submit(submit(key="cancel-unconfirmed-0001"))
    claim = manager.claim("worker-a", "worker-a-secret")
    manager.cancel(view["job_id"])
    manager._conn.execute(
        "UPDATE attempts SET lease_expires_at=? WHERE attempt_id=?",
        (time.time() - 1, claim["attempt"]["attempt_id"]),
    )
    manager._conn.commit()
    swept = manager.sweep_once()
    assert swept["cancel_unconfirmed"] == 1
    assert manager.job_view(view["job_id"])["state"] == "cancel_requested"


def test_manager_restart_restores_job_and_result(tmp_path, monkeypatch):
    monkeypatch.setenv("EPHY_TEST_REQUESTER", "requester-secret")
    monkeypatch.setenv("EPHY_TEST_WORKER_A", "worker-a-secret")
    settings = ManagerSettings(
        database_path=tmp_path / "manager.db",
        artifact_dir=tmp_path / "artifacts",
        requester_credential_env="EPHY_TEST_REQUESTER",
        worker_credentials_env={"worker-a": "EPHY_TEST_WORKER_A"},
    )
    first = Manager(settings)
    register(first)
    view, _ = first.submit(submit())
    claim = first.claim("worker-a", "worker-a-secret")
    envelope = result(view["job_id"], claim["attempt"]["attempt_id"])
    first.finalize(view["job_id"], "worker-a", "worker-a-secret", claim["attempt"]["lease_token"], envelope)
    first.close()
    second = Manager(settings)
    try:
        assert second.job_view(view["job_id"])["state"] == "completed"
        assert second.job_result(view["job_id"])["job_id"] == view["job_id"]
    finally:
        second.close()


def test_queued_cancel_is_terminal_and_idempotent(manager):
    view, _ = manager.submit(submit())
    assert manager.cancel(view["job_id"])["state"] == "cancelled"
    assert manager.cancel(view["job_id"])["state"] == "cancelled"


def test_credentials_never_appear_in_database(manager):
    register(manager)
    manager.submit(submit())
    dump = "\n".join(manager._conn.iterdump())
    assert "worker-a-secret" not in dump
    assert os.environ["EPHY_TEST_REQUESTER"] not in dump
