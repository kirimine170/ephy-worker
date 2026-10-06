"""Finite proposal-only campaign. Resume advances logs, never repeats an interrupted Job."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from .formal_artifacts import GateFailure, digest, encode, now, read_json, write_json
from .formal_runtime import FormalRunner, exclusive_lock, submit


def run_campaign(plan_file: Path, state_root: Path, *, resume: bool = False) -> dict:
    plan = read_json(plan_file)
    if set(plan) != {"specs", "max_consecutive_failures", "timeout_seconds", "resource_lock"}:
        raise GateFailure("Invalid campaign contract")
    if not 1 <= len(plan["specs"]) <= 20 or not 1 <= plan["max_consecutive_failures"] <= 3:
        raise GateFailure("Campaign must have finite jobs and a failure stop")
    if type(plan["timeout_seconds"]) is not int or not 1 <= plan["timeout_seconds"] <= 86400:
        raise GateFailure("Campaign deadline must be finite and <= 24 hours")
    identity = digest(encode(plan))
    with exclusive_lock(Path(plan["resource_lock"])):
        state_path = state_root / "campaign.json"
        if resume:
            state = read_json(state_path)
            if state["plan_sha256"] != identity:
                raise GateFailure("Cannot resume under a changed campaign plan")
            if state["status"] in ("completed", "stopped", "failed"):
                return state
            # Interrupted candidate stays preserved. It counts as a failed attempt;
            # no unknown implementation/check/audit is replayed as completed.
            if state.get("active_job"):
                job_file = Path(state["active_job"])
                job = read_json(job_file)
                if job["status"] not in ("review_ready", "verification_failed", "failed"):
                    job.update(
                        status="failed",
                        message="Controller interrupted; Job is not replayed",
                        updatedAt=now(),
                    )
                    write_json(job_file, job, exclusive=False)
                state["results"].append({"job_file": str(job_file), "status": job["status"]})
                state["next_index"] += 1
                state["consecutive_failures"] = (
                    0 if job["status"] == "review_ready" else state["consecutive_failures"] + 1
                )
                state["active_job"] = None
        else:
            state_root.mkdir()  # never replace an old campaign
            write_json(state_root / "plan.json", plan)
            state = {
                "plan_sha256": identity,
                "started_at": now(),
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
        try:
            while state["next_index"] < len(plan["specs"]):
                if state["results"] and state["results"][0]["status"] != "review_ready":
                    state["status"] = "stopped"
                    state["reason"] = "First formal cycle did not validate; recursion not enabled"
                    break
                if (
                    (state_root / "STOP").exists()
                    or state["elapsed_seconds"] >= plan["timeout_seconds"]
                    or state["consecutive_failures"] >= plan["max_consecutive_failures"]
                ):
                    state["status"] = "stopped"
                    state["reason"] = "stop request, campaign deadline, or consecutive failure limit"
                    break
                spec = plan["specs"][state["next_index"]]
                if spec["runtime"]["resource_lock"] != plan["resource_lock"]:
                    raise GateFailure("Campaign/job singleton locks disagree")
                if spec["contract"]["timeout_seconds"] > plan["timeout_seconds"] - state["elapsed_seconds"]:
                    state["status"] = "stopped"
                    state["reason"] = "Insufficient remaining campaign budget for another whole Job"
                    break
                job_file = submit(spec, state_root)
                state["active_job"] = str(job_file)
                save()
                try:
                    runner = FormalRunner(job_file)
                    runner.campaign_stop = state_root / "STOP"
                    runner.run()
                except Exception:
                    # The runner saved the exact failure. It is counted, never converted to PASS.
                    if not (job_file.parent / "runner-error.json").exists():
                        raise
                job = read_json(job_file)
                state["results"].append({"job_file": str(job_file), "status": job["status"]})
                state["consecutive_failures"] = (
                    0 if job["status"] == "review_ready" else state["consecutive_failures"] + 1
                )
                state["next_index"] += 1
                state["active_job"] = None
                save()
                if state["next_index"] == 1 and job["status"] != "review_ready":
                    state["status"] = "stopped"
                    state["reason"] = "First formal cycle did not validate; recursion not enabled"
                    break
            else:
                state["status"] = "completed"
        except Exception as exc:
            state.update(status="failed", reason=str(exc))
            raise
        finally:
            save()
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
