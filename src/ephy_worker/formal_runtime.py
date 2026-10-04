"""Bounded managed-Pi proposal runner, dispatched by the existing Windows job entry.

The first supported profile is data-only Markdown skill/document improvement.
It deliberately refuses executable candidates without a verifier sandbox.
"""

from __future__ import annotations

import argparse
import configparser
import copy
import importlib.metadata
import json
import math
import os
import re
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
    AUDIT_CHECKS,
    AUDIT_EVIDENCE,
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


def role_model(runtime: dict, role: str) -> str:
    """Model selection belongs to the frozen controller, never to a stage agent."""
    if role not in ("planner", "implementer", "auditor"):
        raise GateFailure("Unknown model role")
    roles = runtime.get("model_roles")
    if roles is None:
        return WORKER if role == "implementer" else LEAD
    if (
        not isinstance(roles, dict)
        or set(roles) != {"planner", "implementer", "auditor"}
        or any(not isinstance(value, str) or not value.strip() for value in roles.values())
    ):
        raise GateFailure("Frozen model roles must identify all three stages")
    return roles[role]


def role_thinking(runtime: dict, role: str) -> str:
    value = runtime.get("thinking", {}).get(role, "off" if role == "implementer" else "medium")
    if value not in ("off", "medium", "high"):
        raise GateFailure("Invalid frozen thinking level")
    return value


def observed_process_identity(pid: int) -> dict:
    process = psutil.Process(pid)
    executable = Path(process.exe()).resolve(strict=True)
    return {
        "pid": pid,
        "created_at": process.create_time(),
        "executable": str(executable),
        "executable_sha256": file_hash(executable),
    }


def observe_submitting_process(pid: int, executable: str, expected_sha256: str) -> dict:
    """Observe the frozen Pi caller from its actual helper-child process."""
    if type(pid) is not int or pid <= 0:
        raise GateFailure("Invalid submitting process PID")
    try:
        caller = psutil.Process(pid)
        if not any(
            parent.pid == pid and parent.create_time() == caller.create_time()
            for parent in psutil.Process().parents()
        ):
            raise GateFailure("Submitting process is not an ancestor of its observer")
        identity = observed_process_identity(pid)
        if (
            identity["executable"] != str(Path(executable).resolve(strict=True))
            or identity["executable_sha256"] != expected_sha256
        ):
            raise GateFailure("Submitting process differs from the frozen Pi executable")
        return identity
    except (psutil.Error, OSError) as exc:
        raise GateFailure("Submitting process identity cannot be observed") from exc


def injected_context_pins(runtime: dict, controls: dict) -> dict[str, str]:
    """Bind the exact files the gate resolves relative to its own module."""
    managed = Path(runtime["governance_gate"]).resolve().parent.parent
    mapping = {
        "policies/independent-audit.md": "audit_contract",
        "prompts/audit-ephy-worker.md": "audit_prompt",
        "policies/audit-input.schema.json": "audit_input_schema",
        "policies/evidence-manifest.schema.json": "evidence_manifest_schema",
        "policies/audit-result.schema.json": "audit_result_schema",
    }
    return {str(safe_path(managed, path)): controls[name] for path, name in mapping.items()}


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=60,
        check=False,
        env=command_environment(root),
    )
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
                "model": role_model(runtime, role),
                "provider": runtime.get("provider_id", "dual-local"),
                "backend": runtime.get("backend", "owned_llama"),
                "role": role,
                "thinking": role_thinking(runtime, role),
                "runtime_files": {
                    name: file_hash(Path(runtime[name]))
                    for name in ("pi", "server", "models_ini", "provider", "stage_guard", "governance_gate")
                },
                "stage_stop_sha256": file_hash(Path(runtime["stage_guard"]).with_name("formal-stage-stop.ts")),
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


def command_environment(cwd: Path, temp_root: Path | None = None) -> dict[str, str]:
    # Keep only platform/locale inputs needed by the fixed subprocess commands.
    # Pi role/session variables and Python/Git injection variables are not inputs
    # to the verifier, so planning and authorized integration use identical bytes.
    keys = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    )
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(
        PYTHONPATH=str(cwd / "src"),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        PIP_NO_INDEX="1",
        UV_OFFLINE="1",
        TEMP=str(temp_root or cwd / ".runner-temp"),
        TMP=str(temp_root or cwd / ".runner-temp"),
        TMPDIR=str(temp_root or cwd / ".runner-temp"),
    )
    return env


def executable_identity(argument: str) -> dict[str, str]:
    path = Path(argument) if Path(argument).is_absolute() else Path(shutil.which(argument) or "")
    if not path.is_file():
        raise GateFailure("Verifier executable unavailable: " + argument)
    path = path.resolve(strict=True)
    return {"path": str(path), "sha256": file_hash(path)}


def observed_verifier_identity(
    runtime: dict, contract: dict, *, observation: dict | None = None
) -> dict[str, str]:
    """Derive identity from the running controller and its actual command resolution."""
    observation = observation if observation is not None else {}
    environment = command_environment(Path("<measured-worktree>"), Path("<runner-temp>"))
    observation.update(
        phase="python_resolution",
        environment_sha256=digest(encode(environment)),
        environment_value_sha256={
            key: digest(value.encode("utf-8")) for key, value in sorted(environment.items())
        },
        checks_sha256=digest(encode(contract["checks"])),
        python_version_sha256=digest(sys.version.encode("utf-8")),
        files={},
    )

    def measure(path: Path, label: str, requested: str) -> dict[str, str]:
        observation["phase"] = label
        entry = observation["files"][label] = {"requested_path_sha256": digest(requested.encode("utf-8"))}
        path = path.resolve(strict=True)
        entry["resolved_path_sha256"] = digest(str(path).encode("utf-8"))
        before = path.stat()
        sha = file_hash(path)
        after = path.stat()
        stamps = [
            (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            for stat in (before, after)
        ]
        entry.update(
            sha256=sha,
            expected_pin_sha256=contract["runtime_hashes"].get(str(path)),
            stat_before_sha256=digest(encode(stamps[0])),
            stat_after_sha256=digest(encode(stamps[1])),
            stable=stamps[0] == stamps[1],
        )
        entry["pin_matches"] = entry["expected_pin_sha256"] == sha
        if not entry["stable"]:
            raise GateFailure("Verifier artifact changed during measurement")
        return {"path": str(path), "sha256": sha}

    def executable(argument: str, label: str) -> dict[str, str]:
        observation["phase"] = label + "_resolution"
        path = Path(argument) if Path(argument).is_absolute() else Path(shutil.which(argument) or "")
        if not path.is_file():
            raise GateFailure("Verifier executable unavailable: " + argument)
        return measure(path, label, argument)

    python = Path(sys.executable).resolve(strict=True)
    observation["python_paths"] = {
        "requested_sha256": digest(str(runtime["python"]).encode("utf-8")),
        "running_resolved_sha256": digest(str(python).encode("utf-8")),
    }
    configured_python = Path(runtime["python"]).resolve(strict=True)
    observation["python_paths"]["configured_resolved_sha256"] = digest(str(configured_python).encode("utf-8"))
    if configured_python != python:
        raise GateFailure("Configured verifier Python differs from the running interpreter")
    modules = [Path(__file__), Path(__file__).with_name("formal_artifacts.py")]
    executables = [executable(str(python), "python"), executable("git", "git")]
    for index, check in enumerate(contract["checks"]):
        argument = check["argv"][0].format(python=str(python))
        executables.append(executable(argument, "check_" + str(index)))
    pins = {item["path"]: item["sha256"] for item in executables}
    for index, path in enumerate(modules):
        item = measure(path, "module_" + str(index), str(path))
        pins[item["path"]] = item["sha256"]
    identity = {
        "executor_id": "ephy_worker.formal_runtime.independent-verifier.v1",
        "runtime_sha256": digest(encode({"files": pins, "python_version": sys.version})),
        "invocation_config_sha256": digest(
            encode(
                {
                    "checks": contract["checks"],
                    "executables": executables,
                    "environment_sha256": observation["environment_sha256"],
                    "command_timeout_seconds": 600,
                }
            )
        ),
    }
    observation.update(phase="pin_validation", observed_identity=identity)
    for path, sha in pins.items():
        if contract["runtime_hashes"].get(path) != sha:
            raise GateFailure("Actual verifier runtime lacks a matching frozen pin: " + path)
    observation["phase"] = "complete"
    return identity


def final_assistant_text(path: Path, *, session: bool = False) -> str:
    messages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == ("message" if session else "message_end"):
            message = event.get("message", {})
            if message.get("role") == "assistant":
                messages.append(message)
    if not messages or messages[-1].get("stopReason") != "stop":
        raise GateFailure("No successful final assistant message in " + path.name)
    return "".join(c.get("text", "") for c in messages[-1]["content"] if c.get("type") == "text")


def validate_observed_audit_output(directory: Path, result: dict) -> None:
    raw_path = safe_path(directory, "audit-result.raw.txt")
    raw = raw_path.read_text(encoding="utf-8")
    if (
        read_json(raw_path) != result
        or final_assistant_text(safe_path(directory, "auditor-session.jsonl"), session=True) != raw
        or final_assistant_text(safe_path(directory, "auditor.stdout.log")) != raw
    ):
        raise GateFailure("Audit result differs from the auditor's actual final output")


def read_trace(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in safe_path(directory, "auditor-trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]


def observed_audit_artifacts(bundle: Path, events: list[dict], model: str = LEAD) -> set[str]:
    """Credit actual complete tool results or byte-matching forced context only."""
    ack = stage_evidence(events, model, "auditor")["governance_ack"]
    entries = {
        entry["artifact_id"]: entry for entry in read_json(bundle / "evidence-manifest.json")["artifacts"]
    }
    observed = set()
    ack_index = events.index(ack)
    subsequent = events[ack_index + 1 :]
    # A subsequent provider request establishes that the final context gate unlocked.
    if any(event.get("kind") == "provider_request" for event in subsequent):
        if ack.get("details", {}).get("policySha256") == entries["system_development_policy"]["sha256"]:
            observed.add("system_development_policy")
        injected = {
            "audit_contract": "policies/independent-audit.md",
            "audit_prompt": "prompts/audit-ephy-worker.md",
            "audit_input_schema": "policies/audit-input.schema.json",
            "evidence_manifest_schema": "policies/evidence-manifest.schema.json",
            "audit_result_schema": "policies/audit-result.schema.json",
        }
        documents = ack.get("details", {}).get("requiredContext", [])
        content = "".join(
            item.get("text", "") for item in ack.get("content", []) if item.get("type") == "text"
        )
        for name, path in injected.items():
            data = safe_path(bundle, entries[name]["path"]).read_bytes()
            matching = [
                d
                for d in documents
                if d.get("path") == path
                and d.get("sha256") == entries[name]["sha256"]
                and d.get("bytes") == len(data)
            ]
            if len(matching) == 1:
                document = matching[0]
                block = (
                    f"BEGIN REQUIRED GOVERNANCE DOCUMENT path={path} sha256={document['sha256']} "
                    f"bytes={len(data)} lines={document['lines']}\n"
                    + data.decode("utf-8")
                    + f"\nEND REQUIRED GOVERNANCE DOCUMENT path={path} sha256={document['sha256']}"
                )
                if block in content:
                    observed.add(name)
    calls = {}
    for event in subsequent:
        if event.get("kind") == "tool_call" and event.get("tool") == "read" and not event.get("blocked"):
            calls[event.get("toolCallId")] = event.get("path")
        elif event.get("kind") == "evidence_read" and event.get("full_content_delivered") is True:
            identifier = event.get("toolCallId")
            value = calls.pop(identifier, None)
            if not isinstance(value, str) or not identifier:
                raise GateFailure("Audit read result has no matching successful call")
            path = Path(value)
            if path.is_absolute():
                try:
                    value = path.relative_to(bundle).as_posix()
                except ValueError as exc:
                    raise GateFailure("Observed audit read escaped bundle") from exc
            target = safe_path(bundle, value)
            if target != safe_path(bundle, event["path"]):
                raise GateFailure("Observed audit read path mismatch")
            for name, entry in entries.items():
                if event["path"] == entry["path"] and event.get("sha256") == entry["sha256"]:
                    observed.add(name)
    return observed


def validate_proposal_stop(
    directory: Path, bundle: Path, result: dict, attestation: dict, auditor_model: str = LEAD
) -> None:
    """The temporal postcondition is runner-observed after audit, never guessed by it."""
    path = safe_path(directory, "post-audit-workflow.json")
    post = read_json(path)
    audit_input = read_json(bundle / "audit-input.json")
    frozen = read_json(bundle / "workflow_events.txt")
    events = post.get("events", [])
    tail = events[len(frozen) :]
    if (
        attestation.get("proposal_stopped") is not True
        or attestation.get("post_audit_workflow_sha256") != file_hash(path)
        or post.get("job_id") != audit_input["job_id"]
        or post.get("frozen_workflow_sha256") != file_hash(bundle / "workflow_events.txt")
        or post.get("audit_result_sha256") != file_hash(directory / "audit-result.json")
        or post.get("audit_trace_sha256") != file_hash(directory / "auditor-trace.jsonl")
        or not frozen
        or frozen[-1].get("stage") != "freeze"
        or frozen[-1].get("proposal_stop_required") is not True
        or frozen[-1].get("controller_sha256") != file_hash(Path(__file__))
        or events[: len(frozen)] != frozen
        or [event.get("stage") for event in tail]
        != ["model loaded", "auditor start", "auditor end", "proposal stop"]
        or tail[0].get("model") != auditor_model
        or not isinstance(tail[0].get("server_pid"), int)
        or tail[0]["server_pid"] <= 0
        or tail[0]["server_pid"] != frozen[-1].get("server_pid")
        or tail[0].get("router_entry", {}).get("id") != auditor_model
        or tail[0].get("router_entry", {}).get("status", {}).get("value") != "loaded"
        or tail[1].get("expected_model") != auditor_model
        or tail[2].get("pid") != attestation["observed_auditor"]["pid"]
        or tail[2].get("trace_sha256") != post.get("audit_trace_sha256")
        or tail[-1].get("decision") != result["decision"]
    ):
        raise GateFailure("Missing or invalid post-audit proposal stop binding")


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
    def __init__(self, job_file: Path, *, initial_job: dict | None = None):
        self.job_file = job_file.resolve()
        self.job = read_json(job_file) if initial_job is None else copy.deepcopy(initial_job)
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
        self.server_owned = True
        self.loaded_model: str | None = None
        self.campaign_stop: Path | None = None

    def reject_conflicting_processes(self, names: tuple[str, ...]) -> None:
        owner = self.job.get("submitting_process")
        if owner is not None:
            if (
                not isinstance(owner, dict)
                or set(owner) != {"pid", "created_at", "executable", "executable_sha256"}
                or type(owner["pid"]) is not int
                or owner["pid"] <= 0
                or type(owner["created_at"]) not in (int, float)
                or not math.isfinite(owner["created_at"])
                or owner["created_at"] <= 0
            ):
                raise GateFailure("Invalid submitting process identity")
            frozen_pi = str(Path(self.runtime["pi"]).resolve(strict=True))
            if (
                owner["executable"] != frozen_pi
                or owner["executable_sha256"] != self.contract["runtime_hashes"].get(frozen_pi)
                or not re.fullmatch("[a-f0-9]{64}", str(owner["executable_sha256"]))
            ):
                raise GateFailure("Submitting process lacks the frozen Pi executable pin")
        for process in psutil.process_iter(["name"]):
            name = (process.info["name"] or "").lower()
            if name not in names:
                continue
            if name == "pi.exe" and owner is not None and process.pid == owner["pid"]:
                try:
                    if observed_process_identity(process.pid) == owner:
                        continue
                except (psutil.Error, OSError) as exc:
                    raise GateFailure("Submitting Pi identity cannot be revalidated") from exc
            raise GateFailure("Another Pi/model process is active; do not duplicate or stop it")

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
        self.reject_conflicting_processes(("pi.exe", "llama-server.exe", "strata.exe"))
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
            self.verify_model_artifacts(model)
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
        # Startup/unload/RAM waits may be long; check at the actual load boundary.
        self.verify_model_artifacts(model)
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

    def verify_model_artifacts(self, model: str) -> None:
        manifest = self.runtime["model_manifests"][model]
        config = configparser.ConfigParser(interpolation=None)
        config.read_string("[__router__]\n" + Path(self.runtime["models_ini"]).read_text(encoding="utf-8"))
        first = Path(config[model]["model"]).resolve(strict=True)
        shard = re.fullmatch(r"(.+)-00001-of-(\d{5})\.gguf", first.name)
        expected = (
            [
                first.with_name(f"{shard[1]}-{index:05d}-of-{shard[2]}.gguf")
                for index in range(1, int(shard[2]) + 1)
            ]
            if shard
            else [first]
        )
        if not manifest or [Path(entry["path"]).resolve(strict=True) for entry in manifest] != expected:
            raise GateFailure("Model manifest does not cover the exact router preset/shards")
        for entry in manifest:
            self.resources()
            path = Path(entry["path"])
            if path.stat().st_size != entry["size_bytes"] or file_hash(path) != entry["sha256"]:
                raise GateFailure("Model artifact changed before/after invocation")
            self.resources()

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
        if current.get("submitting_process") != self.job.get("submitting_process"):
            raise GateFailure("Frozen submitting process identity changed")
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

    def command(
        self,
        argv: list[str],
        cwd: Path,
        label: str,
        seconds: int,
        stage_environment: dict[str, str] | None = None,
    ) -> dict:
        self.resources()
        self.intact()
        out, err = self.directory / (label + ".stdout.log"), self.directory / (label + ".stderr.log")
        temp = self.directory / "temp"
        temp.mkdir(exist_ok=True)
        env = command_environment(cwd, temp)
        if stage_environment is not None:
            env.update(stage_environment)
        executable = executable_identity(argv[0])
        argv = [executable["path"], *argv[1:]]
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
            "effective_environment_sha256": digest(encode(env)),
            "temp_root": str(temp),
            "temp_variables": {key: env[key] for key in ("TEMP", "TMP", "TMPDIR")},
            "executable": executable,
        }
        self.transcripts.append(
            {
                **record,
                "stdout_text": out.read_text(encoding="utf-8", errors="replace"),
                "stderr_text": err.read_text(encoding="utf-8", errors="replace"),
            }
        )
        self.intact()
        if executable_identity(argv[0]) != executable:
            raise GateFailure("Command executable changed during verification")
        return record

    def verifier_identity(self) -> dict:
        observation = {
            "schema": "ephy.verifier-identity-observation.v1",
            "at": now(),
            "controller_pid": os.getpid(),
            "expected_identity": self.job["verifier_identity"],
            "observed_identity": None,
        }
        try:
            observed = observed_verifier_identity(self.runtime, self.contract, observation=observation)
        except Exception as exc:
            observation.update(decision="observation_failed", failure_type=type(exc).__name__)
            self.retain_verifier_observation(observation)
            raise
        matches = observed == self.job["verifier_identity"]
        observation.update(decision="matched" if matches else "identity_mismatch", matches=matches)
        self.retain_verifier_observation(observation)
        if not matches:
            raise GateFailure("Observed independent verifier identity differs from frozen expectation")
        return observed

    def retain_verifier_observation(self, observation: dict) -> None:
        """Persist only digests before permitting checks; missing diagnostics fail closed."""
        try:
            path = safe_path(self.directory, "verifier-identity.jsonl", missing=True)
            data = json.dumps(observation, sort_keys=True).encode("utf-8") + b"\n"
            limit = min(self.contract["max_log_bytes"], 1024 * 1024)
            if (path.stat().st_size if path.exists() else 0) + len(data) > limit:
                raise GateFailure("Verifier identity diagnostic log limit exceeded")
            with path.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise GateFailure("Verifier identity diagnostic could not be retained") from exc

    def run_checks(self, label: str, baseline: bool = False) -> dict:
        verifier = self.verifier_identity()
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
            if (
                result["argv"] != [executable_identity(argv[0])["path"], *argv[1:]]
                or result["cwd"] != str(self.candidate)
                or result["effective_environment_sha256"]
                != digest(encode(command_environment(self.candidate, self.directory / "temp")))
                or result["temp_root"] != str(self.directory / "temp")
                or set(result["temp_variables"].values()) != {str(self.directory / "temp")}
            ):
                raise GateFailure("Observed verifier command differs from frozen invocation")
            checks.append(
                {
                    "id": check["id"],
                    **result,
                    "passed": result["exit_code"] == (check["baseline_exit_code"] if baseline else 0),
                }
            )
        checks.append(self.run_diff_check(label))
        if snapshot_hash(self.candidate) != before:
            raise GateFailure("Independent verification changed candidate")
        after = snapshot(self.candidate)
        changed = sorted(
            name
            for name in set(self.baseline_snapshot) | set(after)
            if self.baseline_snapshot.get(name) != after.get(name)
        )
        if set(changed) - set(self.contract["allowed_files"]):
            raise GateFailure("Candidate scope violation")
        if self.verifier_identity() != verifier:
            raise GateFailure("Independent verifier identity changed during checks")
        result = {
            "verifier_identity": verifier,
            "baseline": baseline,
            "snapshot_sha256": before,
            "checks": checks,
            "changed_files": changed,
            "passed": all(check["passed"] for check in checks),
        }
        write_json(self.directory / (label + "-results.json"), result)
        return result

    def run_diff_check(self, label: str) -> dict:
        index = safe_path(self.directory, label + "-diff.index", missing=True)
        if index.exists():
            raise GateFailure("Diff index already exists")
        index_environment = {"GIT_INDEX_FILE": str(index)}
        preparation = []
        for suffix, arguments in (
            ("base", ["read-tree", self.job["baseRevision"]]),
            ("stage", ["add", "--all", "--"]),
        ):
            record = self.command(
                ["git", "-C", str(self.candidate), *arguments],
                self.candidate,
                label + "-diff-" + suffix,
                600,
                stage_environment=index_environment,
            )
            if record["exit_code"] != 0:
                raise GateFailure("Cannot prepare complete candidate diff")
            preparation.append(record)
        index_hash = file_hash(index)
        diff = self.command(
            ["git", "-C", str(self.candidate), "diff", "--cached", "--check", self.job["baseRevision"]],
            self.candidate,
            label + "-diff",
            600,
            stage_environment=index_environment,
        )
        if file_hash(index) != index_hash:
            raise GateFailure("Diff command changed isolated index")
        return {
            "id": "diff", **diff, "passed": diff["exit_code"] == 0,
            "isolated_index": {"path": str(index), "sha256": index_hash},
            "preparation": preparation,
        }

    def freeze_patch(self, result: dict) -> bytes:
        """Capture exactly the index that passed the complete candidate diff check."""
        diff = next(check for check in result["checks"] if check["id"] == "diff")
        index = safe_path(self.directory, Path(diff["isolated_index"]["path"]).name)
        if (
            result["passed"] is not True
            or diff["passed"] is not True
            or diff["exit_code"] != 0
            or snapshot_hash(self.candidate) != result["snapshot_sha256"]
            or file_hash(index) != diff["isolated_index"]["sha256"]
        ):
            raise GateFailure("Candidate or checked index changed before patch freeze")
        record = self.command(
            ["git", "-C", str(self.candidate), "diff", "--cached", "--binary", "--full-index", self.job["baseRevision"]],
            self.candidate, "candidate-patch", 600,
            stage_environment={"GIT_INDEX_FILE": str(index)},
        )
        if (
            record["exit_code"] != 0
            or file_hash(index) != diff["isolated_index"]["sha256"]
            or snapshot_hash(self.candidate) != result["snapshot_sha256"]
        ):
            raise GateFailure("Candidate or checked index changed during patch freeze")
        return safe_path(self.directory, record["stdout"]).read_bytes()

    def stage(self, role: str, prompt: str, label: str, root: Path, envelope: dict | None = None) -> str:
        self.intact()
        model = role_model(self.runtime, role)
        self.load_model(model)
        stage_config = {
            "root": str(root),
            "role": role,
            "model_id": model,
            "provider_id": self.runtime.get("provider_id", "dual-local"),
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
        system += (
            "\nEvery file tool must use an explicit nonempty path. Use '.' for the stage root; "
            "never use an empty path. Use exact relative file paths for read/edit/write. "
            "Do not list or search files unless the frozen task needs it."
        )
        if envelope:
            system += "\nSYSTEM-CONTROLLED AUDIT ENVELOPE\n" + encode(envelope).decode()
        system_path.write_text(system, encoding="utf-8")
        trace = self.directory / (label + "-trace.jsonl")
        # Managed configuration is dedicated to this run; no global/auth settings or discovery.
        stage_environment = {
            "PI_CODING_AGENT_DIR": self.runtime["managed_dir"],
            "PI_OFFLINE": "1",
            "DUAL_LLAMA_BASE_URL": self.runtime["base_url"],
            "DUAL_GOVERNANCE_ROLE": role,
            "DUAL_GOVERNANCE_POLICY": self.runtime["policy"],
            "DUAL_GOVERNANCE_CONTEXT_ROOT": self.runtime["governance_root"],
            "DUAL_JOB_RUNNER": self.runtime["runner"],
            "DUAL_RUNNER_SMOKE_TEST": self.runtime["runner_test"],
            "EPHY_FORMAL_STAGE_CONFIG": str(config_path),
            "EPHY_FORMAL_TRACE": str(trace),
        }
        if self.runtime.get("backend") == "external_strata":
            stage_environment["EPHY_STRATA_IDENTITY"] = self.runtime["models_ini"]
        if role == "auditor":
            stage_environment["DUAL_AUDIT_BUNDLE_ROOT"] = str(root)
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
            self.runtime.get("provider_id", "dual-local"),
            "--model",
            model,
            "--thinking",
            role_thinking(self.runtime, role),
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
        result = self.command(
            argv, root, label, self.contract["stage_seconds"], stage_environment=stage_environment
        )
        if result["exit_code"] != 0:
            raise GateFailure("Managed Pi stage failed: " + label)
        self.verify_model_artifacts(model)
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
        text = final_assistant_text(self.directory / result["stdout"])
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
            for name in ("__init__.py", "formal_runtime.py", "formal_artifacts.py", "formal_campaign.py", "strata_runtime.py")
        )
        required_runtime.add(str(Path(self.runtime["stage_guard"]).with_name("formal-stage-stop.ts").resolve()))
        context_pins = injected_context_pins(self.runtime, self.job["controls"])
        context_pins[self.runtime["policy"]] = self.job["controls"]["system_development_policy"]
        required_runtime.update(context_pins)
        required_runtime.add(
            str(safe_path(Path(self.runtime["governance_root"]), "docs/self-improvement-mvp.md"))
        )
        if not required_runtime.issubset(self.contract["runtime_hashes"]):
            raise GateFailure("Missing mandatory executable/config pin")
        for path, expected in context_pins.items():
            if self.contract["runtime_hashes"][path] != expected or file_hash(Path(path)) != expected:
                raise GateFailure("Injected context differs from canonical control: " + path)
        for role in ("planner", "implementer", "auditor"):
            if self.job["model_identities"][role]["invocation_config_sha256"] != invocation_identity(
                self.runtime, self.contract, role
            ):
                raise GateFailure("Frozen model invocation config mismatch")
        for role in ("planner", "implementer", "auditor"):
            model = role_model(self.runtime, role)
            identity = self.job["model_identities"][role]
            if identity["model_id"] != model:
                raise GateFailure("Expected model/role mismatch")
            if identity["runtime_sha256"] != file_hash(Path(self.runtime["server"])):
                raise GateFailure("Expected model runtime hash mismatch")
            manifest = self.runtime["model_manifests"][model]
            if digest(encode(manifest)) != identity["model_artifact_manifest_sha256"]:
                raise GateFailure("Model artifact manifest identity mismatch")
            self.verify_model_artifacts(model)
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
            "temp_root": str(self.directory / "temp"),
            "contract_sha256": self.contract_sha,
        }
        self.job["environment_sha256"] = digest(encode(env))
        self.environment = encode(env)
        self.verifier_identity()
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
            self.state("preparing", "Validating frozen runtime before preflight")
            self.preflight()
            self.state("running", "Fresh designated-model planning session")
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
                    "running" if attempt == 0 else "repairing", f"Pi implementation attempt {attempt + 1}"
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
            patch = self.freeze_patch(result)
            (self.directory / "candidate.patch").write_bytes(patch)
            self.event(
                "freeze",
                patch_sha256=digest(patch),
                snapshot_sha256=digest(encode(final)),
                proposal_stop_required=True,
                controller_sha256=file_hash(Path(__file__)),
                server_pid=self.server.pid,
            )
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
            if self.runtime.get("review_mode", "formal") == "external_codex":
                from .strata_runtime import freeze_external_proposal

                freeze_external_proposal(self, bundle, audit_input)
                return
            if self.runtime.get("review_mode", "formal") != "formal":
                raise GateFailure("Unknown frozen review mode")
            self.state("audit_pending", "Fresh read-only designated-model final audit")
            envelope = {
                "audit_id": audit_input["audit_id"],
                "job_id": self.job["id"],
                "bundle_root": str(bundle),
                "audit_input_schema_valid": True,
                "expected_auditor": self.job["model_identities"]["auditor"],
                "hashes": frozen,
                "required_audit_checks": AUDIT_CHECKS,
                "required_audit_evidence": AUDIT_EVIDENCE,
            }
            prompt = artifacts["audit_prompt"].decode().replace("$1", str(bundle / "audit-input.json"))
            prompt += "\nEmit every fixed check ID from required_audit_checks in its domain. "
            prompt += "IDs correspond in order to every mandatory bullet of audit-contract sections A/B/C. "
            prompt += "Never omit, duplicate or invent an ID; use INCONCLUSIVE for unreadable evidence."
            prompt += " Every PASS must cite all artifact IDs in required_audit_evidence for that check, "
            prompt += "with their manifest hashes and relevant locations. Coverage does not replace reading "
            prompt += "and assessing the evidence; never invent a citation to satisfy coverage."
            prompt += (
                " Cited artifacts must have a successful full read or matching forced context delivery. "
            )
            prompt += "For W11 assess the frozen proposal-only boundary and absence of adoption so far; "
            prompt += "the controller must separately bind the actual post-audit stop before review_ready."
            text = self.stage("auditor", prompt, "auditor", bundle, envelope)
            (self.directory / "audit-result.raw.txt").write_text(text, encoding="utf-8")
            audit_result = read_json(self.directory / "audit-result.raw.txt")
            validate_observed_audit_output(self.directory, audit_result)
            events = read_trace(self.directory)
            observed_artifacts = observed_audit_artifacts(bundle, events, role_model(self.runtime, "auditor"))
            validate_audit_result(audit_result, bundle, audit_input, observed_artifacts)
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
            self.event("proposal stop", decision=audit_result["decision"])
            write_json(
                self.directory / "post-audit-workflow.json",
                {
                    "job_id": self.job["id"],
                    "events": self.events,
                    "frozen_workflow_sha256": audit_input["final_bindings"]["workflow_events_sha256"],
                    "audit_result_sha256": file_hash(self.directory / "audit-result.json"),
                    "audit_trace_sha256": file_hash(self.directory / "auditor-trace.jsonl"),
                },
            )
            write_json(
                self.directory / "audit-execution-attestation.json",
                {
                    "schema_valid": True,
                    "candidate_unchanged": True,
                    "bundle_unchanged": True,
                    "observed_auditor": self.provenance[-1],
                    "audit_result_sha256": file_hash(self.directory / "audit-result.json"),
                    "audit_result_raw_sha256": file_hash(self.directory / "audit-result.raw.txt"),
                    "audit_stdout_sha256": file_hash(self.directory / "auditor.stdout.log"),
                    "audit_input_sha256": file_hash(bundle / "audit-input.json"),
                    "passed": True,
                    "proposal_stopped": True,
                    "post_audit_workflow_sha256": file_hash(self.directory / "post-audit-workflow.json"),
                    "observed_audit_artifacts": sorted(observed_artifacts),
                },
            )
            validate_proposal_stop(
                self.directory,
                bundle,
                audit_result,
                read_json(self.directory / "audit-execution-attestation.json"),
                role_model(self.runtime, "auditor"),
            )
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
                if self.server and self.server_owned:
                    stop_tree(self.server)


def freeze_submission_identity(draft: dict, *, observation: dict | None = None) -> dict:
    """Freeze a fresh draft inside the controller that will execute it.

    This trusted submission step cannot accept an existing spec or job. Comparisons
    never call it: a later environment, command or artifact change remains a failure.
    """
    if set(draft) != {
        "repoRoot",
        "baseRevision",
        "contract",
        "runtime",
        "controls",
        "model_identities",
    }:
        raise GateFailure("Verifier identity freeze requires an unfrozen submission draft")
    spec = copy.deepcopy(draft)
    validate_contract(spec["contract"])
    spec["verifier_identity"] = observed_verifier_identity(
        spec["runtime"], spec["contract"], observation=observation
    )
    return spec


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


def verify_proposal_for_integration(job_file: Path) -> Path:
    """Read-only adoption gate outside the model Job; never infer approval from status alone."""
    runner = FormalRunner(job_file)
    if runner.runtime.get("review_mode", "formal") != "formal":
        raise GateFailure(
            "External-review proposals require separate current-head review; cannot integrate here"
        )
    validate_contract(runner.contract)
    runner.intact()
    if runner.job["status"] != "review_ready" or runner.job.get("outcome") != "accepted_proposal":
        raise GateFailure("Proposal is not accepted and review_ready")
    bundle = runner.directory / "audit-bundle"
    audit_input = read_json(bundle / "audit-input.json")
    result_file = runner.directory / "audit-result.json"
    audit_result = read_json(result_file)
    validate_observed_audit_output(runner.directory, audit_result)
    events = read_trace(runner.directory)
    observed_artifacts = observed_audit_artifacts(bundle, events, role_model(runner.runtime, "auditor"))
    validate_audit_result(audit_result, bundle, audit_input, observed_artifacts)
    if audit_result["decision"] != "ACCEPT_PROPOSAL":
        raise GateFailure("Auditor did not accept the proposal")
    inspect_bundle(
        bundle,
        read_json(bundle / "evidence-manifest.json"),
        read_json(bundle / "evidence_manifest_schema.json"),
    )
    if (
        audit_input["job_id"] != runner.job["id"]
        or audit_input["baseline_commit"] != runner.job["baseRevision"]
    ):
        raise GateFailure("Integration Job/base identity mismatch")
    attestation = read_json(runner.directory / "audit-execution-attestation.json")
    if sorted(observed_artifacts) != attestation.get("observed_audit_artifacts"):
        raise GateFailure("Integration observed audit delivery mismatch")
    validate_proposal_stop(
        runner.directory, bundle, audit_result, attestation, role_model(runner.runtime, "auditor")
    )
    observed = attestation["observed_auditor"]
    auditor_model = role_model(runner.runtime, "auditor")
    if (
        observed.get("model_id") != auditor_model
        or runner.job["model_identities"]["auditor"]["model_id"] != auditor_model
    ):
        raise GateFailure("Integration auditor model mismatch")
    events = [
        json.loads(line)
        for line in (runner.directory / "auditor-trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    trace_evidence = stage_evidence(events, auditor_model, "auditor")
    if (
        trace_evidence["governance_ack"]["details"].get("policySha256")
        != runner.job["controls"]["system_development_policy"]
    ):
        raise GateFailure("Integration auditor governance identity mismatch")
    if not all(
        attestation.get(field) is True
        for field in ("passed", "schema_valid", "candidate_unchanged", "bundle_unchanged")
    ):
        raise GateFailure("Integration execution attestation failed")
    if (
        attestation["audit_result_sha256"] != file_hash(result_file)
        or attestation["audit_result_raw_sha256"] != file_hash(runner.directory / "audit-result.raw.txt")
        or attestation["audit_stdout_sha256"] != file_hash(runner.directory / "auditor.stdout.log")
        or attestation["audit_input_sha256"] != file_hash(bundle / "audit-input.json")
        or observed["identity"] != runner.job["model_identities"]["auditor"]
        or observed["trace_sha256"] != file_hash(runner.directory / "auditor-trace.jsonl")
        or observed["session_sha256"] != file_hash(safe_path(runner.directory, observed["session_path"]))
    ):
        raise GateFailure("Integration audit execution identity mismatch")
    verification = read_json(bundle / "verification_results.txt")
    if (
        verification["verifier_identity"] != runner.verifier_identity()
        or audit_input["stage_contract"]["verifier_identity"] != verification["verifier_identity"]
    ):
        raise GateFailure("Integration independent verifier identity mismatch")
    if snapshot(runner.candidate) != read_json(bundle / "candidate_snapshot_manifest.txt"):
        raise GateFailure("Candidate changed after audit")
    patch = runner.directory / "candidate.patch"
    if file_hash(patch) != audit_input["final_bindings"]["candidate_patch_sha256"]:
        raise GateFailure("Audited patch changed before integration")
    return patch


def main(*, launch_job_bytes: bytes | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--verify-proposal-only", action="store_true")
    args = parser.parse_args()
    if args.verify_proposal_only:
        print(json.dumps({"patch": str(verify_proposal_for_integration(args.job)), "verified": True}))
        return
    if launch_job_bytes is None:
        job = read_json(args.job)
    else:
        if args.job.read_bytes() != launch_job_bytes:
            raise GateFailure("Frozen launch job bytes changed before controller entry")
        job = json.loads(launch_job_bytes.decode("utf-8"))
    dispatch_module = Path(__file__).with_name("strata_runtime.py")
    expected = job.get("contract", {}).get("runtime_hashes", {}).get(str(dispatch_module))
    if not expected or file_hash(dispatch_module) != expected:
        raise GateFailure("Strata dispatch module lacks a matching frozen pin")
    from .strata_runtime import make_runner

    runner = make_runner(args.job) if launch_job_bytes is None else make_runner(args.job, initial_job=job)
    with exclusive_lock(Path(runner.runtime["resource_lock"])):
        runner.run()


if __name__ == "__main__":
    main()
