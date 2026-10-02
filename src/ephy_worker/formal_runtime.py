"""Bounded managed-Pi proposal runner, dispatched by the existing Windows job entry.

The first supported profile is data-only Markdown skill/document improvement.
It deliberately refuses executable candidates without a verifier sandbox.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import psutil

from .formal_artifacts import (
    ARTIFACTS,
    CONTROL_PATHS,
    GateFailure,
    digest,
    encode,
    file_hash,
    freeze_bundle,
    inspect_bundle,
    normalized,
    now,
    read_json,
    safe_path,
    validate_audit_result,
    write_json,
)

LEAD = "gpt-oss-20b-MXFP4"
WORKER = "Qwen3-Coder-Next-Q4_K_M"


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise GateFailure(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def snapshot(root: Path) -> dict:
    tracked = git(root, "ls-files", "-z").decode().split("\0")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    files = {}
    for name in sorted(set(tracked + untracked) - {""}):
        target = safe_path(root, name, missing=True)
        files[name] = (
            {"sha256": file_hash(target), "mode": target.stat().st_mode & 0o111} if target.is_file() else None
        )
    return files


def snapshot_hash(root: Path) -> str:
    return digest(encode(snapshot(root)))


@contextmanager
def exclusive_lock(path: Path):
    """Kernel-held singleton lock; crash releases it, metadata never grants ownership."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise GateFailure("Another formal runner owns the resource lock") from exc
    try:
        yield
    finally:
        stream.close()


def stop_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    else:
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    process.wait(timeout=30)


def validate_contract(contract: dict) -> None:
    required = {
        "allowed_files",
        "semantic_scope",
        "task",
        "checks",
        "checker",
        "checker_sha256",
        "checker_controls",
        "checker_controls_sha256",
        "max_repairs",
        "timeout_seconds",
        "stage_seconds",
        "output_token_budget",
        "max_requests",
        "max_response_tokens",
        "minimum_free_ram_bytes",
        "minimum_free_disk_bytes",
        "max_process_rss_bytes",
        "max_log_bytes",
        "runtime_hashes",
    }
    if set(contract) != required:
        raise GateFailure("Frozen formal contract fields missing or unknown")
    if not contract["allowed_files"] or len(set(contract["allowed_files"])) != len(contract["allowed_files"]):
        raise GateFailure("Empty/duplicate file scope")
    for name in contract["allowed_files"]:
        normalized(name)
        if not name.endswith(".md") or not name.startswith(("docs/", ".agents/skills/")):
            raise GateFailure(
                "Only data-only Markdown profile is supported; executable proposals need isolation"
            )
    for field in ("task", "semantic_scope"):
        if not isinstance(contract[field], str) or not contract[field].strip():
            raise GateFailure("Empty task/semantic scope")
    if not 0 <= contract["max_repairs"] <= 2:
        raise GateFailure("Repair limit must be 0..2")
    for field in (
        "timeout_seconds",
        "stage_seconds",
        "output_token_budget",
        "max_requests",
        "max_response_tokens",
        "minimum_free_ram_bytes",
        "minimum_free_disk_bytes",
        "max_process_rss_bytes",
        "max_log_bytes",
    ):
        if type(contract[field]) is not int or contract[field] <= 0:
            raise GateFailure(f"Invalid positive integer: {field}")
    checks = contract["checks"]
    if not isinstance(checks, list) or {c.get("id") for c in checks} != {
        "target",
        "regression",
        "lint",
        "repository",
        "fixed",
    }:
        raise GateFailure("All five independent checks must be fixed")
    for check in checks:
        if set(check) != {"id", "argv", "baseline_exit_code"} or not check["argv"]:
            raise GateFailure("Invalid check definition")
        if not all(isinstance(arg, str) and arg for arg in check["argv"]):
            raise GateFailure("Checks must be argv arrays, never shell strings")
        if type(check["baseline_exit_code"]) is not int:
            raise GateFailure("Expected baseline exit code missing")


def invocation_identity(runtime: dict, contract: dict, role: str) -> str:
    return digest(
        encode(
            {
                "model": WORKER if role == "implementer" else LEAD,
                "role": role,
                "thinking": "off" if role == "implementer" else "medium",
                "runtime_files": {
                    name: file_hash(Path(runtime[name]))
                    for name in ("pi", "server", "models_ini", "provider", "stage_guard", "governance_gate")
                },
                "limits": {
                    name: contract[name]
                    for name in (
                        "stage_seconds",
                        "output_token_budget",
                        "max_requests",
                        "max_response_tokens",
                    )
                },
            }
        )
    )


def stage_evidence(events: list[dict], model: str, role: str) -> dict:
    if any(event.get("kind") == "violation" for event in events):
        raise GateFailure("Formal stage tool/model/budget violation")
    requests = [e for e in events if e.get("kind") == "provider_request"]
    assistants = [e for e in events if e.get("kind") == "assistant"]
    acks = [e for e in events if e.get("kind") == "governance_result"]
    ends = [e for e in events if e.get("kind") == "stage_end"]
    if (
        not requests
        or not assistants
        or not acks
        or len(ends) != 1
        or ends[0]["failed"]
        or any(e["model"] != model for e in requests + assistants)
        or any(e["stopReason"] in ("error", "aborted", "length") for e in assistants)
    ):
        raise GateFailure("Incomplete or failed managed stage evidence")
    ack = acks[0]
    details = ack.get("details", {})
    if ack.get("isError") or details.get("role") != role or details.get("acknowledged") is not True:
        # The canonical gate reports the verified identity in the model-visible JSON text.
        texts = [c.get("text", "") for c in ack.get("content", []) if c.get("type") == "text"]
        try:
            identity = json.loads("".join(texts))
        except (ValueError, TypeError) as exc:
            raise GateFailure("Missing exact governance acknowledgement result") from exc
        if identity.get("role") != role or identity.get("acknowledged") is not True:
            raise GateFailure("Invalid governance acknowledgement result")
    ack_index = events.index(ack)
    if any(e.get("kind") == "tool_call" and e["tool"] != "governance_ack" for e in events[:ack_index]):
        raise GateFailure("Evidence read before acknowledgement")
    return {
        "model_id": model,
        "role": role,
        "governance_ack": ack,
        "output_tokens": ends[0]["outputTokens"],
        "requests": ends[0]["requests"],
        "trace_valid": True,
    }


class FormalRunner:
    def __init__(self, job_file: Path):
        self.job_file = job_file.resolve()
        self.job = read_json(job_file)
        self.contract = self.job["contract"]
        self.directory = Path(self.job["jobDir"]).resolve()
        self.candidate = Path(self.job["worktreePath"]).resolve()
        self.repository = Path(self.job["repoRoot"]).resolve()
        self.runtime = self.job["runtime"]
        self.events: list[dict] = []
        self.provenance: list[dict] = []
        self.transcripts: list[dict] = []
        self.deadline = time.monotonic() + self.contract["timeout_seconds"]
        self.contract_sha = digest(encode(self.contract))
        self.server: subprocess.Popen | None = None
        self.loaded_model: str | None = None
        self.campaign_stop: Path | None = None

    def router_request(self, route: str, body: dict | None = None) -> dict:
        with httpx.Client(trust_env=False, timeout=15) as client:
            url = self.runtime["base_url"] + route
            response = client.post(url, json=body) if body is not None else client.get(url)
            response.raise_for_status()
            return response.json()

    def start_server(self) -> None:
        import socket
        from urllib.parse import urlparse

        url = urlparse(self.runtime["base_url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path:
            raise GateFailure("Only an owned loopback model router is supported")
        for process in psutil.process_iter(["name"]):
            if (process.info["name"] or "").lower() in ("pi.exe", "llama-server.exe", "strata.exe"):
                raise GateFailure("Another Pi/model process is active; do not duplicate or stop it")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", url.port)) == 0:
                raise GateFailure("Frozen model router port is occupied")
        with (
            (self.directory / "server.stdout.log").open("xb") as out,
            (self.directory / "server.stderr.log").open("xb") as err,
        ):
            self.server = subprocess.Popen(
                [
                    self.runtime["server"],
                    "--models-preset",
                    self.runtime["models_ini"],
                    "--models-max",
                    "1",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(url.port),
                    "--no-webui",
                ],
                stdout=out,
                stderr=err,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
        self.job["serverPid"] = self.server.pid
        self.state("preparing", "Starting owned one-model router after preflight")
        until = min(self.deadline, time.monotonic() + 120)
        while time.monotonic() < until:
            self.resources()
            if self.server.poll() is not None:
                raise GateFailure("Owned model router exited during startup")
            try:
                self.router_request("/models")
                return
            except httpx.HTTPError:
                time.sleep(1)
        raise GateFailure("Owned model router startup timeout")

    def load_model(self, model: str) -> None:
        self.resources()
        if not self.server:
            self.start_server()
        if self.loaded_model == model:
            return
        if self.loaded_model:
            self.router_request("/models/unload", {"model": self.loaded_model})
        until = min(self.deadline, time.monotonic() + 600)
        stable = 0
        while time.monotonic() < until:
            self.resources()
            models = self.router_request("/models")["data"]
            if not any(entry["id"] == model for entry in models):
                raise GateFailure("Frozen model absent from router")
            if all(entry["status"]["value"] == "unloaded" for entry in models):
                stable += 1
            else:
                stable = 0
            if stable >= 3:
                break
            time.sleep(2)
        if stable < 3:
            raise GateFailure("Model unload/RAM release timeout")
        self.loaded_model = None
        self.state("running", "Loading frozen model: " + model)
        self.router_request("/models/load", {"model": model})
        until = min(self.deadline, time.monotonic() + 600)
        while time.monotonic() < until:
            self.resources()
            models = self.router_request("/models")["data"]
            entry = next(entry for entry in models if entry["id"] == model)
            if entry["status"]["value"] == "error":
                raise GateFailure("Frozen model failed to load")
            if entry["status"]["value"] == "loaded":
                if any(other["id"] != model and other["status"]["value"] != "unloaded" for other in models):
                    raise GateFailure("One-model resource boundary violated")
                self.loaded_model = model
                self.event("model loaded", model=model, router_entry=entry, server_pid=self.server.pid)
                return
            time.sleep(2)
        raise GateFailure("Frozen model load timeout")

    def state(self, status: str, message: str = "") -> None:
        self.job.update(status=status, message=message, updatedAt=now(), runnerPid=os.getpid())
        write_json(self.job_file, self.job, exclusive=False)

    def event(self, stage: str, **details: Any) -> None:
        self.events.append({"stage": stage, "at": now(), **details})
        with (self.directory / "workflow.jsonl").open("ab") as stream:
            stream.write(json.dumps(self.events[-1], ensure_ascii=False).encode() + b"\n")

    def resources(self) -> None:
        if time.monotonic() >= self.deadline:
            raise GateFailure("Whole-job deadline exhausted")
        if (self.directory / "cancel.request").exists() or (
            self.campaign_stop and self.campaign_stop.exists()
        ):
            raise GateFailure("Cancelled by controller")
        if psutil.virtual_memory().available < self.contract["minimum_free_ram_bytes"]:
            raise GateFailure("RAM headroom below frozen minimum")
        if shutil.disk_usage(self.directory).free < self.contract["minimum_free_disk_bytes"]:
            raise GateFailure("Disk headroom below frozen minimum")
        rss = psutil.Process().memory_info().rss
        for child in psutil.Process().children(recursive=True):
            try:
                rss += child.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        if rss > self.contract["max_process_rss_bytes"]:
            raise GateFailure("Owned process RAM limit exceeded")
        for name in ("server.stdout.log", "server.stderr.log"):
            path = self.directory / name
            if path.exists() and path.stat().st_size > self.contract["max_log_bytes"]:
                raise GateFailure("Model server log limit exceeded")

    def intact(self) -> None:
        current = read_json(self.job_file)
        if digest(encode(current["contract"])) != self.contract_sha:
            raise GateFailure("Frozen contract changed")
        for field in (
            "runtime",
            "controls",
            "model_identities",
            "verifier_identity",
            "baseRevision",
            "repoRoot",
            "worktreePath",
        ):
            if current[field] != self.job[field]:
                raise GateFailure("Frozen Job identity changed: " + field)
        for name, relative in CONTROL_PATHS.items():
            if (
                file_hash(safe_path(Path(self.runtime["governance_root"]), relative))
                != self.job["controls"][name]
            ):
                raise GateFailure("Canonical control changed: " + name)
        for path, expected in self.contract["runtime_hashes"].items():
            if file_hash(Path(path)) != expected:
                raise GateFailure(f"Runtime/config changed: {path}")
        for name in ("checker", "checker_controls"):
            if file_hash(Path(self.contract[name])) != self.contract[name + "_sha256"]:
                raise GateFailure(f"Fixed checker evidence changed: {name}")

    def command(self, argv: list[str], cwd: Path, label: str, seconds: int) -> dict:
        self.resources()
        self.intact()
        out, err = self.directory / (label + ".stdout.log"), self.directory / (label + ".stderr.log")
        env = os.environ.copy()
        env.update(
            PYTHONPATH=str(cwd / "src"),
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            PYTHONDONTWRITEBYTECODE="1",
            PIP_NO_INDEX="1",
            UV_OFFLINE="1",
        )
        started = now()
        with out.open("xb") as stdout, err.open("xb") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=stdout,
                stderr=stderr,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            end = min(self.deadline, time.monotonic() + seconds)
            try:
                while process.poll() is None:
                    self.resources()
                    if time.monotonic() >= end:
                        raise GateFailure("Command/stage timeout: " + label)
                    if out.stat().st_size + err.stat().st_size > self.contract["max_log_bytes"]:
                        raise GateFailure("Command output limit: " + label)
                    time.sleep(0.25)
            finally:
                stop_tree(process)
        record = {
            "argv": argv,
            "cwd": str(cwd),
            "started_at": started,
            "finished_at": now(),
            "pid": process.pid,
            "exit_code": process.returncode,
            "stdout": out.name,
            "stdout_sha256": file_hash(out),
            "stderr": err.name,
            "stderr_sha256": file_hash(err),
            "environment_sha256": self.job["environment_sha256"],
        }
        self.transcripts.append(
            {
                **record,
                "stdout_text": out.read_text(encoding="utf-8", errors="replace"),
                "stderr_text": err.read_text(encoding="utf-8", errors="replace"),
            }
        )
        self.intact()
        return record

    def run_checks(self, label: str, baseline: bool = False) -> dict:
        before = snapshot_hash(self.candidate)
        checks = []
        for check in self.contract["checks"]:
            temp = self.directory / (label + "-temp")
            temp.mkdir(exist_ok=True)
            substitutions = {
                "python": self.runtime["python"],
                "candidate": str(self.candidate),
                "checker": self.contract["checker"],
                "temp": str(temp),
            }
            argv = [arg.format(**substitutions) for arg in check["argv"]]
            result = self.command(argv, self.candidate, label + "-" + check["id"], 600)
            checks.append(
                {
                    "id": check["id"],
                    **result,
                    "passed": result["exit_code"] == (check["baseline_exit_code"] if baseline else 0),
                }
            )
        if snapshot_hash(self.candidate) != before:
            raise GateFailure("Independent verification changed candidate")
        diff = subprocess.run(
            ["git", "-C", str(self.candidate), "diff", "--check"], capture_output=True, check=False
        )
        if diff.returncode:
            checks.append({"id": "diff", "passed": False, "exit_code": diff.returncode})
        after = snapshot(self.candidate)
        changed = sorted(
            name
            for name in set(self.baseline_snapshot) | set(after)
            if self.baseline_snapshot.get(name) != after.get(name)
        )
        if set(changed) - set(self.contract["allowed_files"]):
            raise GateFailure("Candidate scope violation")
        result = {
            "baseline": baseline,
            "snapshot_sha256": before,
            "checks": checks,
            "changed_files": changed,
            "passed": all(check["passed"] for check in checks),
        }
        write_json(self.directory / (label + "-results.json"), result)
        return result

    def stage(self, role: str, prompt: str, label: str, root: Path, envelope: dict | None = None) -> str:
        self.intact()
        model = WORKER if role == "implementer" else LEAD
        self.load_model(model)
        stage_config = {
            "root": str(root),
            "role": role,
            "model_id": model,
            "allowed_files": self.contract["allowed_files"] if role == "implementer" else [],
            **{
                key: self.contract[key]
                for key in ("output_token_budget", "max_requests", "max_response_tokens")
            },
        }
        config_path = self.directory / (label + "-config.json")
        write_json(config_path, stage_config)
        task_path = self.directory / (label + "-prompt.txt")
        task_path.write_text(prompt, encoding="utf-8")
        system_path = self.directory / (label + "-system.txt")
        system = "Use the canonical development_governance section and exact governance_ack before any task."
        if envelope:
            system += "\nSYSTEM-CONTROLLED AUDIT ENVELOPE\n" + encode(envelope).decode()
        system_path.write_text(system, encoding="utf-8")
        trace = self.directory / (label + "-trace.jsonl")
        # Managed configuration is dedicated to this run; no global/auth settings or discovery.
        previous = os.environ.copy()
        os.environ.update(
            PI_CODING_AGENT_DIR=self.runtime["managed_dir"],
            PI_OFFLINE="1",
            DUAL_LLAMA_BASE_URL=self.runtime["base_url"],
            DUAL_GOVERNANCE_ROLE=role,
            DUAL_GOVERNANCE_POLICY=self.runtime["policy"],
            DUAL_GOVERNANCE_CONTEXT_ROOT=self.runtime["governance_root"],
            DUAL_JOB_RUNNER=self.runtime["runner"],
            DUAL_RUNNER_SMOKE_TEST=self.runtime["runner_test"],
            EPHY_FORMAL_STAGE_CONFIG=str(config_path),
            EPHY_FORMAL_TRACE=str(trace),
        )
        if role == "auditor":
            os.environ["DUAL_AUDIT_BUNDLE_ROOT"] = str(root)
        session = self.directory / (label + "-session.jsonl")
        if role == "implementer":
            system_path.write_text(
                system + "\n" + Path(self.runtime["worker_agent"]).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        argv = [
            self.runtime["pi"],
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--offline",
            "--provider",
            "dual-local",
            "--model",
            model,
            "--thinking",
            "off" if role == "implementer" else "medium",
            "--extension",
            self.runtime["provider"],
            "--extension",
            self.runtime["stage_guard"],
            "--extension",
            self.runtime["governance_gate"],
            "--append-system-prompt",
            str(system_path),
            "--session",
            str(session),
            "--mode",
            "json",
            "--print",
            "--approve",
            "--",
            "@" + str(task_path),
        ]
        self.event(role + " start", label=label, expected_model=model)
        try:
            result = self.command(argv, root, label, self.contract["stage_seconds"])
        finally:
            os.environ.clear()
            os.environ.update(previous)
        if result["exit_code"] != 0:
            raise GateFailure("Managed Pi stage failed: " + label)
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line]
        evidence = stage_evidence(events, model, role)
        expected_policy = self.job["controls"]["system_development_policy"]
        ack_details = evidence["governance_ack"]["details"]
        if ack_details.get("policySha256") != expected_policy:
            raise GateFailure("Observed governance policy hash mismatch")
        evidence.update(
            pid=result["pid"],
            session_path=session.name,
            session_sha256=file_hash(session),
            trace_sha256=file_hash(trace),
            identity=self.job["model_identities"][role],
        )
        self.provenance.append(evidence)
        messages = []
        for line in (self.directory / result["stdout"]).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("type") == "message_end" and event.get("message", {}).get("role") == "assistant":
                messages.append(event["message"])
        if not messages:
            raise GateFailure("No complete final assistant message")
        text = "".join(c.get("text", "") for c in messages[-1]["content"] if c.get("type") == "text")
        self.event(role + " end", label=label, pid=result["pid"], trace_sha256=evidence["trace_sha256"])
        return text

    def preflight(self) -> None:
        validate_contract(self.contract)
        self.intact()
        self.resources()
        if self.job["schemaVersion"] != 2:
            raise GateFailure("Formal runner requires schemaVersion 2")
        if self.job.get("humanAuthorization") != "explicit-execute-proposal-only":
            raise GateFailure("Explicit controller authorization missing")
        required_runtime = {
            self.runtime[name]
            for name in (
                "python",
                "pi",
                "server",
                "models_ini",
                "provider",
                "stage_guard",
                "governance_gate",
                "worker_agent",
                "runner",
                "runner_test",
                "policy",
            )
        }
        required_runtime.update(
            str(Path(__file__).with_name(name))
            for name in ("formal_runtime.py", "formal_artifacts.py", "formal_campaign.py")
        )
        if not required_runtime.issubset(self.contract["runtime_hashes"]):
            raise GateFailure("Missing mandatory executable/config pin")
        for role in ("planner", "implementer", "auditor"):
            if self.job["model_identities"][role]["invocation_config_sha256"] != invocation_identity(
                self.runtime, self.contract, role
            ):
                raise GateFailure("Frozen model invocation config mismatch")
        for model, identity in (
            (LEAD, self.job["model_identities"]["planner"]),
            (WORKER, self.job["model_identities"]["implementer"]),
            (LEAD, self.job["model_identities"]["auditor"]),
        ):
            if identity["model_id"] != model:
                raise GateFailure("Expected model/role mismatch")
            if identity["runtime_sha256"] != file_hash(Path(self.runtime["server"])):
                raise GateFailure("Expected model runtime hash mismatch")
            manifest = self.runtime["model_manifests"][model]
            if digest(encode(manifest)) != identity["model_artifact_manifest_sha256"]:
                raise GateFailure("Model artifact manifest identity mismatch")
            for entry in manifest:
                path = Path(entry["path"])
                if path.stat().st_size != entry["size_bytes"] or file_hash(path) != entry["sha256"]:
                    raise GateFailure("Model artifact changed before invocation")
        if self.candidate.exists():
            raise GateFailure("Candidate path already exists; preserve it and submit a new job")
        git(self.repository, "worktree", "add", "--detach", str(self.candidate), self.job["baseRevision"])
        if git(self.candidate, "status", "--porcelain", "--untracked-files=all").strip():
            raise GateFailure("Candidate is not clean")
        if git(self.candidate, "rev-parse", "HEAD").decode().strip() != self.job["baseRevision"]:
            raise GateFailure("Candidate base mismatch")
        self.baseline_snapshot = snapshot(self.candidate)
        for name in CONTROL_PATHS:
            data = safe_path(Path(self.runtime["governance_root"]), CONTROL_PATHS[name]).read_bytes()
            expected = self.job["controls"][name]
            if digest(data) != expected:
                raise GateFailure("Canonical control document changed: " + name)
            markers = {
                "system_development_policy": "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1",
                "audit_contract": "END-OF-EPHY-INDEPENDENT-AUDIT-CONTRACT-V1",
                "audit_prompt": "END-OF-EPHY-INDEPENDENT-AUDIT-PROMPT-V1",
            }
            if name in markers and not data.decode("utf-8").rstrip().endswith(markers[name]):
                raise GateFailure("Canonical completeness marker missing: " + name)
        for external in ("checker", "checker_controls"):
            path = Path(self.contract[external]).resolve(strict=True)
            if path.is_relative_to(self.candidate) or path.is_relative_to(self.repository):
                raise GateFailure("Checker/controls must be outside measured worktrees")
        controls = read_json(Path(self.contract["checker_controls"]))
        if {c["name"] for c in controls} != {
            "known_good",
            "unchanged",
            "wrong_answer",
            "test_weakening",
            "scope_escape",
        } or any(c["actual"] != c["expected"] for c in controls):
            raise GateFailure("Fixed checker control evidence invalid")
        env = {
            "python": self.runtime["python"],
            "python_version": sys.version,
            "dependencies": {
                name: importlib.metadata.version(name) for name in ("pytest", "ruff", "jsonschema", "psutil")
            },
            "offline": True,
            "locale": "UTF-8",
            "pythonpath": "<measured-worktree>/src",
            "temp_root": str(self.directory),
            "contract_sha256": self.contract_sha,
        }
        self.job["environment_sha256"] = digest(encode(env))
        self.environment = encode(env)
        self.state("preparing", "Checking frozen baseline before any model invocation")
        self.baseline_result = self.run_checks("baseline", baseline=True)
        if not self.baseline_result["passed"]:
            raise GateFailure("Baseline environment/checker contract failed")
        self.event(
            "preflight",
            passed=True,
            contract_sha256=self.contract_sha,
            snapshot_sha256=digest(encode(self.baseline_snapshot)),
        )

    def run(self) -> None:
        try:
            self.preflight()
            self.state("running", "Fresh gpt-oss planning session")
            before = snapshot_hash(self.candidate)
            plan = self.stage(
                "planner",
                self.contract["task"] + "\nPlan the single frozen task. Read only; no delegation or edits. "
                "Return a concrete plan. If no justified change is possible, return NO_HYPOTHESIS.",
                "planner",
                self.candidate,
            )
            if snapshot_hash(self.candidate) != before:
                raise GateFailure("Planner modified candidate")
            (self.directory / "lead-plan.txt").write_text(plan, encoding="utf-8")
            if plan.strip() == "NO_HYPOTHESIS":
                self.job["outcome"] = "no_hypothesis"
                self.state(
                    "verification_failed", "No hypothesis; recorded without implementation or adoption"
                )
                write_json(self.directory / "BACKLOG.json", {"task": self.contract["task"], "reason": plan})
                return
            seen = set()
            result: dict = {}
            for attempt in range(self.contract["max_repairs"] + 1):
                self.state(
                    "running" if attempt == 0 else "repairing", f"Qwen implementation attempt {attempt + 1}"
                )
                prompt = (
                    self.contract["task"]
                    + "\nFrozen allowed files: "
                    + json.dumps(self.contract["allowed_files"])
                    + "\nSemantic scope: "
                    + self.contract["semantic_scope"]
                    + "\nPlanner:\n"
                    + plan
                    + "\nImplement using file tools only. Do not run shell, change checks, or adopt your proposal."
                )
                if attempt:
                    prompt += "\nPrevious fixed check failures:\n" + encode(result).decode()
                self.stage("implementer", prompt, f"worker-{attempt + 1}", self.candidate)
                self.state("verifying", f"Independent frozen checks for attempt {attempt + 1}")
                result = self.run_checks(f"attempt-{attempt + 1}")
                write_json(self.directory / f"attempt-{attempt + 1}-snapshot.json", snapshot(self.candidate))
                self.event("independent verification", attempt=attempt + 1, results=result)
                if result["snapshot_sha256"] in seen:
                    raise GateFailure("Repeated candidate state; repair stopped")
                seen.add(result["snapshot_sha256"])
                if result["passed"]:
                    break
                failed = [c for c in result["checks"] if not c["passed"]]
                if any(c["id"] not in ("target", "fixed") or c["exit_code"] != 1 for c in failed):
                    raise GateFailure("Environment/regression/command failure is not a candidate repair")
            if not result["passed"]:
                self.job["outcome"] = "candidate_failed"
                self.state("verification_failed", "Candidate rejected after frozen repair limit")
                return
            if not result["changed_files"]:
                self.job["outcome"] = "not_implemented"
                self.state("verification_failed", "No implementation change; never count this as improvement")
                return
            final = snapshot(self.candidate)
            patch = git(self.candidate, "diff", "--binary", "HEAD")
            # Include new files without altering the candidate index.
            for name in result["changed_files"]:
                if name not in self.baseline_snapshot:
                    safe_path(self.candidate, name)
                    addition = subprocess.run(
                        ["git", "diff", "--no-index", "--binary", "--", os.devnull, name],
                        cwd=self.candidate,
                        capture_output=True,
                        check=False,
                    )
                    if addition.returncode not in (0, 1):
                        raise GateFailure("Cannot capture new-file patch")
                    patch += addition.stdout
            (self.directory / "candidate.patch").write_bytes(patch)
            self.event("freeze", patch_sha256=digest(patch), snapshot_sha256=digest(encode(final)))
            artifacts = {
                name: safe_path(Path(self.runtime["governance_root"]), path).read_bytes()
                for name, path in CONTROL_PATHS.items()
            }
            artifacts.update(
                task_spec=encode(self.contract),
                environment_contract=self.environment,
                preflight_result=encode(self.baseline_result),
                lead_plan=plan.encode(),
                workflow_events=encode(self.events),
                model_provenance=encode(self.provenance),
                candidate_patch=patch,
                candidate_changed_files=encode(result["changed_files"]),
                candidate_snapshot_manifest=encode(final),
                verification_plan=encode(self.contract["checks"]),
                verification_results=encode(result),
                checker_source=Path(self.contract["checker"]).read_bytes(),
                checker_control_results=Path(self.contract["checker_controls"]).read_bytes(),
                command_transcripts=encode(self.transcripts),
            )
            if set(artifacts) != set(ARTIFACTS):
                raise GateFailure("Incomplete frozen bundle")
            bundle = self.directory / "audit-bundle"
            audit_input = freeze_bundle(bundle, self.job, artifacts)
            frozen = {p.name: file_hash(p) for p in bundle.iterdir()}
            self.state("audit_pending", "Fresh read-only gpt-oss final audit")
            envelope = {
                "audit_id": audit_input["audit_id"],
                "job_id": self.job["id"],
                "bundle_root": str(bundle),
                "audit_input_schema_valid": True,
                "expected_auditor": self.job["model_identities"]["auditor"],
                "hashes": frozen,
            }
            prompt = artifacts["audit_prompt"].decode().replace("$1", str(bundle / "audit-input.json"))
            text = self.stage("auditor", prompt, "auditor", bundle, envelope)
            (self.directory / "audit-result.raw.txt").write_text(text, encoding="utf-8")
            audit_result = read_json(self.directory / "audit-result.raw.txt")
            validate_audit_result(audit_result, bundle, audit_input)
            inspect_bundle(
                bundle,
                read_json(bundle / "evidence-manifest.json"),
                json.loads(artifacts["evidence_manifest_schema"]),
            )
            unchanged = final == snapshot(self.candidate) and frozen == {
                p.name: file_hash(p) for p in bundle.iterdir()
            }
            if not unchanged:
                raise GateFailure("Candidate/bundle changed during audit")
            write_json(self.directory / "audit-result.json", audit_result)
            write_json(
                self.directory / "audit-execution-attestation.json",
                {
                    "schema_valid": True,
                    "candidate_unchanged": True,
                    "bundle_unchanged": True,
                    "observed_auditor": self.provenance[-1],
                    "audit_result_sha256": file_hash(self.directory / "audit-result.json"),
                    "audit_input_sha256": file_hash(bundle / "audit-input.json"),
                    "passed": True,
                },
            )
            self.event("proposal stop", decision=audit_result["decision"])
            self.job["outcome"] = (
                "accepted_proposal" if audit_result["decision"] == "ACCEPT_PROPOSAL" else "audit_rejected"
            )
            self.state(
                "review_ready" if audit_result["decision"] == "ACCEPT_PROPOSAL" else "verification_failed",
                "Unapplied proposal: " + audit_result["decision"],
            )
        except Exception as exc:
            self.job["outcome"] = "infrastructure_failed"
            self.state("failed", str(exc))
            write_json(
                self.directory / "runner-error.json",
                {"error": str(exc), "type": type(exc).__name__, "at": now()},
            )
            raise
        finally:
            # Retain the actual failed candidate as evidence, even before freeze/audit.
            try:
                if self.candidate.exists():
                    write_json(self.directory / "retained-candidate-snapshot.json", snapshot(self.candidate))
            except Exception as exc:
                self.state("failed", "Candidate retention failed: " + str(exc))
                write_json(self.directory / "retention-error.json", {"error": str(exc)})
                raise
            finally:
                if self.server:
                    stop_tree(self.server)


def submit(spec: dict, root: Path) -> Path:
    """Trusted controller only; stage agents cannot call this function."""
    validate_contract(spec["contract"])
    if set(spec) != {
        "repoRoot",
        "baseRevision",
        "contract",
        "runtime",
        "controls",
        "model_identities",
        "verifier_identity",
    }:
        raise GateFailure("Unexpected/missing formal submission fields")
    root.mkdir(parents=True, exist_ok=True)
    identifier = "bg-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    directory = root / "jobs" / identifier
    directory.mkdir(parents=True)
    job = {
        "schemaVersion": 2,
        "id": identifier,
        "status": "queued",
        "createdAt": now(),
        "updatedAt": now(),
        "jobDir": str(directory),
        "worktreePath": str(root / "worktrees" / identifier),
        "humanAuthorization": "explicit-execute-proposal-only",
        **spec,
    }
    if job["schemaVersion"] != 2 or job["id"] != identifier or job["jobDir"] != str(directory):
        raise GateFailure("Spec cannot replace runner job identity")
    (root / "worktrees").mkdir(exist_ok=True)
    write_json(directory / "job.json", job)
    return directory / "job.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    runner = FormalRunner(args.job)
    with exclusive_lock(Path(runner.runtime["resource_lock"])):
        runner.run()


if __name__ == "__main__":
    main()
