"""Offline controls for exact final-input and pre-authoring isolation evidence."""
import copy
import json
from pathlib import Path

import pytest

from ephy_worker import verifier_triage_isolation as isolation
from ephy_worker.verifier_triage_consumer import json_bytes
from ephy_worker.verifier_triage_evaluation import InvalidEvaluation, sha256
from ephy_worker.verifier_triage_fixtures import development_fixture


@pytest.fixture
def wire(tmp_path):
    batch, gold = development_fixture()
    private = tmp_path/"private"
    private.mkdir()
    (private/"batch.json").write_bytes(batch)
    (private/"gold.json").write_bytes(gold)
    directory = tmp_path/"planner"
    (directory/"previews").mkdir(parents=True)
    config = {"session_id": "fixed-session", "role": "planner", "prompt": isolation.TASK}
    config_raw = json_bytes(config)
    (directory/"config.json").write_bytes(config_raw)
    start = {"kind": "generation_start", "session_id": config["session_id"], "role": "planner",
             "config_sha256": sha256(config_raw), "pid": 123}
    first = {"model": "fake", "messages": [
        {"role": "developer", "content": "Acknowledgement-Nonce: nonce-1\nRole: planner"},
        {"role": "user", "content": "MANDATORY GOVERNANCE BOOTSTRAP ONLY. test"}],
        "tools": [{"type": "function", "function": {"name": "governance_ack"}}]}
    (directory/"previews/1.json").write_bytes(json_bytes(first))
    previews = [{"kind": "generation_preview", "number": 1, "sha256": sha256(json_bytes(first)),
                 "session_id": config["session_id"], "role": "planner"}]
    first_wire = copy.deepcopy(first)
    first_wire["tool_choice"] = {"type": "function", "function": {"name": "governance_ack"}}
    return {"contract": {"private_root": str(private)}, "directory": directory,
            "config": config, "events": [start, *previews], "raw": json_bytes(first_wire)}


def check(wire, n=1):
    return isolation.wire_matches(wire["contract"], wire["directory"], n, wire["raw"],
                                  wire["config"], wire["events"])


def test_complete_final_payload_has_nonce_binding(wire):
    assert check(wire) == {"nonce": "nonce-1", "tool_results": []}


@pytest.mark.parametrize("change", ["raw", "preview_hash", "session", "missing", "duplicate"])
def test_changed_partial_or_replayed_preview_rejected(wire, change):
    if change == "raw":
        payload = json.loads(wire["raw"])
        payload["messages"][0]["content"] += "\ninjected"
        wire["raw"] = json_bytes(payload)
    elif change == "preview_hash":
        wire["events"][-1]["sha256"] = "0"*64
    elif change == "session":
        wire["events"][0]["session_id"] = "other"
    elif change == "missing":
        wire["events"].pop()
    else:
        wire["events"].append(wire["events"][-1])
    with pytest.raises(InvalidEvaluation):
        check(wire)


@pytest.mark.parametrize("source", ["batch.json", "gold.json"])
def test_held_out_json_escaped_and_embedded_inputs_reject_before_forward(wire, source):
    secret = (Path(wire["contract"]["private_root"])/source).read_text(encoding="utf-8")
    payload = json.loads(wire["raw"])
    payload["messages"][0]["content"] = json.dumps({"nested": secret})
    wire["raw"] = json_bytes(payload)
    with pytest.raises(InvalidEvaluation, match="Held-out/gold leakage"):
        check(wire)


def second_request(wire):
    config, directory = wire["config"], wire["directory"]
    content = "approved result"
    call = {"kind": "generation_tool_call", "id": "read-1", "name": "read",
            "input": {"path": "authoring/task.md"}, "session_id": config["session_id"]}
    result = {"kind": "generation_tool_result", **{k: call[k] for k in ("id", "name", "input", "session_id")},
              "text": content, "bytes": len(content), "sha256": sha256(content.encode())}
    config["allowed_reads"] = ["authoring/task.md"]
    config["input_snapshot"] = {"authoring/task.md": {"bytes": len(content), "sha256": sha256(content.encode())}}
    config_raw = json_bytes(config)
    (directory/"config.json").write_bytes(config_raw)
    wire["events"][0]["config_sha256"] = sha256(config_raw)
    body = {"model": "fake", "tools": [], "messages": [
        {"role": "developer", "content": "Acknowledgement-Nonce: nonce-1\nRole: planner"},
        {"role": "user", "content": '<file name="'+str(directory/"prompt.txt")+'">\n'+config["prompt"]+'\n</file>\n'},
        {"role": "assistant", "tool_calls": [{"id": "read-1", "function": {
            "name": "read", "arguments": json.dumps(call["input"])}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": content}]}
    raw = json_bytes(body)
    (directory/"previews/2.json").write_bytes(raw)
    wire["events"] += [call, result, {"kind": "generation_preview", "number": 2,
                                    "sha256": sha256(raw), "session_id": config["session_id"], "role": "planner"}]
    wire["raw"] = raw


def test_every_actual_tool_result_is_bound(wire):
    second_request(wire)
    assert check(wire, 2)["tool_results"][0]["id"] == "read-1"


@pytest.mark.parametrize("change", ["missing", "substituted", "hash", "unclosed_user", "duplicate_result"])
def test_missing_substituted_or_duplicate_tool_results_are_not_isolation(wire, change):
    second_request(wire)
    result = next(e for e in wire["events"] if e["kind"] == "generation_tool_result")
    if change == "missing":
        wire["events"].remove(result)
    elif change == "substituted":
        result["text"] += "swapped"
        result["bytes"] = len(result["text"])
        result["sha256"] = sha256(result["text"].encode())
    elif change == "hash":
        result["sha256"] = "0"*64
    elif change == "duplicate_result":
        wire["events"].insert(-1, result)
    else:
        payload = json.loads(wire["raw"])
        payload["messages"][1]["content"] += "extra task"
        wire["raw"] = json_bytes(payload)
        (wire["directory"]/"previews/2.json").write_bytes(wire["raw"])
        wire["events"][-1]["sha256"] = sha256(wire["raw"])
    with pytest.raises(InvalidEvaluation):
        check(wire, 2)


def test_saved_freeze_requires_expected_raw_hash(tmp_path):
    p = tmp_path/"freeze.json"
    p.write_bytes(b"{}")
    with pytest.raises(InvalidEvaluation, match="Pre-authoring freeze changed"):
        isolation.frozen(p, "0"*64)


def test_caps_keep_original_role_and_job_limits():
    assert isolation.CAPS == {"max_requests": 8, "output_token_budget": 2200,
                              "max_response_tokens": 1024, "stage_seconds": 300, "job_seconds": 900}
    assert isolation.PLAN["repair"] == 0 and len(isolation.PLAN["scope"]) == 1


def test_native_label_change_cannot_authorize_live(monkeypatch):
    monkeypatch.setattr(isolation, "frozen", lambda *_: {"identity": {"model_id": isolation.FAKE_MODEL}})
    monkeypatch.setattr(isolation, "verify_capture", lambda *_: {"synthetic_only": False})
    with pytest.raises(InvalidEvaluation, match="Native capture cannot authorize"):
        isolation.verify_live_binding(Path("job"), Path("freeze"), "hash", "batch", "gold", "skill")


@pytest.mark.parametrize("change", ["none", "job", "budget", "pins"])
def test_external_adapter_binds_existing_job_and_original_caps(monkeypatch, tmp_path, change):
    from ephy_worker.verifier_triage_evaluation import BASE_REVISION, SKILL_PATH
    module = Path(isolation.__file__).resolve()
    repo = module.parents[2]
    required = [str(module), str(repo/"tools/pi-local/triage-generation-guard.ts"),
                str(module.with_name("verifier_triage_consumer.py"))]
    contract = {"allowed_files": [SKILL_PATH], "max_repairs": 0, "max_requests": 8,
                "output_token_budget": 2200, "max_response_tokens": 1024, "stage_seconds": 300,
                "timeout_seconds": 900, "runtime_hashes": {p: sha256(Path(p).read_bytes()) for p in required}}
    job = {"id": "bound-job", "baseRevision": BASE_REVISION, "contract": contract,
           "worktreePath": str(tmp_path/"candidate")}
    identity = {"model_id": "fixed-existing-model"}
    freeze = {"identity": identity, "runtime": {"extra_guard": required[1]},
              "generation_binding": {"job_id": job["id"], "base": BASE_REVISION,
                                     "contract_sha256": sha256(json_bytes(contract)),
                                     "worktree": job["worktreePath"]}}
    called = []
    def original_init(self, *_):
        called.append("existing runner init")
        self.job, self.contract, self.identity = job, contract, identity
    monkeypatch.setattr(isolation.StrataRunner, "__init__", original_init)
    if change == "job":
        freeze["generation_binding"]["job_id"] = "unrelated-job"
    elif change == "budget":
        contract["max_requests"] = 9
        freeze["generation_binding"]["contract_sha256"] = sha256(json_bytes(contract))
    elif change == "pins":
        contract["runtime_hashes"] = {}
        freeze["generation_binding"]["contract_sha256"] = sha256(json_bytes(contract))
    monkeypatch.setattr(isolation, "frozen", lambda *_: freeze)
    if change == "none":
        runner = isolation.IsolatedStrataRunner(tmp_path/"job", tmp_path/"freeze", "hash")
        assert runner.isolation_stages == []
    else:
        with pytest.raises(InvalidEvaluation):
            isolation.IsolatedStrataRunner(tmp_path/"job", tmp_path/"freeze", "hash")
    assert called == ["existing runner init"]


def test_replayed_payload_is_refused_before_process_or_network(tmp_path, monkeypatch):
    c = isolation.GenerationCapture.__new__(isolation.GenerationCapture)
    c.directory, c.completed = tmp_path, ["sealed"]
    monkeypatch.setattr(c, "check_prefix", lambda: None)
    raw = b'{"messages":[]}'
    (tmp_path/"http-1.complete.json").write_bytes(json_bytes({"payload_sha256": sha256(raw)}))
    monkeypatch.setattr(isolation.psutil, "Process", lambda *_: pytest.fail("Process reached"))
    with pytest.raises(InvalidEvaluation, match="Replay/duplicate payload"):
        c.admit(raw, 2)
