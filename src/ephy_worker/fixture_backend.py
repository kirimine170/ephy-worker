"""Networkを使わずmanager，worker，remote collectorを結合するtest backend．"""

from __future__ import annotations

import asyncio
import os

from .config import ManagerSettings, SearchConfig
from .contracts import ArtifactRef, ResultEnvelope, SubmitJob, WorkerRegistration
from .manager import Manager, ManagerError
from .manager_client import ManagerAPIError
from .worker_service import WorkerService


class InProcessManagerClient:
    """ManagerClientと同じinterfaceでManager coreを直接呼ぶ．"""

    def __init__(self, manager: Manager, credential: str):
        self.manager = manager
        self.credential = credential

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ManagerError as exc:
            raise ManagerAPIError(exc.status, exc.code) from exc

    async def aclose(self) -> None:
        return None

    async def submit_job(self, payload: dict) -> dict:
        try:
            submit = SubmitJob.model_validate(payload)
        except Exception as exc:
            raise ManagerAPIError(422, "invalid_payload") from exc
        view, created = self._call(self.manager.submit, submit)
        return {**view, "created": created}

    async def get_job(self, job_id: str) -> dict:
        return self._call(self.manager.job_view, job_id)

    async def job_result(self, job_id: str) -> dict:
        view = self._call(self.manager.job_view, job_id)
        return {
            "job_id": job_id,
            "state": view["state"],
            "stop_reason": view["stop_reason"],
            "usage": view["usage"],
            "result": view["result"],
        }

    async def cancel_job(self, job_id: str) -> dict:
        return self._call(self.manager.cancel, job_id)

    async def list_workers(self) -> list[dict]:
        return self._call(self.manager.list_workers)

    async def download_artifact(self, job_id: str, artifact_id: str) -> tuple[bytes, str, int]:
        ref, path = self._call(self.manager.get_artifact, job_id, artifact_id)
        return path.read_bytes(), ref.media_type, ref.bytes

    async def register_worker(self, worker_id: str, body: dict) -> dict:
        try:
            registration = WorkerRegistration.model_validate(body)
        except Exception as exc:
            raise ManagerAPIError(422, "invalid_payload") from exc
        return self._call(self.manager.upsert_worker, worker_id, self.credential, registration)

    async def claim_job(self, worker_id: str) -> dict | None:
        return self._call(self.manager.claim, worker_id, self.credential)

    async def renew_lease(
        self, worker_id: str, job_id: str, attempt_id: str, lease_token: str
    ) -> dict:
        return self._call(
            self.manager.renew_lease,
            job_id,
            worker_id,
            self.credential,
            attempt_id,
            lease_token,
        )

    async def upload_artifact(
        self,
        worker_id: str,
        job_id: str,
        attempt_id: str,
        lease_token: str,
        artifact_id: str,
        name: str,
        media_type: str,
        content: bytes,
        sha256: str,
        schema_version: int,
    ) -> dict:
        ref = ArtifactRef(
            artifact_id=artifact_id,
            name=name,
            media_type=media_type,
            bytes=len(content),
            sha256=sha256,
            schema_version=schema_version,
        )
        saved = self._call(
            self.manager.add_artifact,
            job_id,
            worker_id,
            self.credential,
            attempt_id,
            lease_token,
            ref,
            content,
        )
        return saved.model_dump(mode="json")

    async def finalize_job(
        self, worker_id: str, job_id: str, lease_token: str, result: dict
    ) -> dict:
        try:
            envelope = ResultEnvelope.model_validate(result)
        except Exception as exc:
            raise ManagerAPIError(422, "invalid_payload") from exc
        return self._call(
            self.manager.finalize,
            job_id,
            worker_id,
            self.credential,
            lease_token,
            envelope,
        )


class FixtureBackend:
    """同一event loop内のmanagerとworkerを管理する．"""

    def __init__(
        self,
        tmp_path,
        *,
        search_factory=None,
        fetcher_factory=None,
        lease_ttl_seconds: float = 30.0,
    ):
        self.search_factory = search_factory
        self.fetcher_factory = fetcher_factory
        os.environ["EPHY_TEST_REQUESTER"] = "requester-secret"
        os.environ["EPHY_TEST_WORKER_A_CREDENTIAL"] = "worker-a-secret"
        os.environ["EPHY_TEST_WORKER_B_CREDENTIAL"] = "worker-b-secret"
        settings = ManagerSettings(
            database_path=tmp_path / "manager.db",
            artifact_dir=tmp_path / "artifacts",
            requester_credential_env="EPHY_TEST_REQUESTER",
            worker_credentials_env={
                "worker-a": "EPHY_TEST_WORKER_A_CREDENTIAL",
                "worker-b": "EPHY_TEST_WORKER_B_CREDENTIAL",
            },
            lease_ttl_seconds=lease_ttl_seconds,
        )
        self.manager = Manager(settings)
        self.client = InProcessManagerClient(self.manager, "requester-secret")
        self._workers: list[WorkerService] = []
        self._tasks: list[asyncio.Task] = []

    async def start_worker(
        self,
        worker_id: str = "worker-a",
        *,
        search=None,
        fetcher=None,
        poll_interval: float = 0.02,
        heartbeat_interval: float = 60.0,
    ) -> WorkerService:
        client = InProcessManagerClient(self.manager, f"{worker_id}-secret")
        search_provider = search if search is not None else self.search_factory() if self.search_factory else None
        fetcher_instance = fetcher if fetcher is not None else self.fetcher_factory() if self.fetcher_factory else None
        service = WorkerService(
            client,
            worker_id,
            search_config=SearchConfig(),
            search=search_provider,
            fetcher=fetcher_instance,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
        )
        await service.register()
        self._workers.append(service)
        self._tasks.append(asyncio.create_task(service.run()))
        return service

    async def stop(self) -> None:
        for worker in self._workers:
            worker.request_stop()
        if self._tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=5.0)
            except TimeoutError:
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
        self.manager.close()
