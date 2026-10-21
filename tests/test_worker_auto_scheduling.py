import threading
import time

import pytest
from pydantic import ValidationError

from ephy_worker.config import ManagerSettings
from ephy_worker.contracts import SubmitJob, WorkerRegistration
from ephy_worker.manager import Manager
from ephy_worker.schema import Limits


@pytest.fixture
def manager(tmp_path, monkeypatch):
    for worker in ("a", "b"):
        monkeypatch.setenv("AUTO_WORKER_" + worker.upper(), worker + "-secret")
    value = Manager(ManagerSettings(database_path=tmp_path / "manager.db", artifact_dir=tmp_path / "artifacts",
        worker_credentials_env={"worker-a": "AUTO_WORKER_A", "worker-b": "AUTO_WORKER_B"}))
    yield value
    value.close()


def register(manager, worker="worker-a", *, concurrency=1, capabilities=None):
    manager.upsert_worker(worker, worker.removeprefix("worker-") + "-secret", WorkerRegistration(
        contract="0.4", worker_id=worker, capabilities=capabilities or ["web.collect"],
        implementation_version="fixture", os="fixture", architecture="fixture", concurrency=concurrency))


def submit(key, *, target="auto"):
    return SubmitJob(contract="0.4", submit_key=key, kind="web.collect", target_worker_id=target,
        input={"mode": "search", "topic": "public", "round_index": 0, "queries": [
            {"query_id": "Q1", "text": "public specification", "role": "overview", "reason": "fixture"}]},
        budget=Limits(), deadline_seconds=600)


def test_auto_concurrent_claims_distribute_once_to_two_workers(manager):
    register(manager)
    register(manager, "worker-b")
    ids = {manager.submit(submit(f"auto-{n}"))[0]["job_id"] for n in range(2)}
    results = []
    def poll(worker):
        for _ in range(100):
            claimed = manager.claim(worker, worker[-1] + "-secret")
            if claimed:
                results.append(claimed)
                return
            time.sleep(0.01)
    threads = [threading.Thread(target=poll, args=(w,)) for w in ("worker-a", "worker-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {r["job"]["job_id"] for r in results if r} == ids
    assert {r["attempt"]["worker_id"] for r in results if r} == {"worker-a", "worker-b"}


def test_auto_load_then_fairness_and_explicit_target_unchanged(manager):
    register(manager, concurrency=2)
    register(manager, "worker-b", concurrency=2)
    manager.submit(submit("assigned", target="worker-a"))
    assert manager.claim("worker-a", "a-secret")
    manager.submit(submit("automatic"))
    assert manager.claim("worker-a", "a-secret") is None
    automatic = manager.claim("worker-b", "b-secret")
    assert automatic["job"]["target_worker_id"] == "auto"
    manager._conn.execute("UPDATE jobs SET state='completed'")
    manager._conn.commit()
    manager.submit(submit("fairness"))
    assert manager.claim("worker-b", "b-secret") is None
    assert manager.claim("worker-a", "a-secret")
    explicit = manager.submit(submit("explicit", target="worker-b"))[0]
    assert manager.claim("worker-a", "a-secret") is None
    assert manager.claim("worker-b", "b-secret")["job"]["job_id"] == explicit["job_id"]


def test_stale_offline_contract_and_scope_workers_are_ineligible(manager):
    register(manager)
    register(manager, "worker-b")
    manager._conn.execute("UPDATE workers SET last_heartbeat_at=? WHERE worker_id='worker-a'", (time.time()-1000,))
    manager._conn.commit()
    manager.submit(submit("fresh"))
    assert manager.claim("worker-a", "a-secret") is None
    assert manager.claim("worker-b", "b-secret")
    manager._conn.execute("UPDATE workers SET contract_version='old' WHERE worker_id='worker-a'")
    manager._conn.commit()
    queued = manager.submit(submit("waiting"))[0]
    assert queued["wait_reason"] == "no_eligible_worker"


def test_reserved_worker_id_rejected():
    with pytest.raises(ValidationError):
        WorkerRegistration(contract="0.4", worker_id="auto", capabilities=["web.collect"],
            implementation_version="fixture", os="fixture", architecture="fixture", concurrency=1)
