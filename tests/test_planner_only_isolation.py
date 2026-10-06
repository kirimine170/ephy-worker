"""Offline controls for planner-only isolation; no real service or Pi is used."""
import copy
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker import verifier_triage_consumer as consumer
from ephy_worker import verifier_triage_isolation as isolation
from ephy_worker.formal_runtime import GateFailure, observed_process_identity, snapshot_hash
from ephy_worker.verifier_triage_evaluation import BASE_REVISION, SKILL_PATH, InvalidEvaluation, sha256

PLAN = "Read measured evidence, preserve STOP, and return one scoped plan."


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(consumer.json_bytes(value))


@pytest.fixture(autouse=True)
def forbid_live_HTTP(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline control attempted HTTP")
    monkeypatch.setattr("httpx.Client.send", blocked)
    monkeypatch.setattr("httpx.AsyncClient.send", blocked)


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    subprocess.run(["git", "init", str(candidate)], capture_output=True, check=True)
    (candidate / "README.md").write_text("Unchanged offline input\n", encoding="utf-8")
    job_dir, capture = tmp_path / "job", tmp_path / "capture"
    job_dir.mkdir()
    capture.mkdir()
    value = {
        "allowed_files": [SKILL_PATH], "max_repairs": 0, "max_requests": 8,
        "output_token_budget": 2200, "max_response_tokens": 275, "stage_seconds": 180,
        "timeout_seconds": 300, "planner_only": True,
    }
    job = {"id": "offline-bound-planner", "baseRevision": BASE_REVISION, "contract": value,
           "runtime": {"resource_lock": str(tmp_path / "lock")},
           "jobDir": str(job_dir), "worktreePath": str(candidate),
           "status": "planner_stopped", "outcome": "planned_only"}
    job.update(schemaVersion=2, humanAuthorization="explicit-execute-proposal-only",
               controls={"policy_sha256": "a" * 64}, model_identities={"planner": {"model_id": isolation.FAKE_MODEL}},
               repoRoot=str(candidate), verifier_identity={"executor_id": "synthetic"},
               environment_sha256="b" * 64)
    controller = observed_process_identity(os.getpid())
    job["runnerPid"] = controller["pid"]
    save(job_dir / "job.json", job)
    (job_dir / "lead-plan.txt").write_text(PLAN, encoding="utf-8")
    stop = {"job_id": job["id"], "contract_sha256": sha256(isolation.encode(value)),
            "plan_sha256": sha256(PLAN.encode()), "snapshot_sha256": snapshot_hash(candidate),
            "implementation_started": False, "formal_audit_executed": False, "automatic_adoption": False}
    save(job_dir / "planner-stop.json", stop)
    freeze_path = capture / "freeze.json"
    save(freeze_path, {"synthetic_fixture": True})
    expected = sha256(freeze_path.read_bytes())
    frozen = {"directory": str(capture), "generation_binding": {
        "job_id": job["id"], "base": BASE_REVISION, "contract_sha256": stop["contract_sha256"],
        "runtime_sha256": sha256(isolation.encode(job["runtime"])), "job_dir": str(job_dir),
        "worktree": str(candidate)},
        "planner_only": True, "identity": {"model_id": isolation.FAKE_MODEL},
        "runtime": {"pi": str(Path(sys.executable).resolve())}, "created_at": controller["created_at"],
        "frozen": {"policy_sha256": "a" * 64}, "max_log_bytes": 8388608}
    frozen["planner_controller_process"] = controller
    frozen["controller"] = {"executable": str(Path(sys.executable).resolve()), "python_version": sys.version}
    frozen["pins"] = {controller["executable"]: controller["executable_sha256"],
                      str(Path(sys.executable).resolve()): sha256(Path(sys.executable).read_bytes())}
    frozen["generation_submission_sha256"] = sha256(isolation.encode({key: value for key, value in job.items()
        if key not in {"status", "message", "updatedAt", "runnerPid", "outcome", "environment_sha256"}}))
    frozen["generation_environment_sha256"] = job["environment_sha256"]
    monkeypatch.setattr(isolation, "frozen", lambda *_: frozen)
    # These existing boundaries have separate actual-wire and canonical-delivery controls.
    monkeypatch.setattr(isolation, "wire_matches", lambda *args: {})
    monkeypatch.setattr(consumer, "canonical_delivery", lambda *args: None)

    def session(role="planner", tokens=100):
        directory = capture / role
        directory.mkdir(exist_ok=True)
        before = consumer.snapshot(candidate)
        config = {"role": role, "session_id": role + "-session"}
        events = [
            {"kind": "provider_request", "model": isolation.FAKE_MODEL, "requests": 1},
            {"kind": "governance_result", "details": {"role": role, "acknowledged": True,
                                                     "policySha256": "a" * 64}},
            {"kind": "assistant", "model": isolation.FAKE_MODEL, "stopReason": "stop",
             "responseTokenCap": 275, "responseTokens": tokens, "outputTokens": tokens},
            {"kind": "stage_end", "failed": False, "requests": 1, "outputTokens": tokens},
        ]
        save(directory / "config.json", config)
        (directory / "trace.jsonl").write_bytes(b"\n".join(consumer.json_bytes(e) for e in events) + b"\n")
        (directory / "output.txt").write_bytes(PLAN.encode())
        save(directory / "session.jsonl", {"type": "message", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": PLAN}]}})
        (directory / "session.jsonl").write_bytes((directory / "session.jsonl").read_bytes() + b"\n")
        process = {"pid": os.getpid() + 500000000 + (role == "implementer"),
                   "created_at": max(time.time() - 0.02, frozen["created_at"] + 0.05),
                   "executable": frozen["runtime"]["pi"], "parent_pid": controller["pid"],
                   "controller_process": copy.deepcopy(controller)}
        save(directory / "process.json", process)
        raw = consumer.json_bytes({"max_tokens": 275})
        (directory / "http-1.json").write_bytes(raw)
        (directory / "http-1.trace-prefix").write_bytes(b"")
        save(directory / "http-1.admission.json", {"number": 1, "freeze_sha256": expected, "process": process})
        save(directory / "previews/1.json", {"fixture": True})
        usage = {"completion_tokens": tokens, "prompt_tokens": 4, "total_tokens": 4 + tokens}
        (directory / "http-1.response").write_bytes(
            b"data: " + consumer.json_bytes({"choices": [{"delta": {"content": PLAN}, "finish_reason": "stop"}]}) +
            b"\n\ndata: " + consumer.json_bytes({"usage": usage}) + b"\n\ndata: [DONE]\n\n")
        save(directory / "http-1.receipt.json", {
            "accepted": True, "forwarded": True, "payload_sha256": sha256(raw),
            "generation_admission_sha256": sha256((directory / "http-1.admission.json").read_bytes())})
        names = ["http-1.json", "http-1.response", "http-1.trace-prefix",
                 "http-1.admission.json", "previews/1.json"]
        save(directory / "http-1.complete.json", {"number": 1, "previous_sha256": expected,
            "files": {name: sha256((directory / name).read_bytes()) for name in names}})
        receipt = {"schema": "ephy.triage-generation-session.v1", "role": role,
            "session_id": config["session_id"], "freeze_sha256": expected, "exit_code": 0,
            "elapsed_seconds": 1, "process": process, "input_before": before, "input_after": before,
            "captures": [sha256((directory / "http-1.complete.json").read_bytes())],
            "files": {p.relative_to(directory).as_posix(): sha256(p.read_bytes())
                      for p in directory.rglob("*") if p.is_file() and p.name != "receipt.json"}}
        save(directory / "receipt.json", receipt)
        return directory

    directory = session()
    case = SimpleNamespace(job=job, job_dir=job_dir, capture=capture, candidate=candidate,
                           freeze=frozen, path=freeze_path, expected=expected, session=session,
                           directory=directory)
    write_workflow(case)
    return case


def planner_result(case):
    # The result is derived by the production completion verifier.
    return isolation.planner_capture_result(case.freeze, case.expected)


def publish(case):
    result = planner_result(case)
    save(case.capture / "capture.json", result)
    return result


@pytest.mark.parametrize("text", [PLAN, "NO_HYPOTHESIS", PLAN + "\nUnicode: 計画\n"])
def test_complete_planner_only_capture_accepts_without_skill_or_second_session(sealed, text):
    if text != PLAN:
        (sealed.job_dir / "lead-plan.txt").write_text(text, encoding="utf-8")
        (sealed.directory / "output.txt").write_bytes(text.encode())
        save(sealed.directory / "session.jsonl", {"type": "message", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": text}]}})
        stop = isolation.load(sealed.job_dir / "planner-stop.json")
        stop["plan_sha256"] = sha256(text.encode())
        save(sealed.job_dir / "planner-stop.json", stop)
        sealed.job["outcome"] = "no_hypothesis" if text == "NO_HYPOTHESIS" else "planned_only"
        save(sealed.job_dir / "job.json", sealed.job)
        raw = (sealed.directory / "http-1.response").read_bytes()
        (sealed.directory / "http-1.response").write_bytes(raw.replace(consumer.json_bytes(PLAN), consumer.json_bytes(text)))
        reseal(sealed, include_closure=True)
    result = publish(sealed)
    assert isolation.verify_capture(sealed.path, sealed.expected) == result
    assert result["sessions"] == ["planner"] and result["implementation_requests"] == 0
    assert result["requests"] == 1 and result["output_tokens"] == 100
    assert result["synthetic_only"] is True and result["real_model_generations"] == 0
    assert not (sealed.capture / "implementer").exists()
    assert not (sealed.candidate / SKILL_PATH).exists()


def reseal(case, include_closure=False):
    directory = case.directory
    receipt = isolation.load(directory / "receipt.json")
    if include_closure:
        closure = isolation.load(directory / "http-1.complete.json")
        closure["files"] = {name: sha256((directory / name).read_bytes()) for name in closure["files"]}
        save(directory / "http-1.complete.json", closure)
        receipt["captures"] = [sha256((directory / "http-1.complete.json").read_bytes())]
    receipt["files"] = {p.relative_to(directory).as_posix(): sha256(p.read_bytes())
                       for p in directory.rglob("*") if p.is_file() and p.name != "receipt.json"}
    save(directory / "receipt.json", receipt)
    write_workflow(case)


@pytest.mark.parametrize("change", [
    "missing_receipt", "missing_response", "missing_stop", "missing_plan", "missing_session",
    "receipt_hash", "wrong_job", "contract_flag", "contract_hash", "runtime_hash", "freeze_flag",
    "empty_plan", "substituted_plan", "substituted_output", "substituted_final", "candidate",
    "implementer_directory", "audit_bundle", "audit_result", "implementation_flag", "audit_flag",
    "adoption_flag", "failed_job", "wrong_outcome", "unknown_usage", "unfinished_response",
    "usage_trace_disagreement", "negative_usage", "boolean_usage", "response_over_cap",
    "stage_over_time",
])
def test_incomplete_forged_or_unbounded_planner_capture_rejected(sealed, change):
    targets = {"missing_receipt": "receipt.json", "missing_response": "http-1.response",
               "missing_session": "session.jsonl"}
    if change in targets:
        (sealed.directory / targets[change]).unlink()
    elif change in {"missing_stop", "missing_plan"}:
        (sealed.job_dir / ("planner-stop.json" if change == "missing_stop" else "lead-plan.txt")).unlink()
    elif change == "receipt_hash":
        (sealed.directory / "output.txt").write_bytes(b"forged")
    elif change in {"wrong_job", "contract_flag", "contract_hash", "runtime_hash", "failed_job", "wrong_outcome"}:
        job = copy.deepcopy(sealed.job)
        if change == "wrong_job":
            job["id"] = "different-job"
        elif change == "contract_flag":
            job["contract"]["planner_only"] = False
        elif change == "contract_hash":
            job["contract"]["max_requests"] = 7
        elif change == "runtime_hash":
            job["runtime"]["resource_lock"] += ".other"
        elif change == "failed_job":
            job["status"] = "failed"
        else:
            job["outcome"] = "accepted_proposal"
        save(sealed.job_dir / "job.json", job)
    elif change == "freeze_flag":
        sealed.freeze["planner_only"] = False
    elif change in {"empty_plan", "substituted_plan"}:
        (sealed.job_dir / "lead-plan.txt").write_text("" if change == "empty_plan" else "forged", encoding="utf-8")
    elif change == "substituted_output":
        (sealed.directory / "output.txt").write_bytes(b"forged")
        reseal(sealed)
    elif change == "substituted_final":
        save(sealed.directory / "session.jsonl", {"type": "message", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "forged"}]}})
        reseal(sealed)
    elif change == "candidate":
        (sealed.candidate / "README.md").write_text("forged\n", encoding="utf-8")
    elif change == "implementer_directory":
        (sealed.capture / "implementer").mkdir()
    elif change in {"audit_bundle", "audit_result"}:
        target = sealed.job_dir / ("audit-bundle" if change == "audit_bundle" else "audit-result.json")
        target.mkdir() if change == "audit_bundle" else target.write_bytes(b"{}")
    elif change in {"implementation_flag", "audit_flag", "adoption_flag"}:
        stop = isolation.load(sealed.job_dir / "planner-stop.json")
        key = {"implementation_flag": "implementation_started", "audit_flag": "formal_audit_executed",
               "adoption_flag": "automatic_adoption"}[change]
        stop[key] = True
        save(sealed.job_dir / "planner-stop.json", stop)
    elif change == "stage_over_time":
        receipt = isolation.load(sealed.directory / "receipt.json")
        receipt["elapsed_seconds"] = 181
        save(sealed.directory / "receipt.json", receipt)
    else:
        tokens = {"negative_usage": -1, "boolean_usage": True, "response_over_cap": 276}.get(change, 100)
        raw = b"data: {}\n\ndata: [DONE]\n\n" if change == "unknown_usage" else (
            b"data: " + consumer.json_bytes({"usage": {
                "completion_tokens": tokens, "prompt_tokens": 4, "total_tokens": 4 + tokens}}) +
            (b"\n\n" if change == "unfinished_response" else b"\n\ndata: [DONE]\n\n"))
        (sealed.directory / "http-1.response").write_bytes(raw)
        if change == "usage_trace_disagreement":
            (sealed.directory / "http-1.response").write_bytes(raw.replace(b"100", b"101"))
        reseal(sealed, include_closure=True)
    with pytest.raises((InvalidEvaluation, GateFailure, FileNotFoundError, KeyError, ValueError)):
        publish(sealed)


@pytest.mark.parametrize("flag", [0, 1, None, "true", "false", {}, []])
def test_nonboolean_mode_cannot_change_capture_profile(sealed, flag):
    sealed.freeze["planner_only"] = flag
    with pytest.raises((InvalidEvaluation, GateFailure, ValueError)):
        publish(sealed)


@pytest.mark.parametrize("change", ["sessions", "implementation", "requests", "output", "plan", "stop", "synthetic"])
def test_final_report_cannot_self_attest_or_hide_implementation(sealed, change):
    result = publish(sealed)
    if change == "sessions":
        result["sessions"].append("implementer")
    elif change == "implementation":
        result["implementation_requests"] = 1
    elif change == "requests":
        result["requests"] = 0
    elif change == "output":
        result["output_tokens"] = None
    elif change in {"plan", "stop"}:
        result[change + "_sha256"] = "0" * 64
    else:
        result["synthetic_only"] = False
    save(sealed.capture / "capture.json", result)
    with pytest.raises((InvalidEvaluation, GateFailure, ValueError)):
        isolation.verify_capture(sealed.path, sealed.expected)


@pytest.mark.parametrize("mode", ["absent", False])
def test_normal_capture_still_requires_both_distinct_sessions_and_skill(sealed, mode):
    sealed.freeze.pop("planner_only") if mode == "absent" else sealed.freeze.update(planner_only=False)
    sealed.job["contract"].pop("planner_only") if mode == "absent" else sealed.job["contract"].update(planner_only=False)
    sealed.freeze["generation_binding"]["contract_sha256"] = sha256(isolation.encode(sealed.job["contract"]))
    save(sealed.job_dir / "job.json", sealed.job)
    sealed.session("implementer")
    skill = sealed.candidate / SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"Scoped synthetic skill\n")
    result = {"schema": "ephy.triage-generation-capture.v1", "freeze_sha256": sealed.expected,
              "sessions": ["planner", "implementer"], "synthetic_only": True,
              "real_model_generations": 0, "candidate_sha256": sha256(skill.read_bytes())}
    save(sealed.capture / "capture.json", result)
    assert isolation.verify_capture(sealed.path, sealed.expected) == result
    result["sessions"] = ["planner"]
    save(sealed.capture / "capture.json", result)
    with pytest.raises(InvalidEvaluation, match="Incomplete generation"):
        isolation.verify_capture(sealed.path, sealed.expected)


def test_planner_capture_cannot_authorize_consumer(sealed):
    publish(sealed)
    with pytest.raises(InvalidEvaluation, match="Planner-only"):
        isolation._verify_job_binding(sealed.job_dir / "job.json", sealed.path, sealed.expected,
                                      "batch", "gold", "skill")


@pytest.mark.parametrize("failure", [False, True])
def test_adapter_completion_preserves_failure_and_releases_actual_kernel_lock(sealed, monkeypatch, failure):
    runner = isolation.IsolatedStrataRunner.__new__(isolation.IsolatedStrataRunner)
    runner.runtime = sealed.job["runtime"]
    runner.job, runner.contract = sealed.job, sealed.job["contract"]
    runner.isolation, runner.isolation_expected, runner.isolation_path = sealed.freeze, sealed.expected, sealed.path
    runner.isolation_stages = ["planner"]
    runner.identity = sealed.freeze["identity"]
    runner._planner_only = True
    runner.candidate, runner.directory = sealed.candidate, sealed.job_dir
    runner._isolation_started_at = isolation.load(sealed.capture / "planner-completion.json")["started_at"]
    runner._isolation_started_monotonic = time.monotonic() - (time.time() - runner._isolation_started_at)
    (sealed.capture / "planner-completion.json").unlink()
    finalized = []
    def existing_run(self):
        try:
            if failure:
                self.job["status"] = "failed"
                self.job["outcome"] = "infrastructure_failed"
                save(sealed.job_dir / "job.json", self.job)
                raise GateFailure("Frozen offline stage failure")
        finally:
            finalized.append("existing cleanup")
    monkeypatch.setattr(isolation.StrataRunner, "run", existing_run)
    if failure:
        with pytest.raises(GateFailure, match="Frozen offline stage failure"):
            runner.run()
        assert not (sealed.capture / "capture.json").exists()
        assert isolation.load(sealed.job_dir / "job.json")["outcome"] == "infrastructure_failed"
    else:
        runner.run()
        assert isolation.verify_capture(sealed.path, sealed.expected)["sessions"] == ["planner"]
    assert finalized == ["existing cleanup"]
    with isolation.exclusive_lock(Path(runner.runtime["resource_lock"])):
        pass


def test_planner_only_denies_direct_implementer_before_capture_or_model(sealed, monkeypatch):
    runner = isolation.IsolatedStrataRunner.__new__(isolation.IsolatedStrataRunner)
    runner._planner_only = True
    runner.isolation, runner.isolation_stages = sealed.freeze, ["planner"]
    monkeypatch.setattr(isolation.StrataRunner, "stage", lambda *_: pytest.fail("Forbidden stage reached"))
    with pytest.raises((InvalidEvaluation, GateFailure), match="Planner-only"):
        runner.stage("implementer", "forbidden", "implementer", sealed.candidate)
    assert not (sealed.capture / "implementer").exists()


@pytest.mark.parametrize("change", ["wire_text", "missing_finish", "coherent_forged_plan"])
def test_final_plan_requires_actual_last_HTTP_text_and_successful_stop(sealed, change):
    raw = (sealed.directory / "http-1.response").read_bytes()
    if change == "missing_finish":
        raw = raw.replace(b'"finish_reason":"stop"', b'"finish_reason":null')
    elif change == "wire_text":
        raw = raw.replace(PLAN.encode(), b"forged")
    else:
        plan = "Coherently forged saved plan"
        (sealed.job_dir / "lead-plan.txt").write_text(plan, encoding="utf-8")
        (sealed.directory / "output.txt").write_bytes(plan.encode())
        save(sealed.directory / "session.jsonl", {"type": "message", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": plan}]}})
        stop = isolation.load(sealed.job_dir / "planner-stop.json")
        stop["plan_sha256"] = sha256(plan.encode())
        save(sealed.job_dir / "planner-stop.json", stop)
    (sealed.directory / "http-1.response").write_bytes(raw)
    reseal(sealed, include_closure=True)
    with pytest.raises(InvalidEvaluation, match="HTTP final plan"):
        publish(sealed)


@pytest.mark.parametrize("flag", [True, 1, "false"])
def test_fullflow_report_cannot_hide_conflicting_or_malformed_job_mode(sealed, flag):
    sealed.freeze["planner_only"] = False
    sealed.job["contract"]["planner_only"] = flag
    sealed.freeze["generation_binding"]["contract_sha256"] = sha256(isolation.encode(sealed.job["contract"]))
    save(sealed.job_dir / "job.json", sealed.job)
    sealed.session("implementer")
    skill = sealed.candidate / SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"synthetic skill\n")
    save(sealed.capture / "capture.json", {"schema": "ephy.triage-generation-capture.v1",
        "freeze_sha256": sealed.expected, "sessions": ["planner", "implementer"],
        "synthetic_only": True, "real_model_generations": 0, "candidate_sha256": sha256(skill.read_bytes())})
    with pytest.raises(InvalidEvaluation, match="boolean|job/flag"):
        isolation.verify_capture(sealed.path, sealed.expected)

def write_workflow(case):
    receipt = isolation.load(case.directory / "receipt.json")
    stop = isolation.load(case.job_dir / "planner-stop.json")
    model = case.freeze["identity"]["model_id"]
    anchor = receipt["process"]["created_at"]
    stamp = lambda offset: datetime.fromtimestamp(anchor + offset, UTC).isoformat()
    records = [
        {"stage": "preflight", "at": stamp(-0.03), "passed": True,
         "contract_sha256": stop["contract_sha256"], "snapshot_sha256": stop["snapshot_sha256"]},
        {"stage": "model loaded", "at": stamp(-0.02), "model": model},
        {"stage": "planner start", "at": stamp(-0.01),
         "label": "planner", "expected_model": model},
        {"stage": "planner end", "at": stamp(0.01),
         "label": "planner", "pid": receipt["process"]["pid"],
         "trace_sha256": sha256((case.directory / "trace.jsonl").read_bytes())},
        {"stage": "planner stop", "at": stamp(0.02), **stop},
    ]
    save(case.capture / "planner-completion.json", {
        "schema": "ephy.triage-planner-completion.v1", "job_id": case.job["id"],
        "freeze_sha256": case.expected, "submission_sha256": case.freeze["generation_submission_sha256"],
        "started_at": anchor - 0.04, "finished_at": anchor + 0.03, "elapsed_seconds": 0.07,
        "controller_process": copy.deepcopy(case.freeze["planner_controller_process"]),
        "planner_receipt_sha256": sha256((case.directory / "receipt.json").read_bytes()),
        "planner_process_sha256": sha256((case.directory / "process.json").read_bytes())})
    (case.job_dir / "workflow.jsonl").write_bytes(
        b"\n".join(consumer.json_bytes(record) for record in records) + b"\n")


@pytest.mark.parametrize("artifact", [
    "worker-1-trace.jsonl", "worker-1-session.jsonl", "worker-1.stdout.log",
    "worker-1.stderr.log", "worker-1-config.json", "worker-1-prompt.txt", "worker-1-system.txt",
    "implementer-trace.jsonl", "implementer.stdout.log", "auditor-trace.jsonl",
    "audit-result.raw.txt", "candidate.patch", "WORKER-2-SESSION.JSONL",
])
def test_parent_audit_implementation_artifact_cannot_be_hidden(sealed, artifact):
    (sealed.job_dir / artifact).write_bytes(b"{}\n")
    with pytest.raises(InvalidEvaluation, match="implementation or audit evidence"):
        publish(sealed)


@pytest.mark.parametrize("change", [
    "implementer_start", "auditor_start", "unknown_stage", "invalid_json",
    "partial_json", "nonobject", "empty_record", "missing_file", "empty_file",
    "missing_start", "reordered", "duplicate", "missing_timestamp", "bad_timestamp",
    "naive_timestamp", "backward_timestamp", "wrong_pid", "wrong_trace", "wrong_stop",
    "wrong_model", "failed_preflight", "wrong_preflight_binding", "missing_newline",
])
def test_parent_audit_workflow_is_complete_known_and_bound(sealed, change):
    path = sealed.job_dir / "workflow.jsonl"
    records = [isolation.json.loads(line) for line in path.read_bytes().splitlines()]
    if change in {"implementer_start", "auditor_start", "unknown_stage"}:
        records.insert(3, {"stage": {"implementer_start": "implementer start",
                                   "auditor_start": "auditor start",
                                   "unknown_stage": "future unknown stage"}[change],
                           "at": "2026-10-06T12:00:02+00:00"})
    elif change == "invalid_json":
        path.write_bytes(b"not json\n")
    elif change == "partial_json":
        path.write_bytes(path.read_bytes() + b'{"stage":')
    elif change == "nonobject":
        records[2] = []
    elif change == "empty_record":
        records[2] = {}
    elif change == "missing_file":
        path.unlink()
    elif change == "empty_file":
        path.write_bytes(b"")
    elif change == "missing_start":
        records.pop(2)
    elif change == "reordered":
        records[2], records[3] = records[3], records[2]
    elif change == "duplicate":
        records.insert(3, records[2])
    elif change == "missing_timestamp":
        records[2].pop("at")
    elif change == "bad_timestamp":
        records[2]["at"] = "not a time"
    elif change == "naive_timestamp":
        records[2]["at"] = "2026-10-06T12:00:02"
    elif change == "backward_timestamp":
        records[2]["at"] = "2026-10-05T12:00:02+00:00"
    elif change == "wrong_pid":
        records[3]["pid"] += 1
    elif change == "wrong_trace":
        records[3]["trace_sha256"] = "0" * 64
    elif change == "wrong_stop":
        records[4]["implementation_started"] = True
    elif change == "wrong_model":
        records[2]["expected_model"] = "another-model"
    elif change == "failed_preflight":
        records[0]["passed"] = False
    elif change == "wrong_preflight_binding":
        records[0]["contract_sha256"] = "0" * 64
    elif change == "missing_newline":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    if change not in {"invalid_json", "partial_json", "missing_file", "empty_file", "missing_newline"}:
        path.write_bytes(b"\n".join(consumer.json_bytes(record) for record in records) + b"\n")
    with pytest.raises((InvalidEvaluation, FileNotFoundError, KeyError, ValueError)):
        publish(sealed)

@pytest.mark.parametrize("field", [
    "humanAuthorization", "schemaVersion", "controls", "model_identities", "repoRoot",
    "verifier_identity", "environment_sha256", "submitting_process", "extra_immutable_field",
])
def test_review_full_submission_identity_cannot_change(sealed, field):
    changed = copy.deepcopy(sealed.job)
    changed[field] = {"forged": True}
    save(sealed.job_dir / "job.json", changed)
    with pytest.raises(InvalidEvaluation, match="submission identity"):
        publish(sealed)


@pytest.mark.parametrize("field", ["humanAuthorization", "schemaVersion", "controls", "model_identities", "repoRoot"])
def test_review_authorized_submission_cannot_be_replaced_by_minimal_job(sealed, field):
    changed = copy.deepcopy(sealed.job)
    changed.pop(field)
    save(sealed.job_dir / "job.json", changed)
    with pytest.raises(InvalidEvaluation, match="submission identity"):
        publish(sealed)


@pytest.mark.parametrize("change", [
    "missing", "over_elapsed", "negative_elapsed", "boolean_elapsed", "unknown_elapsed",
    "nonfinite", "wrong_job", "wrong_freeze", "before_freeze", "start_after_process",
    "finish_before_stop", "wall_over_cap", "elapsed_disagrees", "missing_started_at",
])
def test_review_whole_job_deadline_requires_bound_runner_window(sealed, change):
    path = sealed.capture / "planner-completion.json"
    timing = isolation.load(path)
    if change == "missing":
        path.unlink()
    elif change == "over_elapsed":
        timing["elapsed_seconds"] = 301
    elif change == "negative_elapsed":
        timing["elapsed_seconds"] = -1
    elif change == "boolean_elapsed":
        timing["elapsed_seconds"] = True
    elif change == "unknown_elapsed":
        timing["elapsed_seconds"] = None
    elif change == "nonfinite":
        timing["elapsed_seconds"] = float("inf")
    elif change == "wrong_job":
        timing["job_id"] = "another-job"
    elif change == "wrong_freeze":
        timing["freeze_sha256"] = "0" * 64
    elif change == "before_freeze":
        timing["started_at"] = sealed.freeze["created_at"] - 1
    elif change == "start_after_process":
        timing["started_at"] = isolation.load(sealed.directory / "process.json")["created_at"] + 0.1
    elif change == "finish_before_stop":
        timing["finished_at"] = datetime.fromisoformat(
            isolation.json.loads((sealed.job_dir / "workflow.jsonl").read_bytes().splitlines()[-1])["at"]).timestamp() - 0.001
    elif change == "wall_over_cap":
        timing["finished_at"] = timing["started_at"] + 301
    elif change == "elapsed_disagrees":
        timing["elapsed_seconds"] = 1
    else:
        timing.pop("started_at")
    if change != "missing":
        save(path, timing)
    with pytest.raises((InvalidEvaluation, FileNotFoundError, KeyError, ValueError)):
        publish(sealed)

def inject_duplicate(raw, key, earlier):
    needle = consumer.json_bytes(key) + b":"
    assert needle in raw
    return raw.replace(needle, needle + consumer.json_bytes(earlier) + b"," + needle, 1)


@pytest.mark.parametrize("boundary", [
    "workflow_stage", "workflow_time", "workflow_stop", "trace_kind", "trace_usage",
    "session_role", "session_text", "HTTP_request", "HTTP_response_usage", "HTTP_response_text",
    "HTTP_prefix", "process", "receipt", "completion", "Job", "report",
])
def test_review_duplicate_JSON_cannot_hide_contradictory_approval_evidence(sealed, boundary):
    if boundary.startswith("workflow"):
        path = sealed.job_dir / "workflow.jsonl"
        key, earlier = {"workflow_stage": ("stage", "implementer start"),
                        "workflow_time": ("at", "not a timestamp"),
                        "workflow_stop": ("implementation_started", True)}[boundary]
    elif boundary.startswith("trace"):
        path = sealed.directory / "trace.jsonl"
        key, earlier = ("kind", "implementer") if boundary == "trace_kind" else ("responseTokens", 9999)
    elif boundary.startswith("session"):
        path = sealed.directory / "session.jsonl"
        key, earlier = ("role", "user") if boundary == "session_role" else ("text", "forged")
    elif boundary == "HTTP_request":
        path, key, earlier = sealed.directory / "http-1.json", "max_tokens", 9999
    elif boundary.startswith("HTTP_response"):
        path = sealed.directory / "http-1.response"
        key, earlier = ("completion_tokens", 9999) if boundary == "HTTP_response_usage" else ("content", "forged")
    elif boundary == "HTTP_prefix":
        path = sealed.directory / "http-1.trace-prefix"
        path.write_bytes(b'{"kind":"implementer","kind":"provider_request"}\n')
        key = None
    else:
        target, key, earlier = {
            "process": ("process.json", "pid", 1),
            "receipt": ("receipt.json", "exit_code", 77),
            "completion": ("planner-completion.json", "elapsed_seconds", 9999),
            "Job": ("job.json", "humanAuthorization", "forbidden"),
            "report": ("capture.json", "implementation_requests", 1),
        }[boundary]
        if boundary == "report":
            publish(sealed)
            path = sealed.capture / target
        elif boundary == "Job":
            path = sealed.job_dir / target
        elif boundary == "completion":
            path = sealed.capture / target
        else:
            path = sealed.directory / target
            if boundary == "process" and not path.exists():
                save(path, isolation.load(sealed.directory / "receipt.json")["process"])
    if key is not None:
        path.write_bytes(inject_duplicate(path.read_bytes(), key, earlier))
    if boundary == "HTTP_request":
        receipt = isolation.load(sealed.directory / "http-1.receipt.json")
        receipt["payload_sha256"] = sha256(path.read_bytes())
        save(sealed.directory / "http-1.receipt.json", receipt)
    if boundary in {"trace_kind", "trace_usage", "session_role", "session_text", "HTTP_request",
                    "HTTP_response_usage", "HTTP_response_text", "HTTP_prefix", "process"}:
        # Seal raw bytes without decoding the intentionally ambiguous record.
        receipt_path = sealed.directory / "receipt.json"
        receipt = isolation.load(receipt_path)
        closure = isolation.load(sealed.directory / "http-1.complete.json")
        closure["files"] = {name: sha256((sealed.directory / name).read_bytes()) for name in closure["files"]}
        save(sealed.directory / "http-1.complete.json", closure)
        receipt["captures"] = [sha256((sealed.directory / "http-1.complete.json").read_bytes())]
        receipt["files"] = {p.relative_to(sealed.directory).as_posix(): sha256(p.read_bytes())
                           for p in sealed.directory.rglob("*") if p.is_file() and p.name != "receipt.json"}
        save(receipt_path, receipt)
        workflow = (sealed.job_dir / "workflow.jsonl").read_bytes()
        records = [isolation.json.loads(line) for line in workflow.splitlines()]
        records[3]["trace_sha256"] = sha256((sealed.directory / "trace.jsonl").read_bytes())
        (sealed.job_dir / "workflow.jsonl").write_bytes(
            b"\n".join(consumer.json_bytes(record) for record in records) + b"\n")
    with pytest.raises(InvalidEvaluation, match="Duplicate JSON key"):
        isolation.verify_capture(sealed.path, sealed.expected) if boundary == "report" else publish(sealed)


@pytest.mark.parametrize("missing", ["receipt_owner", "freeze_owner"])
def test_review_completion_requires_pre_frozen_controller_owner(sealed, missing):
    path = sealed.capture / "planner-completion.json"
    timing = isolation.load(path)
    if missing == "receipt_owner":
        timing.pop("controller_process", None)
        save(path, timing)
    else:
        sealed.freeze.pop("planner_controller_process", None)
    with pytest.raises(InvalidEvaluation, match="controller"):
        publish(sealed)

@pytest.mark.parametrize("change", [
    "pid", "boolean_pid", "created_at", "executable", "executable_sha256", "unknown_field",
    "missing_field", "receipt_hash", "process_hash", "parent_pid", "child_owner", "runnerPid",
    "boolean_runnerPid", "live_child", "missing_process_file", "wrong_frozen_pin",
])
def test_review_controller_receipt_requires_exact_owned_process_and_successful_termination(sealed, monkeypatch, change):
    timing_path = sealed.capture / "planner-completion.json"
    timing = isolation.load(timing_path)
    process_path = sealed.directory / "process.json"
    if change in {"pid", "boolean_pid", "created_at", "executable", "executable_sha256", "unknown_field", "missing_field"}:
        owner = timing["controller_process"]
        if change == "pid":
            owner["pid"] += 1
        elif change == "boolean_pid":
            owner["pid"] = True
        elif change == "created_at":
            owner["created_at"] += 1
        elif change == "executable":
            owner["executable"] += ".other"
        elif change == "executable_sha256":
            owner["executable_sha256"] = "0" * 64
        elif change == "unknown_field":
            owner["unexpected"] = True
        else:
            owner.pop("created_at")
        save(timing_path, timing)
    elif change in {"receipt_hash", "process_hash"}:
        timing["planner_" + ("receipt" if change == "receipt_hash" else "process") + "_sha256"] = "0" * 64
        save(timing_path, timing)
    elif change in {"parent_pid", "child_owner"}:
        process = isolation.load(process_path)
        if change == "parent_pid":
            process["parent_pid"] += 1
        else:
            process["controller_process"]["pid"] += 1
        save(process_path, process)
        receipt_path = sealed.directory / "receipt.json"
        receipt = isolation.load(receipt_path)
        receipt["process"] = process
        receipt["files"]["process.json"] = sha256(process_path.read_bytes())
        save(receipt_path, receipt)
        admission = isolation.load(sealed.directory / "http-1.admission.json")
        admission["process"] = process
        save(sealed.directory / "http-1.admission.json", admission)
        http = isolation.load(sealed.directory / "http-1.receipt.json")
        http["generation_admission_sha256"] = sha256((sealed.directory / "http-1.admission.json").read_bytes())
        save(sealed.directory / "http-1.receipt.json", http)
        reseal(sealed, include_closure=True)
    elif change in {"runnerPid", "boolean_runnerPid"}:
        job = copy.deepcopy(sealed.job)
        job["runnerPid"] = True if change == "boolean_runnerPid" else job["runnerPid"] + 1
        save(sealed.job_dir / "job.json", job)
    elif change == "live_child":
        process = isolation.load(process_path)
        original = isolation.psutil.Process
        def lookup(pid=None):
            if pid == process["pid"]:
                return SimpleNamespace(create_time=lambda: process["created_at"], is_running=lambda: True)
            return original(pid)
        monkeypatch.setattr(isolation.psutil, "Process", lookup)
    elif change == "missing_process_file":
        process_path.unlink()
    else:
        owner = sealed.freeze["planner_controller_process"]
        sealed.freeze["pins"][owner["executable"]] = "0" * 64
    with pytest.raises((InvalidEvaluation, FileNotFoundError), match="controller|process|Process|capture"):
        publish(sealed)
