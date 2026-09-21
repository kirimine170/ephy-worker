from __future__ import annotations

import hashlib
import json

import aiohttp
import pytest
from aiohttp import web

from ephy_worker.config import ManagerSettings
from ephy_worker.contracts import CONTRACT_VERSION, JOB_KIND, WorkerRegistration
from ephy_worker.manager import ManagerApp
from ephy_worker.manager_client import ManagerAPIError, ManagerClient
from ephy_worker.schema import Limits


async def _start_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("EPHY_HTTP_REQUESTER", "requester-secret")
    monkeypatch.setenv("EPHY_HTTP_WORKER", "worker-secret")
    settings = ManagerSettings(
        port=8321,
        database_path=tmp_path / "manager.db",
        artifact_dir=tmp_path / "artifacts",
        requester_credential_env="EPHY_HTTP_REQUESTER",
        worker_credentials_env={"worker-http": "EPHY_HTTP_WORKER"},
    )
    manager_app = ManagerApp(settings)
    runner = web.AppRunner(manager_app.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return manager_app, runner, f"http://127.0.0.1:{port}"


def _submit() -> dict:
    return {
        "contract": CONTRACT_VERSION,
        "submit_key": "http-submit-0001",
        "parent_job_id": None,
        "kind": JOB_KIND,
        "input": {
            "mode": "search",
            "topic": "公開仕様",
            "round_index": 0,
            "queries": [
                {
                    "query_id": "Q1",
                    "text": "public specification",
                    "role": "overview",
                    "reason": "HTTP契約を確認する",
                }
            ],
        },
        "target_worker_id": "worker-http",
        "budget": Limits().model_dump(),
        "deadline_seconds": 600,
    }


async def test_http_app_auth_submit_claim_and_error_mapping(tmp_path, monkeypatch):
    _app, runner, endpoint = await _start_manager(tmp_path, monkeypatch)
    requester = ManagerClient(endpoint, "requester-secret")
    unauthorized = ManagerClient(endpoint, "wrong")
    worker = ManagerClient(endpoint, "worker-secret")
    try:
        with pytest.raises(ManagerAPIError, match="unauthorized"):
            await unauthorized.list_workers()
        registration = WorkerRegistration(
            contract=CONTRACT_VERSION,
            worker_id="worker-http",
            capabilities=["web.collect"],
            implementation_version="test",
            os="test-os",
            architecture="test-arch",
            concurrency=1,
        )
        registered = await worker.register_worker("worker-http", registration.model_dump())
        assert registered["worker_id"] == "worker-http"
        submitted = await requester.submit_job(_submit())
        claimed = await worker.claim_job("worker-http")
        assert claimed["job"]["job_id"] == submitted["job_id"]
        assert claimed["attempt"]["worker_id"] == "worker-http"
    finally:
        await requester.aclose()
        await unauthorized.aclose()
        await worker.aclose()
        await runner.cleanup()


async def test_http_artifact_rejects_declared_size_mismatch(tmp_path, monkeypatch):
    _app, runner, endpoint = await _start_manager(tmp_path, monkeypatch)
    requester = ManagerClient(endpoint, "requester-secret")
    worker = ManagerClient(endpoint, "worker-secret")
    try:
        registration = WorkerRegistration(
            contract=CONTRACT_VERSION,
            worker_id="worker-http",
            capabilities=["web.collect"],
            implementation_version="test",
            os="test-os",
            architecture="test-arch",
            concurrency=1,
        )
        await worker.register_worker("worker-http", registration.model_dump())
        submitted = await requester.submit_job(_submit())
        claimed = await worker.claim_job("worker-http")
        body = b"artifact body"
        headers = {
            "X-Ephy-Credential": "worker-secret",
            "X-Ephy-Worker-Id": "worker-http",
            "X-Ephy-Attempt-Id": claimed["attempt"]["attempt_id"],
            "X-Ephy-Lease-Token": claimed["attempt"]["lease_token"],
            "X-Ephy-Artifact-Id": "artifact-http-0001",
            "X-Ephy-Artifact-Name": "result.json",
            "X-Ephy-Artifact-Media-Type": "application/json",
            "X-Ephy-Artifact-SHA256": hashlib.sha256(body).hexdigest(),
            "X-Ephy-Artifact-Bytes": str(len(body) + 1),
            "X-Ephy-Schema-Version": "1",
        }
        async with aiohttp.ClientSession() as session, session.post(
            f"{endpoint}/jobs/{submitted['job_id']}/artifacts",
            data=body,
            headers=headers,
        ) as response:
            payload = json.loads(await response.text())
            assert response.status == 409
            assert payload["error"]["code"] == "artifact_size_mismatch"
    finally:
        await requester.aclose()
        await worker.aclose()
        await runner.cleanup()
