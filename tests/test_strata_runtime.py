"""External-service negative controls; these tests never contact a live model."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ephy_worker import strata_runtime as strata
from ephy_worker.formal_artifacts import GateFailure, file_hash, write_json
from ephy_worker.formal_runtime import (
    role_model,
    role_thinking,
    verify_proposal_for_integration,
)


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8081",
        "http://localhost:8081",
        "http://127.0.0.1",
        "http://127.0.0.1:8081/v1",
        "http://127.0.0.1:8081/",
        "http://user:secret@127.0.0.1:8081",
        "http://127.0.0.1:8081?x=1",
        "http://127.0.0.1:8081#x",
        "http://127.0.0.1:99999",
        "http://[invalid",
    ],
)
def test_origin_rejects_ambiguous_or_remote_endpoints(origin):
    with pytest.raises(GateFailure):
        strata.validate_origin(origin)


def test_explicit_loopback_origin():
    assert strata.validate_origin("http://127.0.0.1:8081") == 8081


@pytest.mark.parametrize("mutation", ["none", "unloaded", "auth", "model", "context", "duplicate"])
def test_metadata_requires_matching_loaded_identity(monkeypatch, mutation):
    health = {
        "service": "strata",
        "loaded": True,
        "api_key": False,
        "model": "flash-next",
        "max_context": 131072,
    }
    models = [{"id": "flash-next", "status": {"value": "loaded"}, "meta": {"n_ctx": 131072}}]
    if mutation == "unloaded":
        health["loaded"] = False
    elif mutation == "auth":
        health["api_key"] = True
    elif mutation == "model":
        models[0]["id"] = "replacement"
    elif mutation == "context":
        models[0]["meta"]["n_ctx"] = 8192
    elif mutation == "duplicate":
        models += copy.deepcopy(models)
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=health if request.url.path == "/health" else {"data": models})

    original = httpx.Client
    monkeypatch.setattr(
        strata.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    if mutation == "none":
        assert strata.service_metadata("http://127.0.0.1:8081") == {
            "model_id": "flash-next",
            "context": 131072,
        }
    else:
        with pytest.raises(GateFailure):
            strata.service_metadata("http://127.0.0.1:8081")
    assert calls == [("GET", "/health"), ("GET", "/v1/models")]


def frozen_identity(tmp_path):
    executable = tmp_path / "engine.exe"
    executable.write_bytes(b"frozen runtime")
    configuration = tmp_path / "configuration.json"
    configuration.write_text("{}", encoding="utf-8")
    process = {
        "pid": 100,
        "created_at": 123.0,
        "executable": str(executable),
        "executable_sha256": file_hash(executable),
    }
    return {
        "schema": "ephy.existing-strata.v1",
        "base_url": "http://127.0.0.1:8081",
        "model_id": "flash-next",
        "context": 131072,
        "listener": process,
        "engine": {**process, "pid": 101},
        "configuration": {"path": str(configuration), "sha256": file_hash(configuration)},
    }


@pytest.mark.parametrize("mutation", ["none", "model", "context", "listener", "restart", "binary", "config"])
def test_existing_service_replacement_fails_closed(tmp_path, monkeypatch, mutation):
    identity = frozen_identity(tmp_path)
    metadata = {key: identity[key] for key in ("model_id", "context")}
    processes = {identity[key]["pid"]: copy.deepcopy(identity[key]) for key in ("listener", "engine")}
    if mutation in ("model", "context"):
        metadata["model_id" if mutation == "model" else "context"] = (
            "replacement" if mutation == "model" else 8192
        )
    elif mutation == "restart":
        processes[101]["created_at"] += 1
    elif mutation == "binary":
        processes[101]["executable_sha256"] = "0" * 64
    elif mutation == "config":
        Path(identity["configuration"]["path"]).write_text("changed", encoding="utf-8")
    monkeypatch.setattr(strata, "service_metadata", lambda _: metadata)
    monkeypatch.setattr(strata, "listener_pid", lambda _: 999 if mutation == "listener" else 100)
    monkeypatch.setattr(strata, "process_identity", lambda pid: processes[pid])
    if mutation == "none":
        strata.verify_identity(identity)
    else:
        with pytest.raises(GateFailure):
            strata.verify_identity(identity)


def job_fixture(tmp_path):
    identity = frozen_identity(tmp_path)
    identity_path = tmp_path / "identity.json"
    write_json(identity_path, identity)
    managed = tmp_path / "managed-pi"
    managed.mkdir()
    settings_path = managed / "settings.json"
    write_json(settings_path, {"retry": {"enabled": False}, "compaction": {"enabled": False}})
    runtime = {
        "backend": "external_strata",
        "provider_id": "strata-local",
        "review_mode": "external_codex",
        "base_url": identity["base_url"],
        "models_ini": str(identity_path),
        "managed_dir": str(managed),
        "model_roles": {role: identity["model_id"] for role in ("planner", "implementer", "auditor")},
    }
    pins = {str(Path(strata.__file__).resolve()): file_hash(Path(strata.__file__))}
    pins[str(settings_path)] = file_hash(settings_path)
    for entry in (identity["listener"], identity["engine"]):
        pins[entry["executable"]] = entry["executable_sha256"]
    pins[identity["configuration"]["path"]] = identity["configuration"]["sha256"]
    job = {
        "runtime": runtime,
        "contract": {
            "max_repairs": 0,
            "allowed_files": ["docs/example.md"],
            "timeout_seconds": 30,
            "runtime_hashes": pins,
        },
        "jobDir": str(tmp_path / "job"),
        "worktreePath": str(tmp_path / "candidate"),
        "repoRoot": str(tmp_path / "repository"),
    }
    job_file = tmp_path / "job.json"
    write_json(job_file, job)
    return job_file, job


@pytest.mark.parametrize(
    "mutation", ["none", "owned", "provider", "review", "repairs", "scope", "model", "pins"]
)
def test_external_profile_is_explicit_and_bounded(tmp_path, mutation):
    job_file, job = job_fixture(tmp_path)
    if mutation == "owned":
        job["runtime"]["backend"] = "owned_llama"
    elif mutation in ("provider", "review"):
        job["runtime"]["provider_id" if mutation == "provider" else "review_mode"] = "invalid"
    elif mutation == "repairs":
        job["contract"]["max_repairs"] = 1
    elif mutation == "scope":
        job["contract"]["allowed_files"].append("docs/extra.md")
    elif mutation == "model":
        job["runtime"]["model_roles"]["auditor"] = "replacement"
    elif mutation == "pins":
        job["contract"]["runtime_hashes"] = {}
    write_json(job_file, job, exclusive=False)
    if mutation == "none":
        runner = strata.make_runner(job_file)
        assert runner.server_owned is False and runner.server.pid == 100
    else:
        with pytest.raises(GateFailure):
            strata.StrataRunner(job_file)


def test_external_failure_never_stops_existing_service(tmp_path, monkeypatch):
    job_file, _ = job_fixture(tmp_path)
    runner = strata.StrataRunner(job_file)
    runner.directory.mkdir()
    monkeypatch.setattr(runner, "preflight", lambda: (_ for _ in ()).throw(GateFailure("boundary changed")))
    monkeypatch.setattr(
        "ephy_worker.formal_runtime.stop_tree", lambda _: pytest.fail("Existing Strata was stopped")
    )
    with pytest.raises(GateFailure, match="boundary changed"):
        runner.run()
    with pytest.raises(GateFailure, match="forbidden"):
        runner.router_request("/models/unload", {"model": "flash-next"})
    assert json.loads(job_file.read_text())["outcome"] == "infrastructure_failed"


def test_unknown_backend_does_not_fall_back_to_owned_server(tmp_path):
    job_file, job = job_fixture(tmp_path)
    job["runtime"]["backend"] = "unknown"
    write_json(job_file, job, exclusive=False)
    with pytest.raises(GateFailure, match="Unknown"):
        strata.make_runner(job_file)


def test_external_pending_can_never_use_formal_integration_gate(tmp_path):
    job_file, job = job_fixture(tmp_path)
    job.update(status="review_ready", outcome="accepted_proposal")
    write_json(job_file, job, exclusive=False)
    with pytest.raises(GateFailure, match="cannot integrate"):
        verify_proposal_for_integration(job_file)


def test_legacy_role_defaults_and_explicit_role_map():
    assert role_model({}, "planner") == "gpt-oss-20b-MXFP4"
    assert role_model({}, "implementer") == "Qwen3-Coder-Next-Q4_K_M"
    assert role_thinking({}, "implementer") == "off"
    assert (
        role_model(
            {"model_roles": {r: "flash-next" for r in ("planner", "implementer", "auditor")}}, "planner"
        )
        == "flash-next"
    )
    with pytest.raises(GateFailure):
        role_model({"model_roles": {"planner": "flash-next"}}, "planner")


@pytest.mark.parametrize("failure", ["none", "candidate", "infrastructure"])
def test_campaign_never_adopts_or_retries_infrastructure(tmp_path, monkeypatch, failure):
    from test_formal_runtime import verifier_draft, verifier_runner

    from ephy_worker.formal_campaign import run_campaign

    spec = verifier_draft(verifier_runner(tmp_path))
    spec["runtime"].update(backend="external_strata", resource_lock=str(tmp_path / "lock"))
    spec["contract"]["max_repairs"] = 0
    plan = {
        "specs": [copy.deepcopy(spec) for _ in range(4)],
        "max_consecutive_failures": 3,
        "timeout_seconds": 14400,
        "resource_lock": str(tmp_path / "lock"),
    }
    plan_file = tmp_path / "plan.json"
    write_json(plan_file, plan)
    calls = []

    def make_runner(job_file):
        def run():
            job = json.loads(job_file.read_text())
            calls.append(job["id"])
            failed = len(calls) > 1 and failure != "none"
            job.update(
                status="failed" if failed else "external_review_pending",
                outcome="infrastructure_failed"
                if failed and failure == "infrastructure"
                else "candidate_failed"
                if failed
                else "external_review_pending",
            )
            write_json(job_file, job, exclusive=False)

        return SimpleNamespace(run=run)

    monkeypatch.setattr(strata, "make_runner", make_runner)
    monkeypatch.setattr(strata, "verify_external_proposal", lambda _: tmp_path / "unapplied.patch")
    state = run_campaign(plan_file, tmp_path / "state")
    assert len(calls) == (2 if failure == "infrastructure" else 4)
    assert len(set(calls)) == len(calls)
    assert state["status"] == ("completed" if failure == "none" else "stopped")
    assert state["results"][0]["status"] == "external_review_pending"
    assert state["consecutive_failures"] == (
        0 if failure == "none" else 1 if failure == "infrastructure" else 3
    )
    assert "deadline_at" in state


@pytest.mark.parametrize(
    "mutation",
    ["none", "unmanaged", "missing", "unpinned", "defaults", "retry", "compaction", "changed", "type"],
)
def test_external_profile_pins_and_disables_pi_automatic_requests(tmp_path, mutation):
    job_file, job = job_fixture(tmp_path)
    settings_path = Path(job["runtime"]["managed_dir"]) / "settings.json"
    if mutation == "unmanaged":
        del job["runtime"]["managed_dir"]
    elif mutation == "missing":
        settings_path.unlink()
    elif mutation == "unpinned":
        del job["contract"]["runtime_hashes"][str(settings_path)]
    elif mutation in ("defaults", "retry", "compaction", "type", "changed"):
        settings = {"retry": {"enabled": False}, "compaction": {"enabled": False}}
        if mutation == "defaults":
            settings = {}
        elif mutation in ("retry", "compaction"):
            settings[mutation]["enabled"] = True
        elif mutation == "type":
            settings["retry"]["enabled"] = "false"
        else:
            settings["verbose"] = True
        write_json(settings_path, settings, exclusive=False)
        if mutation != "changed":
            # A controller pin cannot make unsafe automatic requests admissible.
            job["contract"]["runtime_hashes"][str(settings_path)] = file_hash(settings_path)
    write_json(job_file, job, exclusive=False)
    if mutation == "none":
        assert strata.StrataRunner(job_file).server_owned is False
    else:
        with pytest.raises(GateFailure):
            strata.StrataRunner(job_file)


def test_resume_stops_interrupted_strata_after_prior_success(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from test_formal_runtime import contract, retain_campaign_fixture_freeze

    from ephy_worker import formal_campaign as campaign
    from ephy_worker.formal_artifacts import digest, encode, read_json
    from ephy_worker.formal_runtime import submit

    spec = {
        "repoRoot": "unused",
        "baseRevision": "a" * 40,
        "contract": contract(),
        "runtime": {"backend": "external_strata", "resource_lock": str(tmp_path / "lock")},
        "controls": {},
        "model_identities": {},
        "verifier_identity": {},
    }
    plan = {
        "specs": [copy.deepcopy(spec) for _ in range(3)],
        "max_consecutive_failures": 3,
        "timeout_seconds": 14400,
        "resource_lock": spec["runtime"]["resource_lock"],
    }
    plan_file = tmp_path / "plan.json"
    write_json(plan_file, plan)
    state_root = tmp_path / "state"
    state_root.mkdir()
    successful = submit(spec, state_root)
    job = read_json(successful)
    job.update(status="external_review_pending", outcome="external_review_pending")
    write_json(successful, job, exclusive=False)
    interrupted = submit(spec, state_root)
    active = read_json(interrupted)
    active.update(status="running")
    active.pop("outcome", None)
    write_json(interrupted, active, exclusive=False)
    retained = interrupted.parent / "retained-evidence.txt"
    retained.write_bytes(b"keep interrupted evidence")
    deadline = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    state = {
        "plan_sha256": digest(encode(plan)),
        "status": "running",
        "results": [{"job_file": str(successful), "status": "external_review_pending"}],
        "next_index": 1,
        "consecutive_failures": 0,
        "active_job": str(interrupted),
        "elapsed_seconds": 2,
        "deadline_at": deadline,
    }
    retain_campaign_fixture_freeze(state_root, plan, state)
    write_json(state_root / "campaign.json", state)
    monkeypatch.setattr(strata, "verify_external_proposal", lambda _: tmp_path / "unapplied.patch")
    monkeypatch.setattr(campaign, "submit", lambda *_: pytest.fail("No next job after interruption"))
    monkeypatch.setattr(strata, "make_runner", lambda *_: pytest.fail("No model starts on resume"))
    result = campaign.run_campaign(plan_file, state_root, resume=True)
    assert result["status"] == "stopped"
    assert result["reason"] == "Interrupted Strata infrastructure failure; no replay"
    assert result["next_index"] == 2
    assert result["consecutive_failures"] == 1
    assert result["active_job"] is None
    assert result["deadline_at"] == deadline
    assert len(result["results"]) == 2
    assert read_json(interrupted)["outcome"] == "infrastructure_failed"
    assert retained.read_bytes() == b"keep interrupted evidence"
    assert len(list((state_root / "jobs").iterdir())) == 2


def test_external_campaign_rejects_more_than_four_hours(tmp_path):
    from ephy_worker.formal_campaign import run_campaign

    plan = {
        "specs": [{"runtime": {"backend": "external_strata"}}],
        "max_consecutive_failures": 3,
        "timeout_seconds": 14401,
        "resource_lock": str(tmp_path / "lock"),
    }
    plan_file = tmp_path / "plan.json"
    write_json(plan_file, plan)
    with pytest.raises(GateFailure, match="four hours"):
        run_campaign(plan_file, tmp_path / "state")


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "adopted",
        "review",
        "workflow",
        "patch",
        "binding",
        "snapshot",
        "trace",
        "session",
        "tokens",
        "checks",
        "diff_missing",
        "diff_duplicate",
        "diff_exit",
        "diff_passed",
        "diff_argv",
        "diff_cwd",
        "diff_environment",
        "diff_transcript",
        "diff_stdout",
    ],
)
def test_external_stop_requires_exact_frozen_candidate_and_evidence(tmp_path, monkeypatch, mutation):
    from test_formal_runtime import REPOSITORY, contract, trace, verifier_runner

    from ephy_worker.formal_artifacts import ARTIFACTS, CONTROL_PATHS, digest, encode, freeze_bundle

    # Only the diff command is real; model/audit provenance remains synthetic.
    command_runner = verifier_runner(tmp_path)
    directory = command_runner.directory
    diff = command_runner.command(
        ["git", "-C", str(command_runner.candidate), "diff", "--check"],
        command_runner.candidate,
        "external-diff",
        30,
    )
    transcripts = copy.deepcopy(command_runner.transcripts)
    configuration = contract()
    configuration["max_repairs"] = 0
    model_identity = {
        "model_id": "flash-next",
        "model_artifact_manifest_sha256": "a" * 64,
        "runtime_sha256": "b" * 64,
        "invocation_config_sha256": "c" * 64,
    }
    verifier = {
        "executor_id": "independent",
        "runtime_sha256": "d" * 64,
        "invocation_config_sha256": "e" * 64,
    }
    job = {
        "id": "test-job",
        "baseRevision": "a" * 40,
        "contract": configuration,
        "model_identities": {r: model_identity for r in ("planner", "implementer", "auditor")},
        "verifier_identity": verifier,
        "status": "external_review_pending",
        "outcome": "external_review_pending",
        "controls": {"system_development_policy": "9" * 64},
        "environment_sha256": command_runner.job["environment_sha256"],
    }
    patch = directory / "candidate.patch"
    patch.write_bytes(b"frozen markdown patch\n")
    final = {"docs/example.md": {"sha256": "f" * 64, "mode": 0}}
    results = {
        "passed": True,
        "baseline": False,
        "verifier_identity": verifier,
        "snapshot_sha256": digest(encode(final)),
        "changed_files": ["docs/example.md"],
        "checks": [{"id": c["id"], "exit_code": 0, "passed": True} for c in configuration["checks"]],
    }
    results["checks"].append({"id": "diff", **diff, "passed": True})
    if mutation == "checks":
        results["checks"][0]["exit_code"] = 1
    elif mutation == "diff_missing":
        results["checks"].pop()
    elif mutation == "diff_duplicate":
        results["checks"].append(copy.deepcopy(results["checks"][-1]))
    elif mutation in ("diff_exit", "diff_passed", "diff_argv", "diff_cwd", "diff_environment"):
        key, value = {
            "diff_exit": ("exit_code", 2),
            "diff_passed": ("passed", False),
            "diff_argv": ("argv", [diff["argv"][0], "--version"]),
            "diff_cwd": ("cwd", str(tmp_path)),
            "diff_environment": ("effective_environment_sha256", "0" * 64),
        }[mutation]
        results["checks"][-1][key] = value
        transcripts[0][key] = value
    elif mutation == "diff_transcript":
        transcripts = []
    elif mutation == "diff_stdout":
        (directory / diff["stdout"]).write_bytes(b"replaced diff output")
    provenance = []
    workflow = [{"stage": "preflight", "at": "synthetic", "passed": True}]
    for index, (role, label) in enumerate((("planner", "planner"), ("implementer", "worker-1"))):
        events = trace()
        events[0]["model"] = events[3]["model"] = "flash-next"
        events[1]["details"].update(role=role, policySha256="9" * 64)
        trace_path = directory / (label + "-trace.jsonl")
        trace_path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        session = directory / (label + "-session.jsonl")
        session.write_text("synthetic unit test session", encoding="utf-8")
        provenance.append(
            {
                "role": role,
                "pid": index + 1000,
                "identity": model_identity,
                "trace_sha256": file_hash(trace_path),
                "session_path": session.name,
                "session_sha256": file_hash(session),
                "output_tokens": 10,
                "requests": 1,
            }
        )
        workflow += [
            {"stage": "model loaded", "at": "synthetic"},
            {"stage": role + " start", "at": "synthetic"},
            {"stage": role + " end", "at": "synthetic"},
        ]
    workflow += [
        {"stage": "independent verification", "at": "synthetic", "results": results},
        {
            "stage": "freeze",
            "at": "synthetic",
            "proposal_stop_required": True,
            "controller_sha256": file_hash(Path(strata.__file__).with_name("formal_runtime.py")),
            "server_pid": 100,
            "snapshot_sha256": digest(encode(final)),
            "patch_sha256": file_hash(patch),
        },
    ]
    artifacts = {name: encode({"unit_test": name}) for name in ARTIFACTS}
    artifacts.update({name: (REPOSITORY / path).read_bytes() for name, path in CONTROL_PATHS.items()})
    artifacts.update(
        task_spec=encode(configuration),
        verification_results=encode(results),
        workflow_events=encode(workflow),
        model_provenance=encode(provenance),
        candidate_patch=patch.read_bytes(),
        candidate_changed_files=encode(["docs/example.md"]),
        candidate_snapshot_manifest=encode(final),
        command_transcripts=encode(transcripts),
    )
    bundle = directory / "audit-bundle"
    audit_input = freeze_bundle(bundle, job, artifacts)
    record = {
        "schema": "ephy.external-review.v1",
        "job_id": job["id"],
        "audit_input_sha256": file_hash(bundle / "audit-input.json"),
        "final_bindings": audit_input["final_bindings"],
        "workflow": workflow
        + [{"stage": "proposal stop", "at": "synthetic", "decision": "EXTERNAL_REVIEW_PENDING"}],
        "required": ["current-head CI", "independent Codex Review", "no unresolved P0/P1"],
        "formal_audit_executed": False,
        "adopted": False,
    }
    runner = SimpleNamespace(
        job=job,
        runtime={
            "review_mode": "external_codex",
            "model_roles": {r: "flash-next" for r in job["model_identities"]},
        },
        directory=directory,
        contract=configuration,
        identity={"listener": {"pid": 100}},
        candidate=tmp_path / "candidate",
        intact=lambda: None,
        verifier_identity=lambda: verifier,
    )
    monkeypatch.setattr(strata, "make_runner", lambda _: runner)
    monkeypatch.setattr(strata, "snapshot", lambda _: final)
    if mutation == "adopted":
        record["adopted"] = True
    elif mutation == "review":
        record["required"] = []
    elif mutation == "workflow":
        record["workflow"].insert(0, {"stage": "fake pass"})
    elif mutation == "patch":
        patch.write_bytes(b"replacement")
    elif mutation == "snapshot":
        monkeypatch.setattr(strata, "snapshot", lambda _: {})
    elif mutation in ("trace", "session"):
        (directory / ("planner-trace.jsonl" if mutation == "trace" else "planner-session.jsonl")).write_text(
            "changed", encoding="utf-8"
        )
    elif mutation == "binding":
        audit_input["final_bindings"]["candidate_patch_sha256"] = "0" * 64
        write_json(bundle / "audit-input.json", audit_input, exclusive=False)
        record.update(
            audit_input_sha256=file_hash(bundle / "audit-input.json"),
            final_bindings=audit_input["final_bindings"],
        )
    elif mutation == "tokens":
        configuration["output_token_budget"] = 1
    write_json(directory / "external-review.json", record)
    if mutation == "none":
        assert strata.verify_external_proposal(tmp_path / "job.json") == patch
    else:
        with pytest.raises((GateFailure, ValueError)):
            strata.verify_external_proposal(tmp_path / "job.json")
