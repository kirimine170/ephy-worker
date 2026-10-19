"""Synthetic protocol controls; no Pi, network, model, or Karte request."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from ephy_worker.coding_executor import CodingExecutionError
from ephy_worker.desktop.config import DesktopConfig
from ephy_worker.desktop.governance import (
    CONTEXT_PATHS,
    END_MARKER,
    LIMITS,
    PINNED_HASHES,
    POLICY_ID,
    POLICY_PATH,
    GovernanceSession,
    validate_runtime,
)


@pytest.fixture
def governed(tmp_path):
    session = GovernanceSession(tmp_path, DesktopConfig(tmp_path / "repository"))
    session.prepare(tmp_path)
    return session


def envelope(session):
    return {"type": "ephy_desktop_governance_envelope", "version": 1,
            "policy_sha256": PINNED_HASHES[POLICY_PATH], "role": "planner",
            "nonce": "01234567-89ab-4def-89ab-0123456789ab", "context_manifest": session.context}


def assistant(session, content, stop="stop"):
    return {"type": "message_end", "message": {"role": "assistant", "provider": "strata-local",
            "model": session.config.model_id, "content": content, "stopReason": stop}}


def ack_message(session):
    return assistant(session, [{"type": "toolCall", "name": "governance_ack", "id": "ack-1",
                     "arguments": {"policyId": POLICY_ID, "policySha256": PINNED_HASHES[POLICY_PATH],
                                   "endMarker": END_MARKER, "nonce": session.nonce, "role": "planner"}}],
                     "toolUse")


def ack_result(session):
    return {"type": "tool_execution_end", "toolName": "governance_ack", "toolCallId": "ack-1",
            "isError": False, "result": {"content": session.expected_bundle(), "details": {
                "policyId": POLICY_ID, "policySha256": PINNED_HASHES[POLICY_PATH],
                "endMarker": END_MARKER, "nonce": session.nonce, "role": "planner", "acknowledged": True,
                "phase": "ready", "activeTools": ["read"], "requiredContext": session.context,
                "governanceContext": {"complete": True, "documents": session.context,
                                      "activeTools": ["read"]}}}}


def acknowledge(session):
    session.observe(envelope(session))
    session.observe(ack_message(session))
    session.observe({"type": "tool_execution_start", "toolName": "governance_ack", "toolCallId": "ack-1"})
    session.observe(ack_result(session))


def finish(session):
    session.observe(assistant(session, [{"type": "text", "text": "Synthetic repository answer."}]))
    session.observe({"type": "agent_end", "messages": []})
    session.observe({"type": "agent_settled"})
    session.trace_path.write_text('{"kind":"stage_end","failed":false}\n', encoding="utf-8")
    session.complete()


def test_runtime_matches_original_canonical_bytes_and_all_ten_paths():
    manifest = validate_runtime()
    repository = Path(__file__).resolve().parents[1]
    runtime = repository / "src/ephy_worker/desktop/governance_runtime"
    assert len(manifest["files"]) == 10
    for name in PINNED_HASHES:
        assert (runtime / name).read_bytes() == (repository / name).read_bytes()


@pytest.mark.parametrize("mutation", ["bytes", "missing", "extra", "manifest"])
def test_runtime_rejects_changed_missing_extra_or_forged_bytes(tmp_path, mutation):
    source = Path(__file__).resolve().parents[1] / "src/ephy_worker/desktop/governance_runtime"
    runtime = tmp_path / "runtime"
    shutil.copytree(source, runtime)
    if mutation == "bytes":
        (runtime / POLICY_PATH).write_text("altered policy", encoding="utf-8")
    elif mutation == "missing":
        (runtime / POLICY_PATH).unlink()
    elif mutation == "extra":
        (runtime / "extra.ts").write_text("// untrusted", encoding="utf-8")
    else:
        manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        (runtime / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="governance"):
        validate_runtime(runtime)


def test_runtime_identity_and_budget_are_controller_owned(governed):
    env = governed.environment
    assert env["DUAL_GOVERNANCE_ROLE"] == "planner"
    assert env["DUAL_JOB_RUNNER"] == env["DUAL_RUNNER_SMOKE_TEST"] == env["DUAL_AUDIT_BUNDLE_ROOT"] == ""
    stage = json.loads(governed.stage_path.read_text(encoding="utf-8"))
    assert {name: stage[name] for name in LIMITS} == LIMITS
    assert [item["path"] for item in governed.context] == list(CONTEXT_PATHS)
    assert len(governed.expected_bundle()[0]["text"].encode()) < 50 * 1024


def test_configured_wall_time_is_per_session_and_stage_remains_hash_bound(tmp_path):
    original_limits = copy.deepcopy(LIMITS)
    assert original_limits == {
        "role": "planner", "allowed_files": [], "provider_id": "strata-local", "max_requests": 8,
        "output_token_budget": 8192, "max_response_tokens": 2048, "wall_seconds": 240,
        "agent_retries": 0, "provider_retries": 0,
    }
    for index, timeout in enumerate((240, 1, 600, 1200, 240)):
        allocation = tmp_path / str(index)
        allocation.mkdir()
        config = DesktopConfig(tmp_path / "repository", timeout_seconds=timeout)
        session = GovernanceSession(allocation, config)
        session.prepare(allocation)
        stage = json.loads(session.stage_path.read_text(encoding="utf-8"))
        assert {key: stage[key] for key in LIMITS} == {**original_limits, "wall_seconds": timeout}
        assert LIMITS == original_limits
        session.check_hashes()
        stage["wall_seconds"] = timeout + 1
        session.stage_path.write_text(json.dumps(stage), encoding="utf-8")
        with pytest.raises(CodingExecutionError, match="pi_rpc_failure") as raised:
            session.check_hashes()
        assert raised.value.detail == "governance: controller resource changed during execution"


@pytest.mark.parametrize("endpoint", ["http://localhost:8081/v1", "https://127.0.0.1:8081/v1",
                                      "http://127.0.0.1:8081/other", "http://[::1]:8081/v1"])
def test_live_provider_contract_is_not_generic_loopback(tmp_path, endpoint):
    with pytest.raises(ValueError, match="strata-local"):
        GovernanceSession(tmp_path, DesktopConfig(tmp_path / "repository", endpoint=endpoint))


def test_ack_is_separate_from_preparation_and_final_task_completion(governed, tmp_path):
    assert not governed.ack_verified
    acknowledge(governed)
    assert governed.ack_verified and not governed.settled and not governed.stage_verified
    governed.observe({"type": "tool_execution_start", "toolName": "read", "toolCallId": "read-1"})
    governed.observe({"type": "tool_execution_end", "toolName": "read", "isError": False})
    finish(governed)
    governed.save(tmp_path)
    saved = json.loads((tmp_path / "governance-attestation.json").read_text(encoding="utf-8"))
    assert saved["stage_verified"] and saved["ack_verified"] and saved["post_ack_answer"]
    assert saved["formal_audit"] is False and saved["read_calls"] == 1


@pytest.mark.parametrize("mutation", ["no-envelope", "nonce", "role", "extra-field", "sibling-tool", "prose"])
def test_ack_requires_trusted_envelope_and_exact_one_shot_input(governed, mutation):
    if mutation != "no-envelope":
        governed.observe(envelope(governed))
    event = ack_message(governed)
    call = event["message"]["content"][0]
    if mutation in {"nonce", "role"}:
        call["arguments"][mutation] = "wrong"
    elif mutation == "extra-field":
        call["arguments"]["extra"] = True
    elif mutation == "sibling-tool":
        event["message"]["content"].append({"type": "toolCall", "name": "read", "id": "bad"})
    elif mutation == "prose":
        event["message"]["content"] = [{"type": "text", "text": "I acknowledge the policy"}]
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe(event)
    assert not governed.ack_verified


@pytest.mark.parametrize("mutation", ["pending", "content", "error", "wrong-id", "nonce", "context", "tools"])
def test_only_final_unmutated_gate_receipt_verifies_ack(governed, mutation):
    governed.observe(envelope(governed))
    governed.observe(ack_message(governed))
    governed.observe({"type": "tool_execution_start", "toolName": "governance_ack", "toolCallId": "ack-1"})
    event = copy.deepcopy(ack_result(governed))
    details = event["result"]["details"]
    if mutation == "pending":
        details["phase"] = "context_delivery_pending"
    elif mutation == "content":
        event["result"]["content"][0]["text"] += " altered"
    elif mutation == "error":
        event["isError"] = True
    elif mutation == "wrong-id":
        event["toolCallId"] = "other"
    elif mutation == "nonce":
        details["nonce"] = "other"
    elif mutation == "context":
        details["governanceContext"]["documents"] = []
    else:
        details["activeTools"] = ["read", "bash"]
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe(event)
    assert not governed.ack_verified


@pytest.mark.parametrize("ack", [False, True])
def test_settled_without_task_answer_is_not_success(governed, ack):
    if ack:
        acknowledge(governed)
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe({"type": "agent_settled"})


@pytest.mark.parametrize("tool", ["read", "write", "bash", "governance_ack", "karte_worklog__karte_work_start"])
def test_tools_cannot_run_before_ack(governed, tool):
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe({"type": "tool_execution_start", "toolName": tool, "toolCallId": "wrong"})


@pytest.mark.parametrize("stop", ["error", "aborted", "length"])
def test_provider_failure_latches_even_if_later_empty_agent_end_or_retry(governed, stop):
    acknowledge(governed)
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe(assistant(governed, [], stop))
    for event in ({"type": "agent_end", "messages": [], "willRetry": True}, {"type": "agent_settled"}):
        with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
            governed.observe(event)


def test_gate_marker_is_final_message_failure_not_raw_delta_match(governed):
    acknowledge(governed)
    governed.observe({"type": "message_update", "assistantMessageEvent": {
        "type": "text_delta", "delta": "GOVERNANCE_GATE_FAILURE: example"}})
    assert governed.failed is None
    governed.observe(assistant(governed, [{"type": "text", "text": "GOVERNANCE_GATE_FAILURE: example"}]))
    assert governed.failed is None
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe(assistant(governed, [{"type": "text", "text": (
            "GOVERNANCE_GATE_FAILURE: invalid. This session is permanently latched; start a fresh session."
        )}]))


@pytest.mark.parametrize("bad_trace", [
    '{"kind":"violation"}\n{"kind":"stage_end","failed":false}\n',
    '{"kind":"hard_stop","exit_code":78}\n',
    '{"kind":"stage_end","failed":true}\n',
    '{"kind":"governance_result","isError":false}\n',
])
def test_pre_gate_trace_or_stage_failure_cannot_replace_final_success(governed, bad_trace):
    acknowledge(governed)
    finish(governed)
    governed.stage_verified = False
    governed.trace_path.write_text(bad_trace, encoding="utf-8")
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.complete()
    assert not governed.stage_verified


def test_controller_identity_mutation_rejected(governed):
    governed.identity_path.write_text('{}', encoding="utf-8")
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.check_hashes()


def test_receipt_without_ack_execution_start_is_rejected(governed):
    governed.observe(envelope(governed))
    governed.observe(ack_message(governed))
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe(ack_result(governed))


def test_new_unfinished_turn_cannot_reuse_previous_final_answer(governed):
    acknowledge(governed)
    governed.observe(assistant(governed, [{"type": "text", "text": "Earlier answer"}]))
    governed.observe({"type": "turn_start"})
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        governed.observe({"type": "agent_settled"})
