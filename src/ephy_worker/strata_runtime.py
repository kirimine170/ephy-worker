"""Explicitly authorized use of an already running Strata service.

The service is read-only infrastructure: no launch, unload, kill or configuration
operation exists here. Markdown candidates stop pending external Codex Review.
Their deployment identity is not a claim about the bytes of weights in VRAM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil

from .formal_artifacts import (
    FINAL_BINDINGS,
    GateFailure,
    digest,
    encode,
    file_hash,
    inspect_bundle,
    read_json,
    safe_path,
    validate_schema,
    write_json,
)
from .formal_runtime import (
    FormalRunner,
    command_environment,
    executable_identity,
    role_model,
    snapshot,
    stage_evidence,
)


def validate_origin(value: str) -> int:
    try:
        url = urlparse(value)
        port = url.port
    except (TypeError, ValueError) as exc:
        raise GateFailure("Invalid Strata origin") from exc
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or not port
        or url.path
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        raise GateFailure("Strata origin must be an explicit loopback HTTP origin")
    return port


def listener_pid(port: int) -> int:
    if os.name == "nt":
        executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "netstat.exe"
        result = subprocess.run(
            [str(executable), "-ano", "-p", "TCP"],
            capture_output=True,
            check=True,
            timeout=5,
        )
        matches = re.findall(
            rf"(?m)^\s*TCP\s+127\.0\.0\.1:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$",
            result.stdout.decode("ascii", errors="replace"),
        )
        pids = {int(value) for value in matches}
    else:
        pids = {
            connection.pid
            for connection in psutil.net_connections(kind="tcp")
            if connection.laddr.ip == "127.0.0.1"
            and connection.laddr.port == port
            and connection.status == psutil.CONN_LISTEN
            and connection.pid
        }
    if len(pids) != 1:
        raise GateFailure("Cannot attest one existing Strata listener PID")
    return pids.pop()


def process_identity(pid: int) -> dict:
    process = psutil.Process(pid)
    executable = Path(process.exe()).resolve(strict=True)
    return {
        "pid": pid,
        "created_at": process.create_time(),
        "executable": str(executable),
        "executable_sha256": file_hash(executable),
    }


def service_metadata(origin: str) -> dict:
    validate_origin(origin)
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        health_response = client.get(origin + "/health")
        models_response = client.get(origin + "/v1/models")
        health_response.raise_for_status()
        models_response.raise_for_status()
        health = health_response.json()
        models = models_response.json()["data"]
    if (
        health.get("service") != "strata"
        or health.get("loaded") is not True
        or health.get("api_key") is not False
        or not isinstance(health.get("model"), str)
        or not health["model"]
        or type(health.get("max_context")) is not int
        or health["max_context"] <= 0
    ):
        raise GateFailure("Existing unauthenticated Strata service is not loaded")
    entries = [entry for entry in models if entry.get("id") == health["model"]]
    if (
        len(entries) != 1
        or entries[0].get("status", {}).get("value") != "loaded"
        or entries[0].get("meta", {}).get("n_ctx") != health["max_context"]
    ):
        raise GateFailure("Strata health/models identities disagree")
    return {"model_id": health["model"], "context": health["max_context"]}


def capture_identity(origin: str, engine_pid: int, configuration: Path) -> dict:
    metadata = service_metadata(origin)
    engine = psutil.Process(engine_pid)
    if engine.name().lower() != "strata.exe":
        raise GateFailure("Explicit engine PID does not identify Strata")
    return {
        "schema": "ephy.existing-strata.v1",
        "base_url": origin,
        **metadata,
        "listener": process_identity(listener_pid(validate_origin(origin))),
        "engine": process_identity(engine_pid),
        "configuration": {
            "path": str(configuration.resolve(strict=True)),
            "sha256": file_hash(configuration),
        },
    }


def verify_identity(identity: dict) -> None:
    if (
        set(identity) != {"schema", "base_url", "model_id", "context", "listener", "engine", "configuration"}
        or identity["schema"] != "ephy.existing-strata.v1"
    ):
        raise GateFailure("Invalid existing-Strata identity")
    metadata = service_metadata(identity["base_url"])
    if metadata != {key: identity[key] for key in ("model_id", "context")}:
        raise GateFailure("Strata model/context changed")
    if listener_pid(validate_origin(identity["base_url"])) != identity["listener"]["pid"]:
        raise GateFailure("Strata listener changed")
    for name in ("listener", "engine"):
        if process_identity(identity[name]["pid"]) != identity[name]:
            raise GateFailure("Strata process/runtime changed")
    configuration = identity["configuration"]
    if file_hash(Path(configuration["path"])) != configuration["sha256"]:
        raise GateFailure("Strata configuration changed")


@dataclass(frozen=True)
class ExistingService:
    pid: int


class StrataRunner(FormalRunner):
    """Reuse managed Pi, fixed checks and frozen artifacts without owning Strata."""

    def __init__(self, job_file: Path, *, initial_job: dict | None = None):
        super().__init__(job_file, initial_job=initial_job)
        if (
            self.runtime.get("backend") != "external_strata"
            or self.runtime.get("provider_id") != "strata-local"
            or self.runtime.get("review_mode") != "external_codex"
            or self.contract["max_repairs"] != 0
            or len(self.contract["allowed_files"]) != 1
        ):
            raise GateFailure("Strata requires the explicit external-review profile")
        identity = read_json(Path(self.runtime["models_ini"]))
        if any(
            role_model(self.runtime, role) != identity["model_id"]
            for role in ("planner", "implementer", "auditor")
        ):
            raise GateFailure("Strata roles must pin the observed running model")
        if self.runtime["base_url"] != identity["base_url"]:
            raise GateFailure("Strata endpoint differs from frozen identity")
        managed_dir = self.runtime.get("managed_dir")
        if not isinstance(managed_dir, str) or not Path(managed_dir).is_absolute():
            raise GateFailure("Strata requires a dedicated managed Pi settings directory")
        settings_path = safe_path(Path(managed_dir), "settings.json")
        required = {
            str(Path(__file__).resolve()),
            identity["listener"]["executable"],
            identity["engine"]["executable"],
            identity["configuration"]["path"],
            str(settings_path),
        }
        if not required.issubset(self.contract["runtime_hashes"]):
            raise GateFailure("Missing Strata deployment/runtime pins")
        if file_hash(settings_path) != self.contract["runtime_hashes"][str(settings_path)]:
            raise GateFailure("Managed Pi settings changed after submission")
        settings = read_json(settings_path)
        if not isinstance(settings, dict) or any(
            not isinstance(settings.get(name), dict) or settings[name].get("enabled") is not False
            for name in ("retry", "compaction")
        ):
            raise GateFailure("Strata requires Pi retries and compaction explicitly disabled")
        self.identity = identity
        self.server = ExistingService(identity["listener"]["pid"])
        self.server_owned = False
        self.last_service_probe = 0.0

    def router_request(self, route: str, body: dict | None = None) -> dict:
        raise GateFailure("Strata lifecycle/router operations are forbidden")

    def start_server(self) -> None:
        verify_identity(self.identity)

    def preflight(self) -> None:
        self.reject_conflicting_processes(("pi.exe", "llama-server.exe"))
        super().preflight()

    def verify_model_artifacts(self, model: str) -> None:
        if model != self.identity["model_id"]:
            raise GateFailure("Wrong frozen Strata model")
        if self.runtime["model_manifests"][model] != self.identity:
            raise GateFailure("Strata deployment manifest changed")
        verify_identity(self.identity)

    def load_model(self, model: str) -> None:
        self.resources()
        self.verify_model_artifacts(model)
        self.loaded_model = model
        self.event(
            "model loaded",
            model=model,
            router_entry={"id": model, "status": {"value": "loaded"}},
            server_pid=self.server.pid,
            ownership="preexisting",
            operation="read-only identity observation",
        )

    def resources(self) -> None:
        super().resources()
        if time.monotonic() - self.last_service_probe >= 5:
            verify_identity(self.identity)
            self.last_service_probe = time.monotonic()


def make_runner(job_file: Path, *, initial_job: dict | None = None) -> FormalRunner:
    job = read_json(job_file) if initial_job is None else initial_job
    backend = job["runtime"].get("backend", "owned_llama")
    if backend == "external_strata":
        return StrataRunner(job_file, initial_job=job)
    if backend != "owned_llama":
        raise GateFailure("Unknown frozen model backend")
    return FormalRunner(job_file, initial_job=job)


def freeze_external_proposal(runner: FormalRunner, bundle: Path, audit_input: dict) -> None:
    """Record a pre-audit stop; never manufacture an auditor or review approval."""
    if runner.runtime.get("backend") != "external_strata":
        raise GateFailure("External review profile requires explicit Strata authorization")
    runner.event("proposal stop", decision="EXTERNAL_REVIEW_PENDING")
    write_json(
        runner.directory / "external-review.json",
        {
            "schema": "ephy.external-review.v1",
            "job_id": runner.job["id"],
            "audit_input_sha256": file_hash(bundle / "audit-input.json"),
            "final_bindings": audit_input["final_bindings"],
            "workflow": runner.events,
            "required": ["current-head CI", "independent Codex Review", "no unresolved P0/P1"],
            "formal_audit_executed": False,
            "adopted": False,
        },
    )
    runner.job["outcome"] = "external_review_pending"
    runner.state("external_review_pending", "Verified and frozen; unapplied, awaiting Codex Review")
    verify_external_proposal(runner.job_file)


def verify_external_proposal(job_file: Path) -> Path:
    runner = make_runner(job_file)
    if (
        runner.job["status"] != "external_review_pending"
        or runner.job["outcome"] != "external_review_pending"
        or runner.runtime.get("review_mode") != "external_codex"
    ):
        raise GateFailure("No frozen external-review proposal")
    runner.intact()
    bundle = runner.directory / "audit-bundle"
    audit_input = read_json(bundle / "audit-input.json")
    validate_schema(audit_input, read_json(bundle / "audit_input_schema.json"))
    inspect_bundle(
        bundle,
        read_json(bundle / "evidence-manifest.json"),
        read_json(bundle / "evidence_manifest_schema.json"),
    )
    manifest = read_json(bundle / "evidence-manifest.json")
    entries = {entry["artifact_id"]: entry for entry in manifest["artifacts"]}
    if (
        audit_input["job_id"] != runner.job["id"]
        or audit_input["audit_id"] != runner.job["id"] + "-audit"
        or audit_input["evidence_manifest_sha256"] != file_hash(bundle / "evidence-manifest.json")
        or audit_input["bundle_integrity_attestation_sha256"] != file_hash(bundle / "bundle-integrity.json")
        or audit_input["allowed_file_scope"] != runner.contract["allowed_files"]
        or audit_input["stage_contract"]["expected_models"] != runner.job["model_identities"]
        or read_json(bundle / "task_spec.txt") != runner.contract
        or any(
            audit_input["final_bindings"][key] != entries[name]["sha256"]
            for key, name in FINAL_BINDINGS.items()
        )
    ):
        raise GateFailure("External-review audit input is not bound to its frozen artifacts")
    record = read_json(runner.directory / "external-review.json")
    prefix = read_json(bundle / "workflow_events.txt")
    if (
        record.get("schema") != "ephy.external-review.v1"
        or record["required"] != ["current-head CI", "independent Codex Review", "no unresolved P0/P1"]
        or record["audit_input_sha256"] != file_hash(bundle / "audit-input.json")
        or record["final_bindings"] != audit_input["final_bindings"]
        or record["workflow"]
        != prefix
        + [
            {
                "at": record["workflow"][-1]["at"],
                "stage": "proposal stop",
                "decision": "EXTERNAL_REVIEW_PENDING",
            }
        ]
        or [event["stage"] for event in prefix]
        != [
            "preflight",
            "model loaded",
            "planner start",
            "planner end",
            "model loaded",
            "implementer start",
            "implementer end",
            "independent verification",
            "freeze",
        ]
        or prefix[0].get("passed") is not True
        or prefix[-1].get("proposal_stop_required") is not True
        or prefix[-1].get("controller_sha256") != file_hash(Path(__file__).with_name("formal_runtime.py"))
        or prefix[-1].get("server_pid") != runner.identity["listener"]["pid"]
        or record["formal_audit_executed"] is not False
        or record["adopted"] is not False
        or record["job_id"] != runner.job["id"]
        or audit_input["baseline_commit"] != runner.job["baseRevision"]
        or snapshot(runner.candidate) != read_json(bundle / "candidate_snapshot_manifest.txt")
    ):
        raise GateFailure("External-review candidate/workflow binding changed")
    results = read_json(bundle / "verification_results.txt")
    changed = read_json(bundle / "candidate_changed_files.txt")
    final = read_json(bundle / "candidate_snapshot_manifest.txt")
    if (
        results["passed"] is not True
        or results["baseline"] is not False
        or results["verifier_identity"] != runner.verifier_identity()
        or {check["id"] for check in results["checks"]}
        != {"target", "regression", "lint", "repository", "fixed", "diff"}
        or len(results["checks"]) != 6
        or any(check["passed"] is not True or check["exit_code"] != 0 for check in results["checks"])
        or changed != results["changed_files"]
        or changed != runner.contract["allowed_files"]
        or results["snapshot_sha256"] != digest(encode(final))
        or prefix[-2].get("results") != results
        or prefix[-1].get("snapshot_sha256") != results["snapshot_sha256"]
        or prefix[-1].get("patch_sha256") != audit_input["final_bindings"]["candidate_patch_sha256"]
    ):
        raise GateFailure("External-review independent verification is invalid")
    diff = next(check for check in results["checks"] if check["id"] == "diff")
    executable = executable_identity("git")
    transcripts = read_json(bundle / "command_transcripts.txt")
    index = safe_path(runner.directory, diff["stdout"].removesuffix(".stdout.log") + ".index")
    environment = {**command_environment(runner.candidate, runner.directory / "temp"), "GIT_INDEX_FILE": str(index)}
    preparation = diff.get("preparation", [])
    expected_arguments = [
        ["read-tree", runner.job["baseRevision"]],
        ["add", "--all", "--"],
        ["diff", "--cached", "--check", runner.job["baseRevision"]],
    ]
    records = [*preparation, {key: value for key, value in diff.items() if key not in ("id", "passed", "isolated_index", "preparation")}]
    if (
        len(preparation) != 2
        or diff.get("isolated_index") != {"path": str(index), "sha256": file_hash(index)}
        or any(
            record.get("argv") != [executable["path"], "-C", str(runner.candidate), *arguments]
            or record.get("exit_code") != 0
            or record.get("cwd") != str(runner.candidate)
            or record.get("executable") != executable
            or record.get("environment_sha256") != runner.job["environment_sha256"]
            or record.get("effective_environment_sha256") != digest(encode(environment))
            or record.get("temp_root") != str(runner.directory / "temp")
            or record.get("temp_variables") != {key: str(runner.directory / "temp") for key in ("TEMP", "TMP", "TMPDIR")}
            or sum(all(entry.get(key) == value for key, value in record.items()) for entry in transcripts) != 1
            or record.get("stdout_sha256") != file_hash(safe_path(runner.directory, record["stdout"]))
            or record.get("stderr_sha256") != file_hash(safe_path(runner.directory, record["stderr"]))
            for record, arguments in zip(records, expected_arguments, strict=True)
        )
    ):
        raise GateFailure("External-review diff-check evidence is invalid")
    provenance = read_json(bundle / "model_provenance.txt")
    if [entry["role"] for entry in provenance] != ["planner", "implementer"]:
        raise GateFailure("External review requires one plan and one implementation")
    if len({entry["pid"] for entry in provenance}) != 2:
        raise GateFailure("Plan and implementation must be distinct Pi processes")
    for entry in provenance:
        role = entry["role"]
        if entry["identity"] != runner.job["model_identities"][role]:
            raise GateFailure("External-review model identity changed")
        trace_name = "planner" if role == "planner" else "worker-1"
        trace = runner.directory / (trace_name + "-trace.jsonl")
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        observed = stage_evidence(events, role_model(runner.runtime, role), role)
        if (
            entry["trace_sha256"] != file_hash(trace)
            or entry["session_sha256"] != file_hash(safe_path(runner.directory, entry["session_path"]))
            or entry["output_tokens"] != observed["output_tokens"]
            or entry["requests"] != observed["requests"]
            or not 0 <= observed["output_tokens"] <= runner.contract["output_token_budget"]
            or not 1 <= observed["requests"] <= runner.contract["max_requests"]
            or observed["governance_ack"]["details"]["policySha256"]
            != runner.job["controls"]["system_development_policy"]
        ):
            raise GateFailure("External-review model/ack trace changed")
    patch = runner.directory / "candidate.patch"
    if file_hash(patch) != audit_input["final_bindings"]["candidate_patch_sha256"]:
        raise GateFailure("External-review patch changed")
    return patch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--engine-pid", type=int)
    parser.add_argument("--configuration", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.capture:
        if not args.base_url or not args.engine_pid or not args.configuration:
            parser.error("Capture requires explicit origin, engine PID and configuration")
        write_json(args.capture, capture_identity(args.base_url, args.engine_pid, args.configuration))
    elif args.verify:
        print(json.dumps({"patch": str(verify_external_proposal(args.verify)), "adopted": False}))
    else:
        parser.error("Select capture or verify")


if __name__ == "__main__":
    main()
