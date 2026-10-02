"""Negative controls for the controller, independent of live inference."""

import copy
import subprocess
import sys
from pathlib import Path

import pytest

from ephy_worker.formal_artifacts import (
    ARTIFACTS,
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
from ephy_worker.formal_runtime import exclusive_lock, snapshot, stage_evidence, submit, validate_contract

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
    for domain, prefix in (("integrity", "I"), ("candidate", "C"), ("workflow", "W")):
        result[domain] = {
            "status": "PASS",
            "checks": [
                {
                    "check_id": prefix + "01_TEST",
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
