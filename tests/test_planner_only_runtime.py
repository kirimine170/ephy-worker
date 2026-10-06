"""Offline controls for the frozen planner-only controller boundary."""

import copy
import time
from types import SimpleNamespace

import pytest
import test_formal_runtime
from test_formal_runtime import (
    REPOSITORY,
    contract,
    verifier_draft,
    verifier_runner,
)

from ephy_worker import formal_campaign
from ephy_worker.formal_artifacts import (
    CONTROL_PATHS,
    GateFailure,
    digest,
    encode,
    file_hash,
    read_json,
    write_json,
)
from ephy_worker.formal_runtime import FormalRunner, snapshot, validate_contract

planner_prompt_case = test_formal_runtime.planner_prompt_case

PLAN = "Read the frozen Markdown checklist, propose one scoped clarification, and stop."


@pytest.fixture(autouse=True)
def forbid_http(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline control attempted HTTP")
    monkeypatch.setattr("httpx.Client.send", blocked)
    monkeypatch.setattr("httpx.AsyncClient.send", blocked)


@pytest.fixture
def planner_only_case(planner_prompt_case, monkeypatch, tmp_path):
    def build(**overrides):
        settings = {
            "planner_only": True, "max_repairs": 0, "max_requests": 8,
            "output_token_budget": 3983, "max_response_tokens": 497,
        }
        settings.update(overrides)
        case = planner_prompt_case(**settings)
        runner = case.runner
        runner.job["id"] = "offline-planner-only"
        runner.runtime["governance_root"] = str(REPOSITORY)
        runner.job["controls"] = {
            name: file_hash(REPOSITORY / path) for name, path in CONTROL_PATHS.items()
        }
        for name in ("checker", "checker_controls"):
            path = tmp_path / (name + ".txt")
            path.write_text("Frozen offline control\n", encoding="utf-8")
            runner.contract[name] = str(path)
            runner.contract[name + "_sha256"] = file_hash(path)
        runner.contract_sha = digest(encode(runner.contract))
        write_json(runner.job_file, runner.job, exclusive=False)
        case.initial = copy.deepcopy(runner.job)
        capture = runner.stage

        def stage(*args, **kwargs):
            capture(*args, **kwargs)
            return PLAN

        monkeypatch.setattr(runner, "stage", stage)
        return case
    return build


@pytest.mark.parametrize("mode", ["absent", False, True])
def test_contract_accepts_only_explicit_optional_mode(mode):
    value = contract()
    if mode != "absent":
        value["planner_only"] = mode
    if mode is True:
        value["max_repairs"] = 0
    validate_contract(value)


@pytest.mark.parametrize("mode", [0, 1, None, "true", "false", {}, []])
def test_contract_rejects_non_boolean_planner_mode(mode):
    value = contract()
    value.update(planner_only=mode, max_repairs=0)
    with pytest.raises(GateFailure, match="planner_only"):
        validate_contract(value)


@pytest.mark.parametrize("repairs", [1, 2])
def test_planner_only_contract_refuses_repairs(repairs):
    value = contract()
    value.update(planner_only=True, max_repairs=repairs)
    with pytest.raises(GateFailure, match="repair"):
        validate_contract(value)


def test_concrete_plan_stops_without_implementation_or_audit(planner_only_case):
    case = planner_only_case()
    before = snapshot(case.runner.candidate)
    case.runner.run()
    saved = read_json(case.runner.job_file)
    assert saved["status"] == "planner_stopped" and saved["outcome"] == "planned_only"
    assert [call.role for call in case.calls] == ["planner"]
    assert case.preflights == ["offline preflight"]
    assert snapshot(case.runner.candidate) == before
    assert (case.runner.directory / "lead-plan.txt").read_text(encoding="utf-8") == PLAN
    stop = read_json(case.runner.directory / "planner-stop.json")
    assert stop["job_id"] == saved["id"]
    assert stop["contract_sha256"] == digest(encode(saved["contract"]))
    assert stop["plan_sha256"] == digest(PLAN.encode("utf-8"))
    assert stop["snapshot_sha256"] == digest(encode(before))
    assert stop["implementation_started"] is False
    assert stop["formal_audit_executed"] is False
    assert stop["automatic_adoption"] is False
    assert read_json(case.runner.directory / "retained-candidate-snapshot.json") == before
    assert not (case.runner.directory / "audit-bundle").exists()
    assert not (case.runner.directory / "candidate.patch").exists()
    assert not (case.runner.directory / "audit-result.json").exists()
    assert [event["stage"] for event in case.runner.events] == ["planner stop"]


def test_explicit_false_retains_normal_implementer_handoff(planner_only_case, monkeypatch):
    case = planner_only_case(planner_only=False)
    capture = case.runner.stage

    def stage(role, *args, **kwargs):
        result = capture(role, *args, **kwargs)
        if role != "planner":
            raise GateFailure("Offline implementer boundary")
        return result

    monkeypatch.setattr(case.runner, "stage", stage)
    with pytest.raises(GateFailure, match="Offline implementer boundary"):
        case.runner.run()
    assert [call.role for call in case.calls] == ["planner", "implementer"]
    assert not (case.runner.directory / "planner-stop.json").exists()


@pytest.mark.parametrize("role", ["implementer", "auditor"])
def test_direct_non_planner_stage_is_denied_before_load(planner_only_case, monkeypatch, role):
    case = planner_only_case()
    monkeypatch.setattr(
        case.runner, "load_model", lambda _: pytest.fail("Forbidden role reached model loading"),
    )
    with pytest.raises(GateFailure, match="Planner-only"):
        FormalRunner.stage(case.runner, role, "Do not execute", role, case.runner.candidate)
    assert not case.calls


def test_no_hypothesis_is_retained_as_a_planner_stop(planner_only_case, monkeypatch):
    case = planner_only_case()
    capture = case.runner.stage

    def stage(*args, **kwargs):
        capture(*args, **kwargs)
        return "NO_HYPOTHESIS"

    monkeypatch.setattr(case.runner, "stage", stage)
    case.runner.run()
    saved = read_json(case.runner.job_file)
    assert saved["status"] == "planner_stopped" and saved["outcome"] == "no_hypothesis"
    assert [call.role for call in case.calls] == ["planner"]
    assert read_json(case.runner.directory / "BACKLOG.json")["reason"] == "NO_HYPOTHESIS"


@pytest.mark.parametrize("change", ["disk_contract", "memory_contract", "candidate"])
def test_planner_drift_cannot_disable_the_stop(planner_only_case, monkeypatch, change):
    case = planner_only_case()
    capture = case.runner.stage

    def stage(*args, **kwargs):
        capture(*args, **kwargs)
        if change == "disk_contract":
            job = read_json(case.runner.job_file)
            job["contract"]["planner_only"] = False
            write_json(case.runner.job_file, job, exclusive=False)
        elif change == "memory_contract":
            case.runner.contract["planner_only"] = False
        else:
            (case.runner.candidate / "README.md").write_text("Forbidden edit\n", encoding="utf-8")
        return PLAN

    monkeypatch.setattr(case.runner, "stage", stage)
    with pytest.raises(GateFailure, match="contract changed|Planner modified"):
        case.runner.run()
    assert [call.role for call in case.calls] == ["planner"]
    assert read_json(case.runner.job_file)["outcome"] == "infrastructure_failed"
    assert not (case.runner.directory / "planner-stop.json").exists()


def test_empty_final_plan_fails_closed(planner_only_case, monkeypatch):
    case = planner_only_case()
    monkeypatch.setattr(case.runner, "stage", lambda *args, **kwargs: " \n")
    with pytest.raises(GateFailure, match="empty plan"):
        case.runner.run()
    assert read_json(case.runner.job_file)["outcome"] == "infrastructure_failed"
    assert not (case.runner.directory / "planner-stop.json").exists()


def test_deadline_after_planning_fails_without_handoff(planner_only_case):
    case = planner_only_case()
    case.runner.deadline = time.monotonic() - 1
    with pytest.raises(GateFailure, match="deadline"):
        case.runner.run()
    assert [call.role for call in case.calls] == ["planner"]
    assert not (case.runner.directory / "planner-stop.json").exists()


def test_stage_failure_never_advances(planner_only_case, monkeypatch):
    case = planner_only_case()
    capture = case.runner.stage

    def stage(*args, **kwargs):
        capture(*args, **kwargs)
        raise GateFailure("Offline stage failure")

    monkeypatch.setattr(case.runner, "stage", stage)
    with pytest.raises(GateFailure, match="Offline stage failure"):
        case.runner.run()
    assert [call.role for call in case.calls] == ["planner"]
    assert read_json(case.runner.job_file)["outcome"] == "infrastructure_failed"


def test_planner_stop_never_stops_an_unowned_service(planner_only_case, monkeypatch):
    case = planner_only_case()
    case.runner.server = SimpleNamespace(pid=100)
    case.runner.server_owned = False
    monkeypatch.setattr(
        "ephy_worker.formal_runtime.stop_tree", lambda _: pytest.fail("Existing service stopped"),
    )
    case.runner.run()
    assert read_json(case.runner.job_file)["status"] == "planner_stopped"


def test_campaign_stops_at_plan_and_resume_preserves_it(tmp_path, monkeypatch):
    spec = verifier_draft(verifier_runner(tmp_path))
    spec["runtime"]["resource_lock"] = str(tmp_path / "lock")
    spec["contract"].update(planner_only=True, max_repairs=0)
    plan = {
        "specs": [copy.deepcopy(spec) for _ in range(2)],
        "max_consecutive_failures": 3, "timeout_seconds": 600,
        "resource_lock": str(tmp_path / "lock"),
    }
    plan_file = tmp_path / "campaign-plan.json"
    write_json(plan_file, plan)
    calls = []

    def make_runner(job_file):
        def run():
            job = read_json(job_file)
            calls.append(job_file)
            job.update(status="planner_stopped", outcome="planned_only")
            write_json(job_file, job, exclusive=False)
        return SimpleNamespace(run=run)

    monkeypatch.setattr(formal_campaign, "FormalRunner", make_runner)
    state_root = tmp_path / "campaign"
    state = formal_campaign.run_campaign(plan_file, state_root)
    assert len(calls) == 1 and state["status"] == "stopped"
    assert state["reason"] == "Planner-only boundary reached; no next job is authorized"
    assert formal_campaign.proposal_valid(calls[0]) is False
    job_bytes = calls[0].read_bytes()

    # Simulate a controller interruption after the terminal Job but before its
    # campaign checkpoint. This uses only this test's synthetic state files.
    state.update(status="running", active_job=str(calls[0]), results=[], next_index=0,
                 consecutive_failures=0)
    write_json(state_root / "campaign.json", state, exclusive=False)
    resumed = formal_campaign.run_campaign(plan_file, state_root, resume=True)
    assert resumed["status"] == "stopped"
    assert resumed["reason"] == "Planner-only boundary reached; no next job is authorized"
    assert len(calls) == 1 and calls[0].read_bytes() == job_bytes

def test_later_planner_stop_survives_termination_after_checkpoint(tmp_path, monkeypatch):
    spec = verifier_draft(verifier_runner(tmp_path))
    spec["runtime"]["resource_lock"] = str(tmp_path / "lock")
    spec["contract"].update(planner_only=False, max_repairs=0)
    specs = [copy.deepcopy(spec) for _ in range(3)]
    specs[1]["contract"]["planner_only"] = True
    plan = {
        "specs": specs, "max_consecutive_failures": 3, "timeout_seconds": 600,
        "resource_lock": str(tmp_path / "lock"),
    }
    plan_file = tmp_path / "campaign-plan.json"
    state_root = tmp_path / "campaign"
    state_path = state_root / "campaign.json"
    write_json(plan_file, plan)
    calls = []

    def make_runner(job_file):
        def run():
            job = read_json(job_file)
            calls.append(job_file)
            if job["contract"]["planner_only"]:
                job.update(status="planner_stopped", outcome="planned_only")
            else:
                job.update(status="review_ready")
            write_json(job_file, job, exclusive=False)
        return SimpleNamespace(run=run)

    monkeypatch.setattr(formal_campaign, "FormalRunner", make_runner)
    monkeypatch.setattr(
        formal_campaign, "proposal_valid",
        lambda job_file: read_json(job_file)["status"] == "review_ready",
    )
    writer = formal_campaign.write_json
    checkpointed = False

    def terminate_after_checkpoint(path, value, *args, **kwargs):
        nonlocal checkpointed
        if path == state_path and value["next_index"] == 2 and value["active_job"] is None:
            if not checkpointed:
                writer(path, value, *args, **kwargs)
                checkpointed = True
            # An abrupt process exit cannot run a successful finally checkpoint.
            raise SystemExit("Simulated termination immediately after planner checkpoint")
        return writer(path, value, *args, **kwargs)

    monkeypatch.setattr(formal_campaign, "write_json", terminate_after_checkpoint)
    with pytest.raises(SystemExit, match="immediately after planner checkpoint"):
        formal_campaign.run_campaign(plan_file, state_root)
    assert checkpointed and len(calls) == 2
    checkpoint = read_json(state_path)
    retained_jobs = [job_file.read_bytes() for job_file in calls]
    monkeypatch.setattr(formal_campaign, "write_json", writer)
    resumed = formal_campaign.run_campaign(plan_file, state_root, resume=True)

    assert checkpoint["status"] == "stopped"
    assert checkpoint["reason"] == formal_campaign.PLANNER_STOP_REASON
    assert checkpoint["next_index"] == 2 and checkpoint["active_job"] is None
    assert [r["status"] for r in checkpoint["results"]] == ["review_ready", "planner_stopped"]
    assert resumed["status"] == "stopped" and resumed["reason"] == formal_campaign.PLANNER_STOP_REASON
    assert resumed["next_index"] == 2 and len(resumed["results"]) == 2
    assert len(calls) == 2
    assert [job_file.read_bytes() for job_file in calls] == retained_jobs

@pytest.mark.parametrize("planner_only", [False, True])
@pytest.mark.parametrize("resume_after_failure", [False, True])
def test_planner_only_failed_attempt_stops_all_later_jobs(tmp_path, monkeypatch,
                                                        planner_only, resume_after_failure):
    spec = verifier_draft(verifier_runner(tmp_path))
    spec["runtime"]["resource_lock"] = str(tmp_path / "lock")
    spec["contract"].update(planner_only=False, max_repairs=0)
    specs = [copy.deepcopy(spec) for _ in range(3)]
    specs[1]["contract"]["planner_only"] = planner_only
    plan = {
        "specs": specs, "max_consecutive_failures": 3, "timeout_seconds": 600,
        "resource_lock": str(tmp_path / "lock"),
    }
    plan_file = tmp_path / "campaign-plan.json"
    state_root = tmp_path / "campaign"
    write_json(plan_file, plan)
    calls = []
    failed_bytes = []

    def make_runner(job_file):
        def run():
            job = read_json(job_file)
            calls.append(job_file)
            if len(calls) == 2:
                job.update(status="failed", outcome="infrastructure_failed",
                           message="Frozen budget/stage/cancellation gate failed")
                write_json(job_file, job, exclusive=False)
                failed_bytes.append(job_file.read_bytes())
                write_json(job_file.parent / "runner-error.json", {"error": job["message"]})
                if resume_after_failure:
                    raise SystemExit("Controller terminated after failed Job")
                raise GateFailure(job["message"])
            job.update(status="review_ready")
            write_json(job_file, job, exclusive=False)
        return SimpleNamespace(run=run)

    monkeypatch.setattr(formal_campaign, "FormalRunner", make_runner)
    monkeypatch.setattr(
        formal_campaign, "proposal_valid",
        lambda job_file: read_json(job_file)["status"] == "review_ready",
    )
    if resume_after_failure:
        with pytest.raises(SystemExit, match="after failed Job"):
            formal_campaign.run_campaign(plan_file, state_root)
        interrupted = read_json(state_root / "campaign.json")
        assert len(calls) == 2 and interrupted["next_index"] == 1
        assert interrupted["active_job"] == str(calls[1])
        state = formal_campaign.run_campaign(plan_file, state_root, resume=True)
    else:
        state = formal_campaign.run_campaign(plan_file, state_root)

    expected_jobs = 2 if planner_only else 3
    assert len(calls) == expected_jobs and state["next_index"] == expected_jobs
    assert len(state["results"]) == expected_jobs and state["active_job"] is None
    assert state["status"] == ("stopped" if planner_only else "completed")
    if planner_only:
        assert state["reason"] == formal_campaign.PLANNER_STOP_REASON
    assert read_json(calls[1])["status"] == "failed"
    assert read_json(calls[1])["outcome"] == "infrastructure_failed"
    assert calls[1].read_bytes() == failed_bytes[0]
