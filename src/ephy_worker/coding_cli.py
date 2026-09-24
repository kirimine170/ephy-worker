"""CLI adapters for local coding execution and model-comparison fixtures．"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .coding_executor import CodingExecutionError, CodingExecutor
from .coding_profiles import load_coding_profiles
from .coding_review import verify_source_manifest
from .coding_schema import CodingJob
from .evaluation import default_evaluation_root, load_suite, run_evaluation_suite


def _load_job(path: Path) -> CodingJob:
    try:
        return CodingJob.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise CodingExecutionError(
            "internal_worker_failure", "worker", f"invalid coding job ({type(exc).__name__})"
        ) from None


def _profile(profile_id: str, catalog_path: Path | None):
    try:
        profiles = load_coding_profiles(catalog_path)
    except ValueError as exc:
        raise CodingExecutionError("invalid_model_profile", "profile", str(exc)) from None
    try:
        return profiles[profile_id]
    except KeyError:
        raise CodingExecutionError(
            "invalid_model_profile", "profile", f"unknown coding profile: {profile_id}"
        ) from None


async def run_coding_command(args) -> int:
    job = _load_job(args.job_file)
    profile = _profile(job.model_profile, args.profiles)
    executor = CodingExecutor()
    if args.dry_run:
        print(executor.plan(job, profile).model_dump_json(indent=2))
        return 0
    root = args.output_dir or default_evaluation_root()
    result, directory = await executor.execute(job, profile, root)
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.result.status,
                "validation_passed": result.result.validation_passed,
                "result": str(directory / "result.json"),
                "artifacts": str(directory),
            },
            ensure_ascii=False,
        )
    )
    if result.result.status == "cancelled":
        return 130
    return 0 if result.result.status == "succeeded" else 1


def run_coding_verify_command(args) -> int:
    verification = verify_source_manifest(args.artifact_dir, args.repository)
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0 if verification["ok"] else 1


async def run_evaluation_command(args) -> int:
    profile = _profile(args.model, args.profiles)
    try:
        suite = load_suite(args.suite)
        results = await run_evaluation_suite(
            suite,
            profile,
            args.output_dir or default_evaluation_root(),
            repeat=args.repeat,
        )
    except ValueError as exc:
        raise CodingExecutionError("internal_worker_failure", "worker", str(exc)) from None
    payload = {
        "suite": suite.suite_id,
        "model": profile.profile_id,
        "repeat": args.repeat,
        "runs": [
            {
                "run_id": result.run_id,
                "task_id": result.task_id,
                "status": result.result.status,
                "validation_passed": result.result.validation_passed,
                "result": str(directory / "result.json"),
            }
            for result, directory in results
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if results and all(result.result.status == "succeeded" for result, _ in results) else 1
