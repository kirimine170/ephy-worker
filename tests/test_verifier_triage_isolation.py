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


@pytest.mark.parametrize("change", ["none", "job", "budget", "pins", "runtime", "job_dir"])
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
           "worktreePath": str(tmp_path/"candidate"), "jobDir": str(tmp_path/"job"),
           "runtime": {"thinking": {"planner": "off", "implementer": "off"}, "resource_lock": str(tmp_path/"lock")}}
    identity = {"model_id": "fixed-existing-model"}
    freeze = {"identity": identity, "runtime": {"extra_guard": required[1]},
              "generation_binding": {"job_id": job["id"], "base": BASE_REVISION,
                                     "contract_sha256": sha256(isolation.encode(contract)),
                                     "runtime_sha256": sha256(isolation.encode(job["runtime"])), "job_dir": job["jobDir"],
                                     "worktree": job["worktreePath"]}}
    called = []
    def original_init(self, *_):
        called.append("existing runner init")
        self.job, self.contract, self.identity = job, contract, identity
        self.runtime = job["runtime"]
    monkeypatch.setattr(isolation.StrataRunner, "__init__", original_init)
    if change == "job":
        freeze["generation_binding"]["job_id"] = "unrelated-job"
    elif change == "budget":
        contract["max_requests"] = 9
        freeze["generation_binding"]["contract_sha256"] = sha256(isolation.encode(contract))
    elif change == "pins":
        contract["runtime_hashes"] = {}
        freeze["generation_binding"]["contract_sha256"] = sha256(isolation.encode(contract))
    elif change == "runtime":
        job["runtime"]["resource_lock"] = str(tmp_path/"different-lock")
    elif change == "job_dir":
        job["jobDir"] = str(tmp_path/"different-job")
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


def alternate_corpus():
    batch, gold = development_fixture()
    rows, answers = json.loads(batch), json.loads(gold)
    for row in rows:
        data = json.loads(row["input"])
        for name in ("expected", "observed"):
            data[name]["id"] += "B"
            data[name]["aggregate"] = data[name]["aggregate"].replace("a", "c").replace("b", "d")
            data[name]["components"] = {k: v.replace("a", "c").replace("b", "d")
                                        for k, v in data[name]["components"].items()}
        if "ci" in data:
            for key in data["ci"]:
                data["ci"][key] += "B"
        row["input"] = json.dumps(data, separators=(",", ":"))
    for answer in answers:
        answer["evidence_ids"] = [v+"B" for v in answer["evidence_ids"]]
    result = json_bytes(rows), json_bytes(answers)
    isolation.validate_corpus(*result)
    return result


def pure_freeze(tmp_path, monkeypatch, batch=None, gold=None, token_argv=None):
    import sys

    original_batch, original_gold = development_fixture()
    batch_path, gold_path = tmp_path/"batch.json", tmp_path/"gold.json"
    batch_path.write_bytes(original_batch if batch is None else batch)
    gold_path.write_bytes(original_gold if gold is None else gold)
    monkeypatch.setattr(isolation, "verify_identity", lambda _: None)
    repository = Path(isolation.__file__).resolve().parents[2]
    # uv uses a symlink for its POSIX interpreter; pin the actual binary.
    return isolation.freeze_generation(repository, Path(sys.executable).resolve(), {"model_id": isolation.FAKE_MODEL},
                                       batch_path, gold_path, tmp_path/"freeze", token_argv or [sys.executable])


def test_tokenizer_argv_uses_pinned_executable_identity(tmp_path, monkeypatch):
    import sys

    path = pure_freeze(tmp_path, monkeypatch)
    contract = isolation.frozen(path, sha256(path.read_bytes()))
    executable = str(Path(sys.executable).resolve())
    assert contract["token_argv"] == [executable]
    assert contract["pins"][executable] == sha256(Path(executable).read_bytes())
    assert not Path(executable).is_symlink()


def test_resolved_tokenizer_file_substitution_is_rejected(tmp_path, monkeypatch):
    import sys

    counter = tmp_path/"counter.py"
    counter.write_bytes(b"print(1)\n")
    path = pure_freeze(tmp_path, monkeypatch, token_argv=[sys.executable, str(counter)])
    expected = sha256(path.read_bytes())
    isolation.frozen(path, expected)
    counter.write_bytes(b"print(2)\n")
    with pytest.raises(InvalidEvaluation, match="Frozen artifact changed"):
        isolation.frozen(path, expected)


def test_source_replacement_is_frozen_from_one_read_per_input(tmp_path, monkeypatch):
    original_read = Path.read_bytes
    first = development_fixture()
    second = alternate_corpus()
    targets = {tmp_path/"batch.json": (first[0], second[0]), tmp_path/"gold.json": (first[1], second[1])}
    counts = {p: 0 for p in targets}

    def replaced_read(path):
        if path in targets:
            counts[path] += 1
            if counts[path] == 1:
                path.write_bytes(targets[path][1])
                return targets[path][0]
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", replaced_read)
    path = pure_freeze(tmp_path, monkeypatch)
    contract = isolation.frozen(path, sha256(path.read_bytes()))
    assert counts == {p: 1 for p in targets}
    assert (contract["batch_sha256"], contract["gold_sha256"]) == tuple(sha256(v) for v in first)
    assert tuple((Path(contract["private_root"])/n).read_bytes() for n in ("batch.json", "gold.json")) == first
    assert tuple(original_read(p) for p in targets) == second


@pytest.mark.parametrize("fields", [("batch",), ("gold",), ("batch", "gold")])
def test_declared_input_hashes_cannot_authorize_other_valid_corpus(tmp_path, monkeypatch, fields):
    path = pure_freeze(tmp_path, monkeypatch)
    contract = json.loads(path.read_bytes())
    alternate = dict(zip(("batch", "gold"), alternate_corpus(), strict=True))
    for name in fields:
        contract[name+"_sha256"] = sha256(alternate[name])
    path.write_bytes(json_bytes(contract))
    expected = sha256(path.read_bytes())
    with pytest.raises(InvalidEvaluation, match="hashes differ from private bytes"):
        isolation.frozen(path, expected)
    with pytest.raises(InvalidEvaluation, match="hashes differ from private bytes"):
        isolation.verify_live_binding(tmp_path/"unused-job.json", path, expected,
                                      sha256(alternate["batch"]), sha256(alternate["gold"]), "unused-skill")


@pytest.mark.parametrize("mutation", ["duplicate", "decision", "evidence", "diagnosis", "all_stop", "malformed",
                                      "unattributed_component", "unattributed_stale_ci", "malformed_ci", "match_stale_ci",
                                      "component_stale_ci"])
def test_invalid_complete_corpus_fails_before_identity_or_authoring(tmp_path, monkeypatch, mutation):
    batch, gold = development_fixture()
    rows, answers = json.loads(batch), json.loads(gold)
    if mutation == "duplicate":
        rows[-1] = rows[0]
    elif mutation == "decision":
        answers[0]["decision"] = "MAYBE"
    elif mutation == "evidence":
        answers[0]["evidence_ids"] = ["absent", "also-absent"]
    elif mutation == "diagnosis":
        answers[0]["diagnosis"] = "component:invented"
    elif mutation == "all_stop":
        for answer in answers:
            answer["decision"] = "STOP"
    elif mutation == "unattributed_component":
        answers[0]["diagnosis"] = "UNATTRIBUTED"
    elif mutation in {"unattributed_stale_ci", "match_stale_ci", "malformed_ci", "component_stale_ci"}:
        data = json.loads(rows[8]["input"])
        if mutation == "malformed_ci":
            data["ci"]["observed_revision"] = 42
        elif mutation == "component_stale_ci":
            data["expected"]["components"] = {"PATH": "a"*64}
            data["observed"]["components"] = {"PATH": "b"*64}
            data["observed"]["aggregate"] = "b"*64
            answers[8]["diagnosis"] = "component:PATH"
            answers[8]["evidence_ids"] = ["e", "o"]
        else:
            answers[8]["diagnosis"] = "UNATTRIBUTED" if mutation == "unattributed_stale_ci" else "MATCH"
            answers[8]["decision"] = "STOP" if mutation == "unattributed_stale_ci" else "PASS"
            answers[8]["evidence_ids"] = ["e", "o"]
            if mutation == "unattributed_stale_ci":
                data["observed"]["aggregate"] = "b"*64
        rows[8]["input"] = json.dumps(data, separators=(",", ":"))
    else:
        rows[0]["input"] = "not evidence JSON"
    # A malformed corpus must be rejected before even querying the service.
    monkeypatch.setattr(isolation, "verify_identity", lambda _: pytest.fail("Service queried"))
    import sys
    repository = Path(isolation.__file__).resolve().parents[2]
    (tmp_path/"batch.json").write_bytes(json_bytes(rows))
    (tmp_path/"gold.json").write_bytes(json_bytes(answers))
    with pytest.raises(InvalidEvaluation):
        isolation.freeze_generation(repository, Path(sys.executable), {"model_id": isolation.FAKE_MODEL},
                                    tmp_path/"batch.json", tmp_path/"gold.json", tmp_path/"freeze", [sys.executable])
    assert not (tmp_path/"freeze").exists()


def test_cumulative_budget_includes_pending_writes_and_external_job_logs(tmp_path):
    capture, job = tmp_path/"capture", tmp_path/"job"
    capture.mkdir()
    job.mkdir()
    (capture/"http.json").write_bytes(b"x"*8)
    trace = job/"trace.jsonl"
    trace.write_bytes(b"x"*10)
    contract = {"directory": str(capture), "log_root": str(job), "max_log_bytes": 32}
    isolation.capture_budget(contract, 14, trace)
    with pytest.raises(InvalidEvaluation, match="cumulative log cap"):
        isolation.write_capture(contract, capture/"pending.json", b"x"*15)
    assert not (capture/"pending.json").exists()


def test_overlapping_capture_roots_are_counted_once(tmp_path):
    (tmp_path/"inner").mkdir()
    (tmp_path/"inner/data").write_bytes(b"x"*8)
    isolation.capture_budget({"directory": str(tmp_path), "log_root": str(tmp_path/"inner"), "max_log_bytes": 16}, 8)


@pytest.mark.parametrize("filename", ["diff.index.lock", "http-1.json"])
def test_only_transient_git_index_lock_disappearance_is_permitted(tmp_path, monkeypatch, filename):
    path = tmp_path/filename
    path.write_bytes(b"temporary")
    original = Path.stat
    calls = 0

    def renamed_stat(p, *args, **kwargs):
        nonlocal calls
        if p == path:
            calls += 1
            if calls >= 2:
                raise FileNotFoundError(path)
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", renamed_stat)
    contract = {"directory": str(tmp_path), "max_log_bytes": 32}
    if filename.endswith(".index.lock"):
        isolation.capture_budget(contract, 20)
    else:
        with pytest.raises(FileNotFoundError):
            isolation.capture_budget(contract, 20)
