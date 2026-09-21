"""同一PCの別processでManager ↔ model-less WorkerのHTTP契約を検証する．"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import yaml

from ephy_worker.config import SearchConfig
from ephy_worker.contracts import CONTRACT_VERSION, JOB_KIND, SubmitJob
from ephy_worker.manager_client import ManagerClient
from ephy_worker.schema import Candidate, Limits, Passage, Source
from ephy_worker.worker_service import WorkerRunner, WorkerService


class ProcessFixtureSearch:
    def __init__(self):
        self.last_diagnostic = {"engine": "process-fixture", "result_count": 1}

    async def search(self, text: str, query_id: str) -> list[Candidate]:
        return [
            Candidate(
                url="https://example.org/process-fixture",
                title=f"fixture for {text}",
                query_id=query_id,
                engine="process-fixture",
                rank=1,
            )
        ]

    async def aclose(self) -> None:
        return None


class ProcessFixtureFetcher:
    async def fetch(self, candidate: Candidate, source_id: str) -> Source:
        text = "別processの収集Workerが返した公開fixture本文です．"
        return Source(
            source_id=source_id,
            requested_url=candidate.url,
            final_url=candidate.url,
            title=candidate.title,
            kind="html",
            media_type="text/html",
            http_status=200,
            status="ok",
            bytes_received=len(text.encode("utf-8")),
            passages=[Passage(passage_id=f"{source_id}-P1", source_id=source_id, text=text)],
        )

    async def aclose(self) -> None:
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(endpoint: str, process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("manager_exited")
        try:
            with urlopen(f"{endpoint}/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("manager_start_timeout")


async def _worker(endpoint: str) -> int:
    credential = os.environ["EPHY_PROCESS_WORKER"]
    service = WorkerService(
        ManagerClient(endpoint, credential),
        "process-worker",
        search_config=SearchConfig(base_url="http://127.0.0.1:9", engine="fixture"),
        search=ProcessFixtureSearch(),
        fetcher=ProcessFixtureFetcher(),
        poll_interval=0.05,
        heartbeat_interval=1.0,
    )
    return await WorkerRunner(service).run()


async def _wait_terminal(client: ManagerClient, job_id: str) -> dict:
    for _ in range(300):
        view = await client.get_job(job_id)
        if view["state"] in {"completed", "partial", "failed", "cancelled", "lost"}:
            return view
        await asyncio.sleep(0.05)
    raise RuntimeError("job_timeout")


def _submit(mode: str, key: str) -> SubmitJob:
    input_value = (
        {
            "mode": "search",
            "topic": "別process検証",
            "round_index": 0,
            "queries": [
                {
                    "query_id": "Q1",
                    "text": "public process fixture",
                    "role": "primary",
                    "reason": "HTTP process境界を確認する",
                }
            ],
        }
        if mode == "search"
        else {
            "mode": "extract",
            "topic": "別process検証",
            "round_index": 0,
            "candidates": [
                {
                    "url": "https://example.org/process-fixture",
                    "title": "process fixture",
                    "query_id": "Q1",
                    "engine": "process-fixture",
                }
            ],
        }
    )
    return SubmitJob(
        contract=CONTRACT_VERSION,
        submit_key=key,
        parent_job_id=None,
        kind=JOB_KIND,
        input=input_value,
        target_worker_id="process-worker",
        budget=Limits(),
        deadline_seconds=300,
    )


async def _exercise(endpoint: str, credential: str) -> dict:
    client = ManagerClient(endpoint, credential)
    try:
        while True:
            workers = await client.list_workers()
            if any(worker["worker_id"] == "process-worker" for worker in workers):
                break
            await asyncio.sleep(0.05)
        results = {}
        for mode in ("search", "extract"):
            submitted = await client.submit_job(
                _submit(mode, f"process-{mode}-0001").model_dump(mode="json")
            )
            view = await _wait_terminal(client, submitted["job_id"])
            if view["state"] != "completed":
                raise RuntimeError(f"{mode}_{view['state']}")
            response = await client.job_result(view["job_id"])
            envelope = response["result"]
            if envelope["usage"]["model_requests"] != 0:
                raise RuntimeError("unexpected_model_request")
            if not any(ref["name"] == "result-pack.json" for ref in envelope["manifest"]):
                raise RuntimeError("result_pack_missing")
            results[mode] = {
                "job_id": view["job_id"],
                "state": view["state"],
                "model_requests": envelope["usage"]["model_requests"],
            }
        return results
    finally:
        await client.aclose()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _orchestrate() -> int:
    repo = Path(__file__).resolve().parents[1]
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["EPHY_PROCESS_REQUESTER"] = "process-requester-fixture"
    environment["EPHY_PROCESS_WORKER"] = "process-worker-fixture"
    environment["PYTHONPATH"] = str(repo / "src")
    with tempfile.TemporaryDirectory(prefix="ephy-phase2-process-") as temp_name:
        temp = Path(temp_name)
        config = temp / "manager.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "manager": {
                        "host": "127.0.0.1",
                        "port": port,
                        "database_path": str(temp / "manager.db"),
                        "artifact_dir": str(temp / "artifacts"),
                        "requester_credential_env": "EPHY_PROCESS_REQUESTER",
                        "worker_credentials_env": {"process-worker": "EPHY_PROCESS_WORKER"},
                        "lease_ttl_seconds": 5,
                        "worker_offline_seconds": 10,
                    }
                }
            ),
            encoding="utf-8",
        )
        manager = subprocess.Popen(
            [sys.executable, "-m", "ephy_worker", "manager", "run", "--config", str(config)],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        worker = None
        try:
            _wait_health(endpoint, manager)
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", "--endpoint", endpoint],
                cwd=repo,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            result = asyncio.run(_exercise(endpoint, environment["EPHY_PROCESS_REQUESTER"]))
            print(json.dumps({"ok": True, "processes": 3, "results": result}, ensure_ascii=False))
            return 0
        finally:
            if worker is not None:
                _stop(worker)
            _stop(manager)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--endpoint")
    args = parser.parse_args()
    if args.worker:
        if not args.endpoint:
            parser.error("--endpoint is required with --worker")
        return asyncio.run(_worker(args.endpoint))
    return _orchestrate()


if __name__ == "__main__":
    raise SystemExit(main())
