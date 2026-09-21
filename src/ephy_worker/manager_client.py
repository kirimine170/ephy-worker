"""manager への HTTP 通信（research 側と worker 側の共通 client，契約 0.4）．

credential は `X-Ephy-Credential` header のみで送信し，ログ・例外・result pack
には載せない．worker 識別は `X-Ephy-Worker-Id`，attempt／lease は専用 header
で送る．エラーレスポンスは code のみ安全に保持し，body は例外 message に載せない．
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import aiohttp


class ManagerAPIError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        self.status = status
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class ManagerClient:
    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        timeout: float = 30.0,
        session: aiohttp.ClientSession | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._owns_session = session is None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60.0))
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed and self._owns_session:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        worker: tuple[str, str] | None = None,
        lease: tuple[str, str] | None = None,
        artifact: dict[str, str] | None = None,
        json_body: Any = None,
        data: bytes | None = None,
    ) -> Any:
        session = await self._ensure()
        url = self.base_url + path
        headers = {"X-Ephy-Credential": self.credential}
        if worker is not None:
            headers["X-Ephy-Worker-Id"] = worker[0]
        if lease is not None:
            headers["X-Ephy-Attempt-Id"] = lease[0]
            headers["X-Ephy-Lease-Token"] = lease[1]
        if artifact is not None:
            headers.update(artifact)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        async with session.request(method, url, data=data, headers=headers, timeout=self.timeout) as response:
            body = await response.read()
            if response.status >= 400:
                code = _error_code(body)
                if response.status in (401, 403):
                    raise ManagerAPIError(response.status, code, "authentication failed")
                raise ManagerAPIError(response.status, code)
            if not body:
                return None
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type:
                return json.loads(body.decode("utf-8"))
            return body

    # ------------------------------------------------------------------ requester

    async def submit_job(self, payload: dict) -> dict:
        return await self._request("POST", "/jobs", json_body=payload)

    async def get_job(self, job_id: str) -> dict:
        return await self._request("GET", f"/jobs/{quote(job_id, safe='')}")

    async def job_result(self, job_id: str) -> dict:
        return await self._request("GET", f"/jobs/{quote(job_id, safe='')}/result")

    async def cancel_job(self, job_id: str) -> dict:
        return await self._request("POST", f"/jobs/{quote(job_id, safe='')}/cancel")

    async def retry_job(self, job_id: str, submit_key: str | None = None) -> dict:
        return await self._request("POST", f"/jobs/{quote(job_id, safe='')}/retry", json_body={"submit_key": submit_key} if submit_key else {})

    async def delete_job(self, job_id: str) -> dict:
        return await self._request("DELETE", f"/jobs/{quote(job_id, safe='')}")

    async def list_workers(self) -> list:
        return await self._request("GET", "/workers")

    async def download_artifact(self, job_id: str, artifact_id: str) -> tuple[bytes, str, int]:
        session = await self._ensure()
        url = f"{self.base_url}/jobs/{quote(job_id, safe='')}/artifacts/{quote(artifact_id, safe='')}"
        headers = {"X-Ephy-Credential": self.credential}
        async with session.get(url, headers=headers, timeout=self.timeout) as response:
            if response.status >= 400:
                body = await response.read()
                if response.status in (401, 403):
                    raise ManagerAPIError(response.status, "unauthorized", "authentication failed")
                raise ManagerAPIError(response.status, _error_code(body))
            data = await response.read()
            return data, response.headers.get("Content-Type", ""), int(response.headers.get("Content-Length", "0") or len(data))

    # -------------------------------------------------------------------- worker

    async def register_worker(self, worker_id: str, registration: dict) -> dict:
        return await self._request("POST", "/workers/register", worker=(worker_id, self.credential), json_body=registration)

    async def claim_job(self, worker_id: str) -> dict | None:
        result = await self._request("POST", "/jobs/claim", worker=(worker_id, self.credential))
        if not result or result.get("job") is None:
            return None
        return result

    async def renew_lease(self, worker_id: str, job_id: str, attempt_id: str, lease_token: str) -> dict:
        return await self._request(
            "POST",
            f"/jobs/{quote(job_id, safe='')}/lease",
            worker=(worker_id, self.credential),
            lease=(attempt_id, lease_token),
        )

    async def finalize_job(self, worker_id: str, job_id: str, lease_token: str, result_envelope: dict) -> dict:
        return await self._request(
            "POST",
            f"/jobs/{quote(job_id, safe='')}/finalize",
            worker=(worker_id, self.credential),
            lease=("", lease_token),
            json_body=result_envelope,
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
        headers = {
            "X-Ephy-Attempt-Id": attempt_id,
            "X-Ephy-Artifact-Id": artifact_id,
            "X-Ephy-Artifact-Name": name,
            "X-Ephy-Artifact-Media-Type": media_type,
            "X-Ephy-Artifact-SHA256": sha256,
            "X-Ephy-Artifact-Bytes": str(len(content)),
            "X-Ephy-Schema-Version": str(schema_version),
        }
        return await self._request(
            "POST",
            f"/jobs/{quote(job_id, safe='')}/artifacts",
            worker=(worker_id, self.credential),
            lease=(attempt_id, lease_token),
            artifact=headers,
            data=content,
        )


def _error_code(body: bytes) -> str:
    """エラー body から code を安全に取り出す（不正 form は generic code）．"""
    try:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return error["code"][:128]
            if isinstance(error, str):
                return error[:128]
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        pass
    return "request_failed"
