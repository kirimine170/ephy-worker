"""Managed Pi proposals against a pinned, already running llama.cpp service.

All service operations are observations. This backend never starts, stops,
unloads, reconfigures, or claims cryptographic identity of loaded weight bytes.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from .formal_artifacts import GateFailure, digest, encode, file_hash, read_json, safe_path
from .formal_runtime import FormalRunner, role_model
from .strata_runtime import ExistingService, listener_pid, process_identity, validate_origin
from .verifier_sandbox import PROFILE, validate_descriptor
from .verifier_sandbox import identity as sandbox_identity


def service_metadata(origin: str) -> dict:
    validate_origin(origin)
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        health_response = client.get(origin + "/health")
        models_response = client.get(origin + "/v1/models")
        props_response = client.get(origin + "/props")
        for response in (health_response, models_response, props_response):
            response.raise_for_status()
        health, models, props = health_response.json(), models_response.json(), props_response.json()
    entries = models.get("data")
    context = props.get("default_generation_settings", {}).get("n_ctx")
    if (
        health.get("status") != "ok" or not isinstance(entries, list) or len(entries) != 1
        or not isinstance(entries[0], dict)
        or not isinstance(entries[0].get("id"), str) or not entries[0]["id"].strip()
        or type(context) is not int or context <= 0
    ):
        raise GateFailure("Existing llama.cpp health/models/context identity is not ready")
    return {"model_id": entries[0]["id"], "context": context}


def capture_identity(origin: str, engine_pid: int, configuration: Path) -> dict:
    metadata = service_metadata(origin)
    listener = listener_pid(validate_origin(origin))
    engine = process_identity(engine_pid)
    if listener != engine_pid or Path(engine["executable"]).name.lower() != "llama-server.exe":
        raise GateFailure("Explicit PID must own the existing llama.cpp loopback listener")
    return {
        "schema": "ephy.existing-llama.v1", "base_url": origin, **metadata,
        "listener": engine, "engine": engine,
        "configuration": {"path": str(configuration.resolve(strict=True)), "sha256": file_hash(configuration)},
    }


def verify_identity(identity: dict) -> None:
    if not isinstance(identity, dict) or set(identity) != {
        "schema", "base_url", "model_id", "context", "listener", "engine", "configuration",
    } or identity["schema"] != "ephy.existing-llama.v1":
        raise GateFailure("Invalid existing-llama deployment identity")
    if identity["listener"] != identity["engine"]:
        raise GateFailure("Existing llama.cpp must directly own its listener")
    if service_metadata(identity["base_url"]) != {key: identity[key] for key in ("model_id", "context")}:
        raise GateFailure("Existing llama.cpp model/context changed")
    if listener_pid(validate_origin(identity["base_url"])) != identity["listener"]["pid"]:
        raise GateFailure("Existing llama.cpp listener changed")
    if process_identity(identity["engine"]["pid"]) != identity["engine"]:
        raise GateFailure("Existing llama.cpp process/runtime changed")
    configuration = identity["configuration"]
    if file_hash(Path(configuration["path"])) != configuration["sha256"]:
        raise GateFailure("Existing llama.cpp configuration changed")


class ExistingLlamaRunner(FormalRunner):
    """Full governed proposal stages; only the verifier receives executable code."""

    def __init__(self, job_file: Path, *, initial_job: dict | None = None):
        super().__init__(job_file, initial_job=initial_job)
        if (
            self.runtime.get("backend") != "external_existing_llama"
            or self.runtime.get("provider_id") != "dual-local"
            or self.runtime.get("review_mode", "formal") != "formal"
            or self.contract.get("execution_profile") != PROFILE
        ):
            raise GateFailure("Existing llama.cpp requires the explicit isolated formal code profile")
        identity = read_json(Path(self.runtime["models_ini"]))
        if self.runtime["base_url"] != identity["base_url"] or any(
            role_model(self.runtime, role) != identity["model_id"]
            for role in ("planner", "implementer", "auditor")
        ):
            raise GateFailure("Every managed Pi role must pin the observed existing llama.cpp model")
        if self.runtime["server"] != identity["engine"]["executable"]:
            raise GateFailure("Existing llama.cpp executable differs from the frozen deployment")
        managed = self.runtime.get("managed_dir")
        if not isinstance(managed, str) or not Path(managed).is_absolute():
            raise GateFailure("A dedicated managed Pi settings directory is required")
        settings_path = safe_path(Path(managed), "settings.json")
        settings = read_json(settings_path)
        if not isinstance(settings, dict) or any(
            not isinstance(settings.get(name), dict) or settings[name].get("enabled") is not False
            for name in ("retry", "compaction")
        ):
            raise GateFailure("Managed Pi retries and compaction must be explicitly disabled")
        descriptor = self.contract["verifier_sandbox"]
        validate_descriptor(descriptor)
        required = {
            str(Path(__file__).resolve()), str(Path(__file__).with_name("verifier_sandbox.py").resolve()),
            identity["engine"]["executable"], identity["configuration"]["path"], str(settings_path),
            descriptor["codex"]["path"], descriptor["python"]["path"], descriptor["capability"]["path"],
            descriptor["acl_helper"]["path"],
        }
        if not required.issubset(self.contract["runtime_hashes"]):
            raise GateFailure("Missing existing deployment or native sandbox runtime pins")
        if any(file_hash(Path(path)) != self.contract["runtime_hashes"][path] for path in required):
            raise GateFailure("Existing deployment/sandbox pin changed")
        # Construction also occurs in the read-only integration gate, whose
        # path need not call resources/preflight. Reobserve the live listener
        # there too, so a restarted service cannot inherit old provenance.
        verify_identity(identity)
        self.identity = identity
        self.server = ExistingService(identity["listener"]["pid"])
        self.server_owned = False
        self.last_service_probe = 0.0

    def router_request(self, route: str, body: dict | None = None) -> dict:
        raise GateFailure("Existing llama.cpp lifecycle/router operations are forbidden")

    def start_server(self) -> None:
        verify_identity(self.identity)

    def preflight(self) -> None:
        # The sole explicitly pinned existing listener is not owned by this job.
        # Other Pi/Strata processes still prevent concurrent formal execution.
        self.reject_conflicting_processes(("pi.exe", "strata.exe"))
        verify_identity(self.identity)
        super().preflight()

    def verify_model_artifacts(self, model: str) -> None:
        if model != self.identity["model_id"] or self.runtime["model_manifests"][model] != self.identity:
            raise GateFailure("Existing llama.cpp deployment manifest changed")
        verify_identity(self.identity)

    def load_model(self, model: str) -> None:
        self.resources()
        self.verify_model_artifacts(model)
        self.loaded_model = model
        self.event(
            "model loaded", model=model, router_entry={"id": model, "status": {"value": "loaded"}},
            server_pid=self.server.pid, ownership="preexisting", operation="read-only identity observation",
        )

    def resources(self) -> None:
        super().resources()
        if time.monotonic() - self.last_service_probe >= 5:
            verify_identity(self.identity)
            self.last_service_probe = time.monotonic()


def sandbox_binding(contract: dict) -> str:
    """Bound into the real verifier identity and stage guard configuration."""
    return digest(encode(sandbox_identity(contract["verifier_sandbox"])))
