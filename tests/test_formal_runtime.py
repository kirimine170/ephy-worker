"""Negative controls for the controller, independent of live inference."""

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker.formal_artifacts import (
    ARTIFACTS,
    AUDIT_CHECKS,
    AUDIT_EVIDENCE,
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
    write_json,
)
from ephy_worker.formal_campaign import run_campaign
from ephy_worker.formal_runtime import (
    FormalRunner,
    command_environment,
    exclusive_lock,
    freeze_submission_identity,
    injected_context_pins,
    observed_audit_artifacts,
    observed_verifier_identity,
    snapshot,
    stage_evidence,
    submit,
    validate_contract,
    validate_observed_audit_output,
    validate_proposal_stop,
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
                            "artifact_id": name,
                            "sha256": entries[name]["sha256"],
                            "location": "whole test artifact",
                        }
                        for name in AUDIT_EVIDENCE[check_id]
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
    validate_audit_result(result, bundle, audit_input, set(ARTIFACTS))
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
        validate_audit_result(result, bundle, audit_input, set(ARTIFACTS))


@pytest.mark.parametrize("check_id", [item for ids in AUDIT_CHECKS.values() for item in ids])
def test_pass_without_required_relevant_evidence_is_rejected(tmp_path, check_id):
    bundle, audit_input = bundle_fixture(tmp_path)
    result = accepted_result(bundle, audit_input)
    check = next(
        check
        for domain in AUDIT_CHECKS
        for check in result[domain]["checks"]
        if check["check_id"] == check_id
    )
    check["evidence"].pop()
    with pytest.raises(GateFailure, match="relevant audit evidence coverage"):
        validate_audit_result(result, bundle, audit_input, set(ARTIFACTS))


def test_all_task_spec_citations_cannot_authorize_acceptance(tmp_path):
    bundle, audit_input = bundle_fixture(tmp_path)
    result = accepted_result(bundle, audit_input)
    task = next(item for item in result["documents_read"] if item["artifact_id"] == "task_spec")
    for domain in AUDIT_CHECKS:
        for check in result[domain]["checks"]:
            check["evidence"] = [{**task, "location": "whole artifact"}]
    with pytest.raises(GateFailure, match="relevant audit evidence coverage"):
        validate_audit_result(result, bundle, audit_input, set(ARTIFACTS))


def test_audit_evidence_mapping_covers_exact_contract_checks():
    assert set(AUDIT_EVIDENCE) == {item for ids in AUDIT_CHECKS.values() for item in ids}
    assert all(set(names) <= set(ARTIFACTS) and names for names in AUDIT_EVIDENCE.values())


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
    retain_campaign_fixture_freeze(state_root, plan, state)
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
        validate_audit_result(result, bundle, audit_input, set(ARTIFACTS))


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


def test_role_change_uses_same_verifier_environment_and_real_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("DUAL_GOVERNANCE_ROLE", "planner")
    runner = verifier_runner(tmp_path)
    before = command_environment(runner.candidate, runner.directory / "temp")
    monkeypatch.setenv("DUAL_GOVERNANCE_ROLE", "integration")
    monkeypatch.setenv("DUAL_STAGE_TRACE", "integration-trace")
    monkeypatch.setenv("PYTHONSTARTUP", "untrusted.py")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    assert command_environment(runner.candidate, runner.directory / "temp") == before
    result = runner.run_checks("integration")
    assert result["passed"] and result["verifier_identity"] == runner.job["verifier_identity"]
    assert all(check["pid"] > 0 for check in result["checks"])
    assert all(check["effective_environment_sha256"] == digest(encode(before)) for check in result["checks"])


def test_changed_verifier_platform_environment_still_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LANG", "C.UTF-8")
    runner = verifier_runner(tmp_path)
    monkeypatch.setenv("LANG", "different-locale")
    runner.command = lambda *_: pytest.fail("Changed environment reached check process")
    with pytest.raises(GateFailure, match="Observed independent verifier"):
        runner.run_checks("verification")


def test_verifier_tempfile_uses_declared_private_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path / "shared"))
    monkeypatch.setenv("TMP", str(tmp_path / "other-shared"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "posix-shared"))
    runner = verifier_runner(tmp_path)
    command = [
        sys.executable,
        "-c",
        "import tempfile; print(tempfile.gettempdir()); f=tempfile.NamedTemporaryFile(); print(f.name)",
    ]
    result = runner.command(command, runner.candidate, "temp-probe", 30)
    output = (runner.directory / result["stdout"]).read_text(encoding="utf-8").splitlines()
    assert Path(output[0]) == runner.directory / "temp"
    assert Path(output[1]).parent == runner.directory / "temp"
    assert set(result["temp_variables"].values()) == {str(runner.directory / "temp")}


def test_managed_stage_environment_reaches_real_process_without_verifier_inheritance(tmp_path):
    runner = verifier_runner(tmp_path)
    # The controller supplies stage-only configuration explicitly, never through os.environ.
    stage = {
        "DUAL_GOVERNANCE_ROLE": "auditor",
        "PI_CODING_AGENT_DIR": "managed",
        "EPHY_FORMAL_STAGE_CONFIG": "config",
        "EPHY_FORMAL_TRACE": "trace",
    }
    result = runner.command(
        [
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({k:os.environ[k] for k in " + repr(list(stage)) + "}))",
        ],
        runner.candidate,
        "stage-env-probe",
        30,
        stage_environment=stage,
    )
    assert read_json(runner.directory / result["stdout"]) == stage
    assert not set(stage) & set(command_environment(runner.candidate, runner.directory / "temp"))


@pytest.mark.parametrize("mutation", ["none", "no_reads", "wrong_hash", "missing_call", "partial"])
def test_audit_citations_require_observed_successful_delivery(tmp_path, mutation):
    bundle, audit_input = bundle_fixture(tmp_path)
    entries = read_json(bundle / "evidence-manifest.json")["artifacts"]
    events = trace()
    events[0]["model"] = events[3]["model"] = "gpt-oss-20b-MXFP4"
    events[1]["details"]["role"] = "auditor"
    events.insert(2, {"kind": "provider_request", "model": "gpt-oss-20b-MXFP4"})
    for entry in entries:
        name = entry["artifact_id"]
        if mutation == "no_reads":
            continue
        if mutation != "missing_call" or name != "task_spec":
            events.insert(
                -2,
                {
                    "kind": "tool_call",
                    "tool": "read",
                    "path": entry["path"],
                    "toolCallId": name,
                    "blocked": False,
                },
            )
        events.insert(
            -2,
            {
                "kind": "evidence_read",
                "path": entry["path"],
                "toolCallId": name,
                "sha256": "0" * 64 if mutation == "wrong_hash" and name == "task_spec" else entry["sha256"],
                "full_content_delivered": not (mutation == "partial" and name == "task_spec"),
            },
        )
    result = accepted_result(bundle, audit_input)
    if mutation == "none":
        observed = observed_audit_artifacts(bundle, events)
        assert observed == set(ARTIFACTS)
        validate_audit_result(result, bundle, audit_input, observed)
    else:
        with pytest.raises(GateFailure, match="observed|matching"):
            validate_audit_result(result, bundle, audit_input, observed_audit_artifacts(bundle, events))


def test_injected_document_requires_actual_matching_context_bytes(tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    entries = {e["artifact_id"]: e for e in read_json(bundle / "evidence-manifest.json")["artifacts"]}
    data = (bundle / "audit_contract.txt").read_bytes()
    path = "policies/independent-audit.md"
    sha = entries["audit_contract"]["sha256"]
    events = trace()
    events[0]["model"] = events[3]["model"] = "gpt-oss-20b-MXFP4"
    ack = events[1]
    ack["details"].update(
        role="auditor",
        policySha256=entries["system_development_policy"]["sha256"],
        requiredContext=[{"path": path, "sha256": sha, "bytes": len(data), "lines": 1}],
    )
    ack["content"] = [
        {
            "type": "text",
            "text": f"BEGIN REQUIRED GOVERNANCE DOCUMENT path={path} "
            f"sha256={sha} bytes={len(data)} lines=1\n"
            + data.decode()
            + f"\nEND REQUIRED GOVERNANCE DOCUMENT path={path} sha256={sha}",
        }
    ]
    events.insert(2, {"kind": "provider_request", "model": "gpt-oss-20b-MXFP4"})
    assert observed_audit_artifacts(bundle, events) == {"system_development_policy", "audit_contract"}
    ack["content"][0]["text"] = "claimed but not delivered"
    assert observed_audit_artifacts(bundle, events) == {"system_development_policy"}


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "missing_stop",
        "wrong_decision",
        "extra_event",
        "wrong_prefix",
        "wrong_trace",
        "wrong_hash",
        "missing_load",
        "duplicate_load",
        "wrong_load_model",
        "wrong_router_model",
        "not_loaded",
        "different_server",
        "invalid_server",
        "load_after_start",
    ],
)
def test_post_audit_stop_is_bound_and_rechecked(tmp_path, monkeypatch, mutation):
    bundle, audit_input = bundle_fixture(tmp_path)
    frozen = [
        {
            "stage": "freeze",
            "proposal_stop_required": True,
            "controller_sha256": file_hash(REPOSITORY / "src/ephy_worker/formal_runtime.py"),
            "server_pid": 456,
        }
    ]
    (bundle / "workflow_events.txt").write_bytes(encode(frozen))
    (tmp_path / "audit-result.json").write_bytes(encode({"decision": "ACCEPT_PROPOSAL"}))
    (tmp_path / "auditor-trace.jsonl").write_text("trace", encoding="utf-8")
    trace_sha = file_hash(tmp_path / "auditor-trace.jsonl")
    # Execute the production load boundary with an offline router, rather than omit its event.
    runner = FormalRunner.__new__(FormalRunner)
    runner.directory = tmp_path
    runner.server = SimpleNamespace(pid=456)
    runner.loaded_model = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M"
    runner.deadline = time.monotonic() + 60
    runner.events = copy.deepcopy(frozen)
    runner.resources = lambda: None
    runner.state = lambda *_: None
    runner.verify_model_artifacts = lambda model: None
    routes = []
    loaded = False

    def router(route, body=None):
        nonlocal loaded
        routes.append(route)
        if route == "/models/load":
            assert body == {"model": "gpt-oss-20b-MXFP4"}
            loaded = True
        return {
            "data": [{"id": "gpt-oss-20b-MXFP4", "status": {"value": "loaded" if loaded else "unloaded"}}]
        }

    runner.router_request = router
    monkeypatch.setattr("ephy_worker.formal_runtime.time.sleep", lambda *_: None)
    runner.load_model("gpt-oss-20b-MXFP4")
    assert routes == ["/models/unload", "/models", "/models", "/models", "/models/load", "/models"]
    post = {
        "job_id": audit_input["job_id"],
        "frozen_workflow_sha256": file_hash(bundle / "workflow_events.txt"),
        "audit_result_sha256": file_hash(tmp_path / "audit-result.json"),
        "audit_trace_sha256": trace_sha,
        "events": runner.events
        + [
            {"stage": "auditor start", "expected_model": "gpt-oss-20b-MXFP4"},
            {"stage": "auditor end", "pid": 123, "trace_sha256": trace_sha},
            {"stage": "proposal stop", "decision": "ACCEPT_PROPOSAL"},
        ],
    }
    if mutation == "missing_stop":
        post["events"].pop()
    elif mutation == "wrong_decision":
        post["events"][-1]["decision"] = "REJECT_PROPOSAL"
    elif mutation == "extra_event":
        post["events"].append({"stage": "apply"})
    elif mutation == "wrong_prefix":
        post["events"][0] = {"stage": "different freeze"}
    elif mutation == "wrong_trace":
        post["audit_trace_sha256"] = "0" * 64
    elif mutation == "missing_load":
        post["events"].pop(1)
    elif mutation == "duplicate_load":
        post["events"].insert(1, copy.deepcopy(post["events"][1]))
    elif mutation == "wrong_load_model":
        post["events"][1]["model"] = "other-model"
    elif mutation == "wrong_router_model":
        post["events"][1]["router_entry"]["id"] = "other-model"
    elif mutation == "not_loaded":
        post["events"][1]["router_entry"]["status"]["value"] = "unloaded"
    elif mutation == "different_server":
        post["events"][1]["server_pid"] = 789
    elif mutation == "invalid_server":
        post["events"][1]["server_pid"] = 0
    elif mutation == "load_after_start":
        post["events"][1], post["events"][2] = post["events"][2], post["events"][1]
    (tmp_path / "post-audit-workflow.json").write_bytes(encode(post))
    attestation = {
        "proposal_stopped": True,
        "observed_auditor": {"pid": 123},
        "post_audit_workflow_sha256": file_hash(tmp_path / "post-audit-workflow.json"),
    }
    if mutation == "wrong_hash":
        attestation["post_audit_workflow_sha256"] = "0" * 64
    if mutation == "none":
        validate_proposal_stop(tmp_path, bundle, {"decision": "ACCEPT_PROPOSAL"}, attestation)
    else:
        with pytest.raises(GateFailure, match="proposal stop binding"):
            validate_proposal_stop(tmp_path, bundle, {"decision": "ACCEPT_PROPOSAL"}, attestation)


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


def verifier_observations(runner):
    return [
        json.loads(line) for line in (runner.directory / "verifier-identity.jsonl").read_text().splitlines()
    ]


def verifier_draft(runner):
    return {
        "repoRoot": str(runner.candidate),
        "baseRevision": "d" * 40,
        "contract": runner.contract,
        "runtime": runner.runtime,
        "controls": {},
        "model_identities": {},
    }


def campaign_fixture(tmp_path):
    runner = verifier_runner(tmp_path)
    draft = verifier_draft(runner)
    draft["runtime"]["resource_lock"] = str(tmp_path / "campaign.lock")
    plan = {
        "specs": [copy.deepcopy(draft), copy.deepcopy(draft)],
        "max_consecutive_failures": 3,
        "timeout_seconds": 1000,
        "resource_lock": draft["runtime"]["resource_lock"],
    }
    path = tmp_path / "plan.json"
    write_json(path, plan)
    return path, tmp_path / "campaign", runner


def retain_campaign_fixture_freeze(root, plan, state):
    # Legacy interruption fixtures are synthetic metadata, never live inference.
    freeze = {
        "plan_sha256": digest(encode(plan)),
        "verifier_identities": [spec["verifier_identity"] for spec in plan["specs"]],
        "observations": [{} for _ in plan["specs"]],
    }
    write_json(root / "verifier-freeze.json", freeze)
    state["verifier_freeze_sha256"] = digest(encode(freeze))


@pytest.mark.parametrize("status", ["completed", "stopped", "failed"])
@pytest.mark.parametrize("mutation", ["none", "missing", "changed"])
def test_terminal_campaign_resume_validates_freeze_before_return(tmp_path, monkeypatch, status, mutation):
    plan_file, root, _ = campaign_fixture(tmp_path)

    def run(runner):
        runner.verifier_identity()
        runner.state("review_ready", "Offline identity control only")

    monkeypatch.setattr(FormalRunner, "run", run)
    run_campaign(plan_file, root)
    state_file = root / "campaign.json"
    terminal = read_json(state_file)
    terminal["status"] = status
    write_json(state_file, terminal, exclusive=False)
    original_state = state_file.read_bytes()
    freeze_file = root / "verifier-freeze.json"
    if mutation == "missing":
        freeze_file.unlink()
    elif mutation == "changed":
        freeze = read_json(freeze_file)
        freeze["verifier_identities"][0]["runtime_sha256"] = "0" * 64
        write_json(freeze_file, freeze, exclusive=False)
    monkeypatch.setattr(FormalRunner, "run", lambda *_: pytest.fail("Terminal campaign restarted"))
    if mutation == "none":
        assert run_campaign(plan_file, root, resume=True) == terminal
    else:
        with pytest.raises(GateFailure, match="cannot recapture"):
            run_campaign(plan_file, root, resume=True)
    assert state_file.read_bytes() == original_state
    assert len(list((root / "jobs").iterdir())) == 2


@pytest.mark.parametrize("mutation", ["none", "PATH", "LANG"])
def test_standard_campaign_freezes_drafts_then_stops_drift_before_next_submission(
    tmp_path, monkeypatch, mutation
):
    plan_file, root, _ = campaign_fixture(tmp_path)
    calls = []

    def run(runner):
        runner.verifier_identity()
        calls.append(runner.job["id"])
        runner.state("review_ready", "Offline identity control only")
        if len(calls) == 1 and mutation != "none":
            monkeypatch.setenv(mutation, os.environ.get(mutation, "") + os.pathsep + "after-freeze")

    monkeypatch.setattr(FormalRunner, "run", run)
    if mutation == "none":
        result = run_campaign(plan_file, root)
        assert result["status"] == "completed" and len(calls) == 2
    else:
        with pytest.raises(GateFailure, match="changed after freeze"):
            run_campaign(plan_file, root)
        result = read_json(root / "campaign.json")
        assert result["status"] == "failed" and len(calls) == 1
        assert result["next_index"] == 1
    freeze = read_json(root / "verifier-freeze.json")
    assert result["verifier_freeze_sha256"] == digest(encode(freeze))
    assert all("verifier_identity" not in draft for draft in read_json(plan_file)["specs"])
    comparison = read_json(root / "verifier-submission-1.json")
    assert comparison["expected_identity"] == freeze["verifier_identities"][1]
    assert comparison["matches"] is (mutation == "none")
    assert len(list((root / "jobs").iterdir())) == len(calls)


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_fresh_campaign_validates_retained_evidence_before_reporting_completion(
    tmp_path, monkeypatch, mutation
):
    plan_file, root, _ = campaign_fixture(tmp_path)
    calls = []

    def run(runner):
        runner.verifier_identity()
        calls.append(runner.job["id"])
        runner.state("review_ready", "Offline identity control only")
        if len(calls) == 2:
            path = root / "verifier-freeze.json"
            if mutation == "missing":
                path.unlink()
            else:
                record = read_json(path)
                record["verifier_identities"][1]["runtime_sha256"] = "0" * 64
                write_json(path, record, exclusive=False)

    monkeypatch.setattr(FormalRunner, "run", run)
    with pytest.raises(GateFailure, match="cannot recapture"):
        run_campaign(plan_file, root)
    assert read_json(root / "campaign.json")["status"] == "completed"
    assert len(calls) == 2 and len(list((root / "jobs").iterdir())) == 2
    assert all(read_json(job)["status"] == "review_ready" for job in (root / "jobs").glob("*/job.json"))


def test_standard_campaign_refuses_pre_frozen_parent_spec(tmp_path, monkeypatch):
    plan_file, root, parent = campaign_fixture(tmp_path)
    plan = read_json(plan_file)
    for spec in plan["specs"]:
        spec["verifier_identity"] = parent.job["verifier_identity"]
    write_json(plan_file, plan, exclusive=False)
    monkeypatch.setattr(FormalRunner, "run", lambda *_: pytest.fail("Pre-frozen spec reached a job"))
    with pytest.raises(GateFailure, match="unfrozen submission draft"):
        run_campaign(plan_file, root)
    assert not root.exists()


def test_standard_campaign_uses_actual_child_launch_environment(tmp_path):
    plan_file, root, parent = campaign_fixture(tmp_path)
    env = command_environment(REPOSITORY, tmp_path / "child-temp")
    env["PATH"] += os.pathsep + str(tmp_path / "different-campaign-controller")
    script = """
import sys
from pathlib import Path
from ephy_worker.formal_runtime import FormalRunner
from ephy_worker.formal_campaign import run_campaign
def run(runner):
    runner.verifier_identity()
    runner.state('review_ready','Offline identity control only')
FormalRunner.run=run
run_campaign(Path(sys.argv[1]),Path(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(plan_file), str(root)],
        env=env, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    freeze = read_json(root / "verifier-freeze.json")
    assert freeze["verifier_identities"][0] != parent.job["verifier_identity"]
    assert freeze["observations"][0]["environment_value_sha256"]["PATH"] == digest(env["PATH"].encode())
    jobs = list((root / "jobs").glob("*/job.json"))
    assert len(jobs) == 2
    assert all(read_json(job)["verifier_identity"] == freeze["verifier_identities"][0] for job in jobs)


@pytest.mark.parametrize("mutation", ["none", "PATH", "record", "missing"])
def test_campaign_resume_preserves_original_expectations_and_deadline(tmp_path, monkeypatch, mutation):
    plan_file, root, _ = campaign_fixture(tmp_path)
    calls = []

    def run(runner):
        runner.verifier_identity()
        calls.append(runner.job["id"])
        runner.state("review_ready", "Offline identity control only")
        if len(calls) == 1:
            raise KeyboardInterrupt("Offline controller pause after completed job")

    monkeypatch.setattr(FormalRunner, "run", run)
    with pytest.raises(KeyboardInterrupt):
        run_campaign(plan_file, root)
    before = read_json(root / "campaign.json")
    freeze_file = root / "verifier-freeze.json"
    original = freeze_file.read_bytes()
    if mutation == "PATH":
        monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + "changed-on-resume")
    elif mutation == "record":
        record = read_json(freeze_file)
        record["verifier_identities"][1]["invocation_config_sha256"] = "0" * 64
        write_json(freeze_file, record, exclusive=False)
    elif mutation == "missing":
        freeze_file.unlink()
    if mutation == "none":
        after = run_campaign(plan_file, root, resume=True)
        assert after["status"] == "completed" and len(calls) == 2
    else:
        with pytest.raises(GateFailure, match="cannot recapture"):
            run_campaign(plan_file, root, resume=True)
        after = read_json(root / "campaign.json")
        assert after["status"] == "failed" and len(calls) == 1
    assert after["deadline_at"] == before["deadline_at"]
    assert after["verifier_freeze_sha256"] == before["verifier_freeze_sha256"]
    if mutation in ("none", "PATH"):
        assert freeze_file.read_bytes() == original
    assert len(list((root / "jobs").iterdir())) == len(calls)


def test_fresh_submission_freeze_copies_inputs_and_validates_pins(tmp_path):
    runner = verifier_runner(tmp_path)
    draft = verifier_draft(runner)
    original = copy.deepcopy(draft)
    observation = {}
    frozen = freeze_submission_identity(draft, observation=observation)
    assert draft == original and "verifier_identity" not in draft
    assert frozen["verifier_identity"] == runner.job["verifier_identity"]
    assert observation["phase"] == "complete"
    draft["contract"]["task"] = "Changed outside the frozen copy"
    assert frozen["contract"]["task"] == original["contract"]["task"]
    draft["contract"]["runtime_hashes"][str(Path(sys.executable).resolve())] = "0" * 64
    with pytest.raises(GateFailure, match="matching frozen pin"):
        freeze_submission_identity(draft)


@pytest.mark.parametrize("extra", ["verifier_identity", "id", "status"])
def test_freeze_refuses_existing_specs_jobs_and_unknown_fields(tmp_path, extra):
    draft = verifier_draft(verifier_runner(tmp_path))
    draft[extra] = "existing"
    with pytest.raises(GateFailure, match="unfrozen submission draft"):
        freeze_submission_identity(draft)


@pytest.mark.parametrize("mutation", ["none", "PATH", "checks"])
def test_background_controller_freezes_its_launch_environment_then_rejects_drift(tmp_path, mutation):
    runner = verifier_runner(tmp_path)
    spec_file = tmp_path / "draft.json"
    write_json(spec_file, {"draft": verifier_draft(runner), "directory": str(runner.directory)})
    env = command_environment(REPOSITORY, tmp_path / "child-temp")
    env["PATH"] += os.pathsep + str(tmp_path / "different-background-launch")
    script = """
import json,os,sys
from pathlib import Path
from ephy_worker.formal_runtime import FormalRunner,freeze_submission_identity
from ephy_worker.formal_artifacts import GateFailure,write_json
data=json.loads(Path(sys.argv[1]).read_text())
observation={}
spec=freeze_submission_identity(data['draft'],observation=observation)
directory=Path(data['directory'])
write_json(directory/'frozen-spec.json',spec)
write_json(directory/'freeze-observation.json',observation)
runner=FormalRunner.__new__(FormalRunner)
runner.runtime,runner.contract,runner.job=spec['runtime'],spec['contract'],spec
runner.directory=directory
runner.verifier_identity()
if sys.argv[2]=='PATH':
    os.environ['PATH']+=os.pathsep+'changed-after-freeze'
elif sys.argv[2]=='checks':
    runner.contract['checks'][0]['argv'][1]='--version'
try:
    runner.verifier_identity()
except GateFailure:
    sys.exit(78)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(spec_file), mutation],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == (0 if mutation == "none" else 78), result.stderr.decode()
    frozen = read_json(runner.directory / "frozen-spec.json")["verifier_identity"]
    assert frozen != runner.job["verifier_identity"]
    observation = read_json(runner.directory / "freeze-observation.json")
    assert observation["environment_value_sha256"]["PATH"] == digest(env["PATH"].encode())
    before, after = verifier_observations(runner)
    assert before["decision"] == "matched" and before["observed_identity"] == frozen
    assert after["expected_identity"] == frozen
    assert after["decision"] == ("matched" if mutation == "none" else "identity_mismatch")
    assert (after["observed_identity"] == frozen) is (mutation == "none")
    if mutation == "PATH":
        assert after["environment_sha256"] != before["environment_sha256"]
        assert after["checks_sha256"] == before["checks_sha256"]
    if mutation == "checks":
        assert after["checks_sha256"] != before["checks_sha256"]
        assert after["environment_sha256"] == before["environment_sha256"]
    assert before["files"] == after["files"]


def test_verifier_diagnostic_is_retained_before_real_checks_and_redacts_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "private-verifier-environment-value")
    monkeypatch.setenv("API_KEY", "private-auth-value-never-an-input")
    runner = verifier_runner(tmp_path)
    original = runner.command

    def command(*args):
        recorded = verifier_observations(runner)[-1]
        assert recorded["decision"] == "matched"
        assert recorded["expected_identity"] == recorded["observed_identity"]
        return original(*args)

    runner.command = command
    assert runner.run_checks("diagnostic")["passed"]
    raw = (runner.directory / "verifier-identity.jsonl").read_text()
    assert "private-verifier-environment-value" not in raw
    assert "private-auth-value-never-an-input" not in raw
    assert str(Path(sys.executable).resolve()) not in raw
    assert str(REPOSITORY) not in raw
    assert "API_KEY" not in verifier_observations(runner)[0]["environment_value_sha256"]


@pytest.mark.parametrize("key", ["LANG", "PATH"])
def test_launch_environment_difference_records_changed_component_before_rejection(tmp_path, monkeypatch, key):
    runner = verifier_runner(tmp_path)
    assert runner.verifier_identity() == runner.job["verifier_identity"]
    before = verifier_observations(runner)[0]
    suffix = os.pathsep + str(tmp_path / "unused-launch-path") if key == "PATH" else "-changed"
    monkeypatch.setenv(key, os.environ.get(key, "") + suffix)
    runner.command = lambda *_: pytest.fail("Environment mismatch reached a command")
    with pytest.raises(GateFailure, match="Observed independent verifier"):
        runner.run_checks("rejected")
    records = verifier_observations(runner)
    assert len(records) == 2 and records[0] == before
    after = records[1]
    assert after["decision"] == "identity_mismatch" and not after["matches"]
    assert after["expected_identity"] != after["observed_identity"]
    assert after["environment_value_sha256"][key] != before["environment_value_sha256"].get(key)
    assert after["checks_sha256"] == before["checks_sha256"]
    assert after["files"] == before["files"]


def test_equivalent_python_path_normalizes_without_changing_identity(tmp_path):
    runner = verifier_runner(tmp_path)
    python = Path(sys.executable)
    runner.runtime["python"] = os.path.join(str(python.parent), ".", python.name)
    assert runner.verifier_identity() == runner.job["verifier_identity"]
    record = verifier_observations(runner)[0]
    assert record["decision"] == "matched"
    assert (
        record["python_paths"]["configured_resolved_sha256"]
        == record["python_paths"]["running_resolved_sha256"]
    )


@pytest.mark.parametrize("changed", [False, True])
def test_canonical_check_hash_distinguishes_mapping_order_from_changed_arguments(tmp_path, changed):
    runner = verifier_runner(tmp_path)
    before = {}
    observed_verifier_identity(runner.runtime, runner.contract, observation=before)
    runner.contract["checks"] = [dict(reversed(list(check.items()))) for check in runner.contract["checks"]]
    if changed:
        runner.contract["checks"][0]["argv"][1] = "--version"
        with pytest.raises(GateFailure, match="Observed independent verifier"):
            runner.verifier_identity()
    else:
        assert runner.verifier_identity() == runner.job["verifier_identity"]
    after = verifier_observations(runner)[0]
    assert (before["checks_sha256"] == after["checks_sha256"]) is (not changed)
    assert after["decision"] == ("identity_mismatch" if changed else "matched")


def test_changed_frozen_pin_records_actual_hash_and_still_rejects(tmp_path):
    runner = verifier_runner(tmp_path)
    source = (REPOSITORY / "src/ephy_worker/formal_runtime.py").resolve()
    runner.contract["runtime_hashes"][str(source)] = "0" * 64
    runner.command = lambda *_: pytest.fail("Wrong pin reached a command")
    with pytest.raises(GateFailure, match="matching frozen pin"):
        runner.run_checks("rejected")
    record = verifier_observations(runner)[0]
    assert record["decision"] == "observation_failed"
    assert record["observed_identity"] is not None
    assert record["phase"] == "pin_validation"
    module = record["files"]["module_0"]
    assert module["sha256"] == file_hash(source)
    assert module["expected_pin_sha256"] == "0" * 64 and not module["pin_matches"]


def test_concurrent_artifact_change_is_recorded_and_rejected(tmp_path, monkeypatch):
    from ephy_worker import formal_runtime as formal

    runner = verifier_runner(tmp_path)
    fake_git = tmp_path / "measured-git.exe"
    fake_git.write_bytes(b"stable")
    runner.contract["runtime_hashes"][str(fake_git.resolve())] = file_hash(fake_git)
    monkeypatch.setattr(formal.shutil, "which", lambda _: str(fake_git))
    runner.job["verifier_identity"] = observed_verifier_identity(runner.runtime, runner.contract)
    original = formal.file_hash

    def changing_hash(path):
        sha = original(path)
        if path == fake_git:
            path.write_bytes(b"changed during measurement")
        return sha

    monkeypatch.setattr(formal, "file_hash", changing_hash)
    runner.command = lambda *_: pytest.fail("Concurrent change reached a command")
    with pytest.raises(GateFailure, match="changed during measurement"):
        runner.run_checks("rejected")
    record = verifier_observations(runner)[0]
    assert record["decision"] == "observation_failed" and record["phase"] == "git"
    assert record["files"]["git"]["stable"] is False
    assert record["files"]["git"]["stat_before_sha256"] != record["files"]["git"]["stat_after_sha256"]


def test_unavailable_verifier_records_partial_observation_without_raw_error(tmp_path, monkeypatch):
    from ephy_worker import formal_runtime as formal

    runner = verifier_runner(tmp_path)
    monkeypatch.setattr(formal.shutil, "which", lambda _: None)
    with pytest.raises(GateFailure, match="executable unavailable"):
        runner.verifier_identity()
    record = verifier_observations(runner)[0]
    assert record["decision"] == "observation_failed" and record["phase"] == "git_resolution"
    assert record["observed_identity"] is None and record["failure_type"] == "GateFailure"
    assert record["files"]["python"]["pin_matches"]


def test_wrong_python_diagnostic_records_resolutions_without_raw_paths(tmp_path):
    runner = verifier_runner(tmp_path)
    fake = tmp_path / "private-path-other-python.exe"
    fake.write_bytes(b"other interpreter")
    runner.runtime["python"] = str(fake)
    with pytest.raises(GateFailure, match="running interpreter"):
        runner.verifier_identity()
    record = verifier_observations(runner)[0]
    assert record["decision"] == "observation_failed"
    assert (
        record["python_paths"]["configured_resolved_sha256"]
        != record["python_paths"]["running_resolved_sha256"]
    )
    assert str(fake) not in json.dumps(record)


@pytest.mark.parametrize("failure", ["io", "limit"])
def test_verifier_diagnostic_write_failure_stops_before_commands(tmp_path, failure):
    runner = verifier_runner(tmp_path)
    if failure == "io":
        (runner.directory / "verifier-identity.jsonl").mkdir()
        message = "could not be retained"
    else:
        runner.contract["max_log_bytes"] = 1
        message = "log limit exceeded"
    runner.command = lambda *_: pytest.fail("Missing diagnostic reached a command")
    with pytest.raises(GateFailure, match=message):
        runner.run_checks("rejected")


@pytest.mark.parametrize("changed", [False, True])
def test_actual_offline_child_launch_records_matching_and_different_environment(
    tmp_path, monkeypatch, changed
):
    monkeypatch.setenv("TZ", "frozen-offline-launch")
    runner = verifier_runner(tmp_path)
    spec = tmp_path / "observation-input.json"
    write_json(
        spec,
        {
            "runtime": runner.runtime,
            "contract": runner.contract,
            "expected": runner.job["verifier_identity"],
            "directory": str(runner.directory),
        },
    )
    env = command_environment(REPOSITORY, tmp_path / "child-temp")
    if changed:
        env["TZ"] = "changed-offline-launch"
    script = """
import json,sys
from pathlib import Path
from ephy_worker.formal_runtime import FormalRunner
from ephy_worker.formal_artifacts import GateFailure
spec=json.loads(Path(sys.argv[1]).read_text())
runner=FormalRunner.__new__(FormalRunner)
runner.runtime,runner.contract=spec['runtime'],spec['contract']
runner.job={'verifier_identity':spec['expected']}
runner.directory=Path(spec['directory'])
try:
    runner.verifier_identity()
except GateFailure:
    sys.exit(78)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(spec)],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == (78 if changed else 0), result.stderr.decode()
    observed = verifier_observations(runner)[0]
    assert observed["decision"] == ("identity_mismatch" if changed else "matched")
    assert (observed["observed_identity"] == runner.job["verifier_identity"]) is (not changed)
    assert observed["observed_identity"]["runtime_sha256"] == runner.job["verifier_identity"][
        "runtime_sha256"
    ]
    assert observed["environment_value_sha256"]["TZ"] == digest(env["TZ"].encode())


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
