"""Finite proposal-only campaign. Resume advances logs, never repeats an interrupted Job."""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .formal_artifacts import GateFailure, digest, encode, now, read_json, write_json
from .formal_runtime import FormalRunner, exclusive_lock, freeze_submission_identity, submit

PLANNER_STOP_REASON = "Planner-only boundary reached; no next job is authorized"


def proposal_valid(job_file: Path) -> bool:
    job = read_json(job_file)
    if job["status"] == "external_review_pending":
        from .strata_runtime import verify_external_proposal

        verify_external_proposal(job_file)
        return True
    if job["status"] == "review_ready":
        from .formal_runtime import verify_proposal_for_integration

        verify_proposal_for_integration(job_file)
        return True
    return False


def retained_verifier_freeze(state_root: Path, state: dict, identity: str, count: int) -> dict:
    """Validate retained expectations even when no further trial will execute."""
    try:
        freeze = read_json(state_root / "verifier-freeze.json")
    except FileNotFoundError as exc:
        raise GateFailure("Campaign verifier freeze is missing; cannot recapture") from exc
    if (
        state.get("verifier_freeze_sha256") != digest(encode(freeze))
        or freeze.get("plan_sha256") != identity
        or len(freeze.get("verifier_identities", [])) != count
    ):
        raise GateFailure("Campaign verifier freeze is missing or changed; cannot recapture")
    return freeze


def run_campaign(plan_file: Path, state_root: Path, *, resume: bool = False) -> dict:
    plan = read_json(plan_file)
    if set(plan) != {"specs", "max_consecutive_failures", "timeout_seconds", "resource_lock"}:
        raise GateFailure("Invalid campaign contract")
    if not 1 <= len(plan["specs"]) <= 20 or not 1 <= plan["max_consecutive_failures"] <= 3:
        raise GateFailure("Campaign must have finite jobs and a failure stop")
    if type(plan["timeout_seconds"]) is not int or not 1 <= plan["timeout_seconds"] <= 86400:
        raise GateFailure("Campaign deadline must be finite and <= 24 hours")
    if (
        any(spec["runtime"].get("backend") == "external_strata" for spec in plan["specs"])
        and plan["timeout_seconds"] > 14400
    ):
        raise GateFailure("External Strata campaigns are bounded to four hours")
    identity = digest(encode(plan))
    with exclusive_lock(Path(plan["resource_lock"])):
        state_path = state_root / "campaign.json"
        if resume:
            state = read_json(state_path)
            if state["plan_sha256"] != identity:
                raise GateFailure("Cannot resume under a changed campaign plan")
            retained_verifier_freeze(state_root, state, identity, len(plan["specs"]))
            if state["status"] in ("completed", "stopped", "failed"):
                return state
            # Interrupted candidate stays preserved. It counts as a failed attempt;
            # no unknown implementation/check/audit is replayed as completed.
            if state.get("active_job"):
                job_file = Path(state["active_job"])
                job = read_json(job_file)
                if job["status"] not in (
                    "review_ready",
                    "external_review_pending",
                    "verification_failed",
                    "failed",
                    "planner_stopped",
                ):
                    job.update(
                        status="failed",
                        message="Controller interrupted; Job is not replayed",
                        updatedAt=now(),
                    )
                    if job.get("runtime", {}).get("backend") == "external_strata":
                        job["outcome"] = "infrastructure_failed"
                    write_json(job_file, job, exclusive=False)
                state["results"].append({"job_file": str(job_file), "status": job["status"]})
                state["next_index"] += 1
                state["consecutive_failures"] = (
                    0 if proposal_valid(job_file) else state["consecutive_failures"] + 1
                )
                state["active_job"] = None
                if job["status"] == "planner_stopped":
                    state.update(status="stopped", reason=PLANNER_STOP_REASON)
                if (
                    job.get("runtime", {}).get("backend") == "external_strata"
                    and job.get("outcome") == "infrastructure_failed"
                ):
                    state.update(
                        status="stopped", reason="Interrupted Strata infrastructure failure; no replay"
                    )
        else:
            # Freeze only fresh drafts in this executing controller. The immutable
            # record remains the expectation across later jobs and resume.
            observations = []
            identities = []
            for draft in plan["specs"]:
                observation = {}
                frozen = freeze_submission_identity(draft, observation=observation)
                identities.append(frozen["verifier_identity"])
                observations.append(observation)
            freeze = {
                "plan_sha256": identity,
                "verifier_identities": identities,
                "observations": observations,
            }
            state_root.mkdir()  # never replace an old campaign
            write_json(state_root / "plan.json", plan)
            write_json(state_root / "verifier-freeze.json", freeze)
            state = {
                "plan_sha256": identity,
                "verifier_freeze_sha256": digest(encode(freeze)),
                "started_at": now(),
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=plan["timeout_seconds"])).isoformat(),
                "status": "running",
                "next_index": 0,
                "consecutive_failures": 0,
                "results": [],
                "active_job": None,
                "human_interventions": 0,
                "elapsed_seconds": 0,
            }
        started = time.monotonic()
        previous_elapsed = state["elapsed_seconds"]

        def save():
            state["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
            state["updated_at"] = now()
            write_json(state_path, state, exclusive=False)

        save()
        if state["status"] == "stopped":
            retained_verifier_freeze(state_root, state, identity, len(plan["specs"]))
            return state
        if state["consecutive_failures"] >= plan["max_consecutive_failures"]:
            state.update(status="stopped", reason="Consecutive candidate failure limit reached")
            save()
            retained_verifier_freeze(state_root, state, identity, len(plan["specs"]))
            return state
        try:
            # Resume loads the original expectations, never today's environment.
            freeze = retained_verifier_freeze(state_root, state, identity, len(plan["specs"]))
            while state["next_index"] < len(plan["specs"]):
                if state["results"] and not proposal_valid(Path(state["results"][0]["job_file"])):
                    state["status"] = "stopped"
                    state["reason"] = "First formal cycle did not validate; recursion not enabled"
                    break
                if (
                    (state_root / "STOP").exists()
                    or (
                        state.get("deadline_at")
                        and datetime.now(UTC) >= datetime.fromisoformat(state["deadline_at"])
                    )
                    or state["elapsed_seconds"] >= plan["timeout_seconds"]
                    or state["consecutive_failures"] >= plan["max_consecutive_failures"]
                ):
                    state["status"] = "stopped"
                    state["reason"] = "stop request, campaign deadline, or consecutive failure limit"
                    break
                index = state["next_index"]
                draft = plan["specs"][index]
                if draft["runtime"]["resource_lock"] != plan["resource_lock"]:
                    raise GateFailure("Campaign/job singleton locks disagree")
                if draft["contract"]["timeout_seconds"] > plan["timeout_seconds"] - state["elapsed_seconds"]:
                    state["status"] = "stopped"
                    state["reason"] = "Insufficient remaining campaign budget for another whole Job"
                    break
                if (
                    state.get("deadline_at")
                    and draft["contract"]["timeout_seconds"]
                    > (datetime.fromisoformat(state["deadline_at"]) - datetime.now(UTC)).total_seconds()
                ):
                    state.update(
                        status="stopped",
                        reason="Insufficient remaining wall-clock budget for another whole Job",
                    )
                    break
                observation = {}
                expected = freeze["verifier_identities"][index]
                spec = None
                try:
                    spec = freeze_submission_identity(draft, observation=observation)
                    if spec["verifier_identity"] != expected:
                        raise GateFailure("Campaign verifier changed after freeze; cannot recapture")
                finally:
                    # A rejected submission has no Job, so keep its redacted
                    # comparison here before allowing submit or any model stage.
                    write_json(
                        state_root / ("verifier-submission-" + str(index) + ".json"),
                        {
                            "expected_identity": expected,
                            "observed_identity": spec["verifier_identity"] if spec else None,
                            "matches": spec is not None and spec["verifier_identity"] == expected,
                            "observation": observation,
                        },
                    )
                job_file = submit(spec, state_root)
                state["active_job"] = str(job_file)
                save()
                try:
                    if spec["runtime"].get("backend", "owned_llama") != "owned_llama":
                        from .strata_runtime import make_runner

                        runner = make_runner(job_file)
                    else:
                        runner = FormalRunner(job_file)
                    runner.campaign_stop = state_root / "STOP"
                    runner.run()
                except Exception:
                    # The runner saved the exact failure. It is counted, never converted to PASS.
                    if not (job_file.parent / "runner-error.json").exists():
                        raise
                job = read_json(job_file)
                state["results"].append({"job_file": str(job_file), "status": job["status"]})
                valid = proposal_valid(job_file)
                state["consecutive_failures"] = 0 if valid else state["consecutive_failures"] + 1
                state["next_index"] += 1
                state["active_job"] = None
                if job["status"] == "planner_stopped":
                    state.update(status="stopped", reason=PLANNER_STOP_REASON)
                save()
                if job["status"] == "planner_stopped":
                    break
                if (
                    spec["runtime"].get("backend") == "external_strata"
                    and job.get("outcome") == "infrastructure_failed"
                ):
                    state["status"] = "stopped"
                    state["reason"] = "Strata boundary/resource/budget/infrastructure failure; no repair"
                    break
                if state["next_index"] == 1 and not valid:
                    state["status"] = "stopped"
                    state["reason"] = "First formal cycle did not validate; recursion not enabled"
                    break
                if state["consecutive_failures"] >= plan["max_consecutive_failures"]:
                    state.update(status="stopped", reason="Consecutive candidate failure limit reached")
                    break
            else:
                state["status"] = "completed"
        except Exception as exc:
            state.update(status="failed", reason=str(exc))
            raise
        finally:
            save()
        retained_verifier_freeze(state_root, state, identity, len(plan["specs"]))
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--execute-authorized",
        action="store_true",
        required=True,
        help="Trusted controller records the user's explicit execution authorization",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    run_campaign(args.plan, args.state, resume=args.resume)


if __name__ == "__main__":
    main()
