"""Negative controls for the controller, independent of live inference."""

import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker.formal_artifacts import (
    ARTIFACTS,
    AUDIT_CHECKS,
    CONTROL_PATHS,
    FINAL_BINDINGS,
    GateFailure,
    digest,
    encode,
    file_hash,
    freeze_bundle,
    inspect_bundle,
    read_json,
    safe_path,
    validate_audit_result,
    validate_schema,
)
from ephy_worker.formal_campaign import run_campaign
from ephy_worker.formal_runtime import (
    FormalRunner,
    exclusive_lock,
    injected_context_pins,
    observed_verifier_identity,
    snapshot,
    stage_evidence,
    submit,
    validate_contract,
    validate_observed_audit_output,
)

REPOSITORY = Path(__file__).resolve().parents[1]


def contract():
    return {
        "allowed_files": ["docs/example.md"],
        "semantic_scope": "Improve fixed evaluation instructions",
        "task": "Improve one checklist",
        "checks": [
            {"id": name, "argv": ["{python}", "-V"], "baseline_exit_code": 0}
            for name in ("target", "regression", "lint", "repository", "fixed")
        ],
        "checker": "/external/checker.py",
        "checker_sha256": "a" * 64,
        "checker_controls": "/external/controls.json",
        "checker_controls_sha256": "b" * 64,
        "max_repairs": 2,
        "timeout_seconds": 300,
        "stage_seconds": 60,
        "output_token_budget": 1000,
        "max_requests": 10,
        "max_response_tokens": 400,
        "minimum_free_ram_bytes": 1,
        "minimum_free_disk_bytes": 1,
        "max_process_rss_bytes": 2**40,
        "max_log_bytes": 100000,
        "runtime_hashes": {},
    }


def bundle_fixture(tmp_path):
    artifacts = {name: encode({"artifact": name}) for name in ARTIFACTS}
    for name, path in CONTROL_PATHS.items():
        artifacts[name] = (REPOSITORY / path).read_bytes()
    identity = {
        "model_id": "test-model",
        "model_artifact_manifest_sha256": "a" * 64,
        "runtime_sha256": "b" * 64,
        "invocation_config_sha256": "c" * 64,
    }
    job = {
        "id": "test-job",
        "baseRevision": "d" * 40,
        "contract": contract(),
        "model_identities": {role: identity for role in ("planner", "implementer", "auditor")},
        "verifier_identity": {
            "executor_id": "runner",
            "runtime_sha256": "e" * 64,
            "invocation_config_sha256": "f" * 64,
        },
    }
    artifacts["verification_results"] = encode({"verifier_identity": job["verifier_identity"]})
    bundle = tmp_path / "bundle"
    audit_input = freeze_bundle(bundle, job, artifacts)
    return bundle, audit_input


def accepted_result(bundle, audit_input):
    manifest = read_json(bundle / "evidence-manifest.json")
    entries = {entry["artifact_id"]: entry for entry in manifest["artifacts"]}
    result = {
        "schema_version": "ephy.audit-result.v1",
        "audit_id": audit_input["audit_id"],
        "job_id": audit_input["job_id"],
        "decision": "ACCEPT_PROPOSAL",
        "reason_codes": ["ALL_GATES_PASSED"],
        "bound_inputs": {
            "audit_input_sha256": file_hash(bundle / "audit-input.json"),
            "evidence_manifest_sha256": file_hash(bundle / "evidence-manifest.json"),
            "prompt_template_sha256": entries["audit_prompt"]["sha256"],
            **{
                key: audit_input["final_bindings"][key]
                for key in FINAL_BINDINGS
                if key != "changed_files_manifest_sha256"
            },
        },
        "documents_read": [
            {"artifact_id": name, "sha256": entries[name]["sha256"]} for name in ARTIFACTS[:10]
        ],
        "findings": [],
        "missing_or_invalid_evidence": [],
        "human_report": {
            name: "Synthetic unit test only"
            for name in ("headline", "summary", "candidate_summary", "workflow_summary", "next_action")
        },
        "auditor_assertions": {
            name: False
            for name in (
                "repair_attempted",
                "write_attempted",
                "tests_rerun",
                "network_used",
                "subagent_used",
                "repository_or_release_action_attempted",
            )
        },
    }
    result["human_report"]["blockers"] = []
    for domain, ids in AUDIT_CHECKS.items():
        result[domain] = {
            "status": "PASS",
            "checks": [
                {
                    "check_id": check_id,
                    "status": "PASS",
                    "summary": "Synthetic",
                    "evidence": [
                        {
                            "artifact_id": "task_spec",
                            "sha256": entries["task_spec"]["sha256"],
                            "location": "whole test artifact",
                        }
                    ],
                }
                for check_id in ids
            ],
        }
    return result


@pytest.mark.parametrize("name", ["../outside", "a/../b", "./a", "C:/a", "/a", "a\\b", "a//b"])
def test_path_escape_rejected(tmp_path, name):
    with pytest.raises(GateFailure):
        safe_path(tmp_path, name, missing=True)


def test_hard_link_rejected(tmp_path):
    source = tmp_path / "a"
    source.write_text("data")
    target = tmp_path / "b"
    target.hardlink_to(source)
    with pytest.raises(GateFailure, match="Hard link"):
        safe_path(tmp_path, "b")


def test_bundle_schema_and_actual_bytes(tmp_path):
    bundle, audit_input = bundle_fixture(tmp_path)
    manifest = read_json(bundle / "evidence-manifest.json")
    schema = read_json(bundle / "evidence_manifest_schema.json")
    assert inspect_bundle(bundle, manifest, schema)["all_artifacts_match"]
    result = accepted_result(bundle, audit_input)
    validate_audit_result(result, bundle, audit_input)
    (bundle / "candidate_patch.txt").write_text("modified after freeze")
    with pytest.raises(GateFailure, match="changed"):
        inspect_bundle(bundle, manifest, schema)


@pytest.mark.parametrize(
    "mutation", ["wrong_job", "stale_patch", "invented_citation", "document_hash", "fake_pass"]
)
def test_false_acceptance_rejected(tmp_path, mutation):
    bundle, audit_input = bundle_fixture(tmp_path)
    result = accepted_result(bundle, audit_input)
    if mutation == "wrong_job":
        result["job_id"] = "another-job"
    elif mutation == "stale_patch":
        result["bound_inputs"]["candidate_patch_sha256"] = "0" * 64
    elif mutation == "invented_citation":
        result["candidate"]["checks"][0]["evidence"][0]["artifact_id"] = "fiction"
    elif mutation == "document_hash":
        result["documents_read"][0]["sha256"] = "0" * 64
    else:
        result["workflow"]["checks"][0]["status"] = "FAIL"
    with pytest.raises(GateFailure):
        validate_audit_result(result, bundle, audit_input)


def test_duplicate_manifest_path_and_external_schema_ref(tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    manifest = read_json(bundle / "evidence-manifest.json")
    manifest["artifacts"][1]["path"] = manifest["artifacts"][0]["path"]
    with pytest.raises(GateFailure, match="Duplicate"):
        inspect_bundle(bundle, manifest, read_json(bundle / "evidence_manifest_schema.json"))
    with pytest.raises(GateFailure, match="External"):
        validate_schema({}, {"$ref": "https://example.invalid/schema.json"})


@pytest.mark.parametrize("change", ["scope", "checks", "budget", "repair", "unknown"])
def test_invalid_contract_stops_before_models(change):
    value = contract()
    if change == "scope":
        value["allowed_files"] = ["src/ephy_worker/unsafe.py"]
    elif change == "checks":
        value["checks"].pop()
    elif change == "budget":
        value["output_token_budget"] = 0
    elif change == "repair":
        value["max_repairs"] = 3
    else:
        value["optional_bypass"] = True
    with pytest.raises(GateFailure):
        validate_contract(value)


def test_snapshot_covers_untracked_and_executable_mode(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "new.md").write_text("untracked")
    assert "new.md" in snapshot(tmp_path)
    assert "mode" in snapshot(tmp_path)["new.md"]


def test_kernel_lock_rejects_second_process(tmp_path):
    lock = tmp_path / "lock"
    with exclusive_lock(lock):
        code = "from pathlib import Path; from ephy_worker.formal_runtime import exclusive_lock; "
        code += f"\nwith exclusive_lock(Path({str(lock)!r})): print('unexpected')"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
        assert result.returncode != 0
        assert b"resource lock" in result.stderr
    with exclusive_lock(lock):
        pass


def trace():
    return [
        {"kind": "provider_request", "model": "expected"},
        {"kind": "governance_result", "isError": False, "details": {"role": "planner", "acknowledged": True}},
        {"kind": "tool_call", "tool": "read"},
        {"kind": "assistant", "model": "expected", "stopReason": "stop"},
        {"kind": "stage_end", "failed": False, "outputTokens": 10, "requests": 1},
    ]


def test_stage_provenance_wrong_model_pre_ack_and_violation():
    assert stage_evidence(trace(), "expected", "planner")["trace_valid"]
    cases = [trace(), trace(), trace()]
    cases[0][0]["model"] = "wrong"
    cases[1].insert(0, {"kind": "tool_call", "tool": "write"})
    cases[2].append({"kind": "violation", "reason": "scope escape"})
    for events in cases:
        with pytest.raises(GateFailure):
            stage_evidence(events, "expected", "planner")


def test_campaign_resume_never_replays_interrupted_job(tmp_path, monkeypatch):
    spec = {
        "contract": contract(),
        "runtime": {"resource_lock": str(tmp_path / "lock")},
        "repoRoot": "unused",
        "baseRevision": "a" * 40,
        "controls": {},
        "model_identities": {},
        "verifier_identity": {},
    }
    plan = {
        "specs": [spec, copy.deepcopy(spec)],
        "max_consecutive_failures": 1,
        "timeout_seconds": 1000,
        "resource_lock": str(tmp_path / "lock"),
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_bytes(encode(plan))
    state_root = tmp_path / "campaign"
    state_root.mkdir()
    job_file = submit(spec, state_root)
    state = {
        "plan_sha256": digest(encode(plan)),
        "status": "running",
        "results": [],
        "next_index": 0,
        "consecutive_failures": 0,
        "active_job": str(job_file),
        "elapsed_seconds": 0,
    }
    (state_root / "campaign.json").write_bytes(encode(state))
    monkeypatch.setattr(
        "ephy_worker.formal_campaign.FormalRunner.run",
        lambda *_: pytest.fail("Interrupted job must not be replayed"),
    )
    result = run_campaign(plan_file, state_root, resume=True)
    assert result["status"] == "stopped"
    assert result["next_index"] == 1
    assert read_json(job_file)["status"] == "failed"
    assert len(list((state_root / "jobs").iterdir())) == 1


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "ambiguous.json"
    path.write_text('{"x": 1, "x": 2}')
    with pytest.raises(GateFailure, match="Duplicate"):
        read_json(path)


@pytest.mark.parametrize("mutation", ["omit", "invent", "duplicate"])
def test_complete_mandatory_audit_checks_required(tmp_path, mutation):
    bundle, audit_input = bundle_fixture(tmp_path)
    result = accepted_result(bundle, audit_input)
    if mutation == "omit":
        result["workflow"]["checks"].pop()
    elif mutation == "invent":
        for domain in AUDIT_CHECKS:
            result[domain]["checks"] = result[domain]["checks"][:1]
            result[domain]["checks"][0]["check_id"] = {
                "integrity": "I01",
                "candidate": "C01",
                "workflow": "W01",
            }[domain] + "_TEST"
    else:
        result["candidate"]["checks"].append(copy.deepcopy(result["candidate"]["checks"][0]))
    with pytest.raises(GateFailure):
        validate_audit_result(result, bundle, audit_input)


def test_actual_gate_adjacent_context_paths_are_pinned(tmp_path):
    managed = tmp_path / "runtime"
    gate = managed / "extensions" / "governance-gate.ts"
    gate.parent.mkdir(parents=True)
    gate.write_text("gate")
    mapping = {
        "policies/independent-audit.md": "audit_contract",
        "prompts/audit-ephy-worker.md": "audit_prompt",
        "policies/audit-input.schema.json": "audit_input_schema",
        "policies/evidence-manifest.schema.json": "evidence_manifest_schema",
        "policies/audit-result.schema.json": "audit_result_schema",
    }
    controls = {}
    for relative, name in mapping.items():
        path = managed / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(name)
        controls[name] = file_hash(path)
    pins = injected_context_pins({"governance_gate": str(gate)}, controls)
    assert len(pins) == 5
    assert all(file_hash(Path(path)) == expected for path, expected in pins.items())
    (managed / "policies/independent-audit.md").write_text("stale or changed")
    assert (
        file_hash(managed / "policies/independent-audit.md")
        != pins[str(managed / "policies/independent-audit.md")]
    )


def test_model_rehash_detects_change_after_preflight(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"original")
    ini = tmp_path / "models.ini"
    ini.write_text(f"version=1\n[*]\nc=32768\n[test]\nmodel={model.as_posix()}\n", encoding="utf-8")
    runner = FormalRunner.__new__(FormalRunner)
    runner.resources = lambda: None
    runner.runtime = {
        "models_ini": str(ini),
        "model_manifests": {
            "test": [{"path": str(model), "size_bytes": model.stat().st_size, "sha256": file_hash(model)}]
        },
    }
    runner.verify_model_artifacts("test")
    model.write_bytes(b"modified")  # same size; size checking alone must not pass
    with pytest.raises(GateFailure, match="Model artifact changed"):
        runner.verify_model_artifacts("test")
    other = tmp_path / "different-model.gguf"
    other.write_bytes(b"original")
    ini.write_text(f"[test]\nmodel={other.as_posix()}\n", encoding="utf-8")
    with pytest.raises(GateFailure, match="exact router preset"):
        runner.verify_model_artifacts("test")


@pytest.mark.parametrize("during", ["startup", "unload"])
def test_model_replaced_while_waiting_is_rejected_before_load(tmp_path, monkeypatch, during):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"original")
    ini = tmp_path / "models.ini"
    ini.write_text(f"[test]\nmodel={model.as_posix()}\n", encoding="utf-8")
    runner = FormalRunner.__new__(FormalRunner)
    runner.runtime = {
        "models_ini": str(ini),
        "model_manifests": {"test": [{"path": str(model), "size_bytes": 8, "sha256": file_hash(model)}]},
    }
    runner.server = None if during == "startup" else SimpleNamespace(pid=1)
    runner.loaded_model = None if during == "startup" else "other"
    runner.deadline = time.monotonic() + 60
    runner.resources = lambda: None
    runner.state = lambda *_: None
    routes = []

    def startup():
        model.write_bytes(b"modified")
        runner.server = SimpleNamespace(pid=1)

    def router(route, body=None):
        routes.append(route)
        if route == "/models" and during == "unload":
            model.write_bytes(b"modified")
        assert route != "/models/load", "Wrong bytes reached the load request"
        return {"data": [{"id": "test", "status": {"value": "unloaded"}}]}

    runner.start_server = startup
    runner.router_request = router
    monkeypatch.setattr("ephy_worker.formal_runtime.time.sleep", lambda *_: None)
    with pytest.raises(GateFailure, match="Model artifact changed"):
        runner.load_model("test")
    assert routes.count("/models") == 3
    assert "/models/load" not in routes


def verifier_runner(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    candidate = tmp_path / "candidate"
    subprocess.run(["git", "init", str(candidate)], check=True, capture_output=True)
    (candidate / "doc.md").write_text("unchanged", encoding="utf-8")
    runner = FormalRunner.__new__(FormalRunner)
    runner.runtime = {"python": sys.executable}
    runner.contract = contract()
    from ephy_worker.formal_runtime import executable_identity

    runtime = REPOSITORY / "src/ephy_worker/formal_runtime.py"
    artifacts = REPOSITORY / "src/ephy_worker/formal_artifacts.py"
    pins = {str(path.resolve()): file_hash(path) for path in (Path(sys.executable), runtime, artifacts)}
    git_identity = executable_identity("git")
    pins[git_identity["path"]] = git_identity["sha256"]
    runner.contract["runtime_hashes"] = pins
    runner.job = {
        "verifier_identity": observed_verifier_identity(runner.runtime, runner.contract),
        "environment_sha256": "e" * 64,
    }
    runner.directory, runner.candidate = directory, candidate
    runner.baseline_snapshot = snapshot(candidate)
    runner.transcripts = []
    runner.deadline = time.monotonic() + 60
    runner.resources = runner.intact = lambda: None
    return runner


def test_observed_verifier_matches_real_check_processes(tmp_path):
    runner = verifier_runner(tmp_path)
    result = runner.run_checks("verification")
    assert result["passed"]
    assert result["verifier_identity"] == runner.job["verifier_identity"]
    assert all(check["pid"] > 0 and check["exit_code"] == 0 for check in result["checks"])
    assert all(
        check["executable"]["path"] == str(Path(sys.executable).resolve()) for check in result["checks"]
    )
    assert len({check["effective_environment_sha256"] for check in result["checks"]}) == 1


@pytest.mark.parametrize("field", ["executor_id", "runtime_sha256", "invocation_config_sha256"])
def test_fabricated_verifier_identity_stops_before_checks(tmp_path, field):
    runner = verifier_runner(tmp_path)
    runner.job["verifier_identity"][field] = "fabricated"
    runner.command = lambda *_: pytest.fail("Fabricated identity reached verifier command")
    with pytest.raises(GateFailure, match="Observed independent verifier"):
        runner.run_checks("verification")


def test_wrong_actual_verifier_python_is_rejected(tmp_path):
    runner = verifier_runner(tmp_path)
    fake = tmp_path / "other-python.exe"
    fake.write_bytes(b"different executable")
    runner.runtime["python"] = str(fake)
    with pytest.raises(GateFailure, match="running interpreter"):
        runner.verifier_identity()


def test_freeze_requires_observed_verifier(tmp_path):
    bundle, audit_input = bundle_fixture(tmp_path)
    manifest = read_json(bundle / "evidence-manifest.json")
    entries = {entry["artifact_id"]: entry for entry in manifest["artifacts"]}
    artifacts = {name: (bundle / entries[name]["path"]).read_bytes() for name in ARTIFACTS}
    job = {"verifier_identity": audit_input["stage_contract"]["verifier_identity"]}
    artifacts["verification_results"] = encode({"verifier_identity": {"executor_id": "fabricated"}})
    with pytest.raises(GateFailure, match="observed independent verification"):
        freeze_bundle(tmp_path / "new-bundle", job, artifacts)
    assert not (tmp_path / "new-bundle").exists()


@pytest.mark.parametrize("mutation", ["none", "result", "raw", "session", "stdout", "failed_session"])
def test_auditor_output_binding_rejects_forged_accept(tmp_path, mutation):
    # Genuine rejection plus forged acceptance must never be rescued by rehashing an attestation.
    rejection = {"decision": "REJECT_PROPOSAL"}
    acceptance = {"decision": "ACCEPT_PROPOSAL"}
    raw = json.dumps(rejection)
    message = {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": raw}]}
    session = {"type": "message", "message": copy.deepcopy(message)}
    stdout = {"type": "message_end", "message": copy.deepcopy(message)}
    result = rejection
    if mutation == "result":
        result = acceptance
    elif mutation == "raw":
        raw = json.dumps(acceptance)
        result = acceptance
    elif mutation in ("session", "stdout"):
        record = session if mutation == "session" else stdout
        record["message"]["content"][0]["text"] = json.dumps(acceptance)
    elif mutation == "failed_session":
        session["message"]["stopReason"] = "error"
    (tmp_path / "audit-result.raw.txt").write_text(raw, encoding="utf-8")
    (tmp_path / "auditor-session.jsonl").write_text(json.dumps(session) + "\n", encoding="utf-8")
    (tmp_path / "auditor.stdout.log").write_text(json.dumps(stdout) + "\n", encoding="utf-8")
    if mutation == "none":
        validate_observed_audit_output(tmp_path, result)
    else:
        with pytest.raises(GateFailure):
            validate_observed_audit_output(tmp_path, result)
