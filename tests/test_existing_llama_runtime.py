"""No live model calls: identity and lifecycle refusal controls."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from ephy_worker import existing_llama_runtime as llama
from ephy_worker.formal_artifacts import GateFailure, file_hash, write_json


@pytest.mark.parametrize("mutation", ["none", "health", "empty", "duplicate", "model", "context"])
def test_actual_llama_metadata_requires_one_ready_model(monkeypatch, mutation):
    health = {"status": "ok"}
    models = {"data": [{"id": "Qwen3-Coder-Next-Q4_K_M"}]}
    props = {"default_generation_settings": {"n_ctx": 32768}}
    if mutation == "health":
        health["status"] = "loading model"
    elif mutation == "empty":
        models["data"] = []
    elif mutation == "duplicate":
        models["data"] *= 2
    elif mutation == "model":
        models["data"][0]["id"] = ""
    elif mutation == "context":
        props["default_generation_settings"]["n_ctx"] = 0
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"/health": health, "/v1/models": models, "/props": props}[request.url.path])

    original = httpx.Client
    monkeypatch.setattr(llama.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    if mutation == "none":
        assert llama.service_metadata("http://127.0.0.1:18080") == {
            "model_id": "Qwen3-Coder-Next-Q4_K_M", "context": 32768,
        }
    else:
        with pytest.raises(GateFailure):
            llama.service_metadata("http://127.0.0.1:18080")
    assert calls == [("GET", "/health"), ("GET", "/v1/models"), ("GET", "/props")]


@pytest.mark.parametrize("origin", ["http://example.com:18080", "http://localhost:18080", "https://127.0.0.1:18080"])
def test_remote_and_ambiguous_llama_origins_rejected(origin):
    with pytest.raises(GateFailure):
        llama.service_metadata(origin)


def frozen_identity(tmp_path):
    engine, configuration = tmp_path / "llama-server.exe", tmp_path / "configuration.json"
    engine.write_bytes(b"synthetic runtime")
    configuration.write_text("{}")
    process = {"pid": 42, "created_at": 123.0, "executable": str(engine), "executable_sha256": file_hash(engine)}
    return {
        "schema": "ephy.existing-llama.v1", "base_url": "http://127.0.0.1:18080",
        "model_id": "Qwen3-Coder-Next-Q4_K_M", "context": 32768,
        "listener": process, "engine": copy.deepcopy(process),
        "configuration": {"path": str(configuration), "sha256": file_hash(configuration)},
    }


@pytest.mark.parametrize("mutation", ["none", "model", "context", "listener", "restart", "binary", "config"])
def test_existing_llama_replacement_fails_closed(tmp_path, monkeypatch, mutation):
    identity = frozen_identity(tmp_path)
    metadata = {key: identity[key] for key in ("model_id", "context")}
    process = copy.deepcopy(identity["engine"])
    if mutation in ("model", "context"):
        metadata["model_id" if mutation == "model" else "context"] = "changed"
    elif mutation == "restart":
        process["created_at"] += 1
    elif mutation == "binary":
        process["executable_sha256"] = "0" * 64
    elif mutation == "config":
        Path(identity["configuration"]["path"]).write_text("changed")
    monkeypatch.setattr(llama, "service_metadata", lambda _: metadata)
    monkeypatch.setattr(llama, "listener_pid", lambda _: 99 if mutation == "listener" else 42)
    monkeypatch.setattr(llama, "process_identity", lambda _: process)
    if mutation == "none":
        llama.verify_identity(identity)
    else:
        with pytest.raises(GateFailure):
            llama.verify_identity(identity)


def test_model_lifecycle_is_always_refused():
    runner = llama.ExistingLlamaRunner.__new__(llama.ExistingLlamaRunner)
    for route in ("/models/load", "/models/unload", "/shutdown", "/props"):
        with pytest.raises(GateFailure, match="forbidden"):
            runner.router_request(route, {"model": "anything"})


def test_unknown_backend_cannot_fall_back_during_integration(tmp_path):
    from ephy_worker.formal_runtime import verify_proposal_for_integration

    path = tmp_path / "job.json"
    write_json(path, {"runtime": {"backend": "unknown"}})
    with pytest.raises(GateFailure, match="Unknown frozen model backend at integration"):
        verify_proposal_for_integration(path)


def test_failure_does_not_stop_existing_model(tmp_path, monkeypatch):
    runner = llama.ExistingLlamaRunner.__new__(llama.ExistingLlamaRunner)
    runner.directory = tmp_path
    runner.candidate = tmp_path / "absent-candidate"
    runner.job_file = tmp_path / "job.json"
    runner.job = {}
    runner.contract = {}
    runner.server_owned = False
    runner.server = object()
    monkeypatch.setattr(runner, "preflight", lambda: (_ for _ in ()).throw(GateFailure("synthetic boundary")))
    monkeypatch.setattr("ephy_worker.formal_runtime.stop_tree", lambda _: pytest.fail("Existing model stopped"))
    with pytest.raises(GateFailure, match="synthetic boundary"):
        runner.run()
    assert json.loads(runner.job_file.read_text())["outcome"] == "infrastructure_failed"


@pytest.mark.parametrize("replaced", [False, True])
def test_dispatch_reobserves_existing_listener_even_before_preflight(tmp_path, monkeypatch, replaced):
    from test_verifier_sandbox import descriptor_fixture

    from ephy_worker.strata_runtime import make_runner

    controls = tmp_path / "controls"
    controls.mkdir()
    monkeypatch.setattr(Path, "home", lambda: controls)
    descriptor = descriptor_fixture(controls)
    identity = frozen_identity(tmp_path)
    identity_path = tmp_path / "identity.json"
    write_json(identity_path, identity)
    managed = tmp_path / "managed"
    managed.mkdir()
    settings = managed / "settings.json"
    write_json(settings, {"retry": {"enabled": False}, "compaction": {"enabled": False}})
    required = [Path(llama.__file__).resolve(), Path(llama.__file__).with_name("verifier_sandbox.py").resolve(),
                Path(identity["engine"]["executable"]), Path(identity["configuration"]["path"]), settings,
                *(Path(descriptor[name]["path"]) for name in ("codex", "python", "acl_helper", "capability"))]
    job = {
        "runtime": {"backend": "external_existing_llama", "provider_id": "dual-local", "review_mode": "formal",
            "base_url": identity["base_url"], "server": identity["engine"]["executable"],
            "models_ini": str(identity_path), "managed_dir": str(managed),
            "model_roles": {role: identity["model_id"] for role in ("planner", "implementer", "auditor")}},
        "contract": {"timeout_seconds": 30, "execution_profile": "native_windows_code_v1",
            "verifier_sandbox": descriptor, "runtime_hashes": {str(path): file_hash(path) for path in required}},
        "jobDir": str(tmp_path / "job"), "worktreePath": str(tmp_path / "candidate"),
        "repoRoot": str(tmp_path / "repository"),
    }
    path = tmp_path / "job.json"
    write_json(path, job)
    observations = []

    def observe(value):
        observations.append(value)
        if replaced:
            raise GateFailure("existing listener replaced")

    monkeypatch.setattr(llama, "verify_identity", observe)
    if replaced:
        with pytest.raises(GateFailure, match="listener replaced"):
            make_runner(path)
    else:
        runner = make_runner(path)
        assert runner.server_owned is False and runner.server.pid == identity["listener"]["pid"]
    assert observations == [identity]
