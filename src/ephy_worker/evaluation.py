"""Offline coding evaluation suites driven through the normal CodingExecutor．"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .coding_executor import CodingExecutionError, CodingExecutor
from .coding_schema import CodingFixture, CodingJob, CodingModelProfile, CodingResult, CodingSuite


def default_evaluation_root() -> Path:
    configured = os.environ.get("EPHY_WORKER_EVAL_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "ephy-worker" / "eval" / "runs"


def load_suite(suite_id: str) -> CodingSuite:
    if not suite_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid suite ID")
    resource = files("ephy_worker").joinpath("fixtures", "coding", f"{suite_id}.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
        return CodingSuite.model_validate(raw)
    except FileNotFoundError:
        raise ValueError(f"unknown evaluation suite: {suite_id}") from None
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid evaluation suite ({type(exc).__name__})") from None


def _write_fixture_file(root: Path, relative: str, content: str) -> None:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def create_fixture_repository(fixture: CodingFixture) -> tuple[Path, str]:
    root = Path(tempfile.mkdtemp(prefix=f"ephy-worker-fixture-{fixture.task_id}-"))
    try:
        for relative, content in fixture.files.items():
            _write_fixture_file(root, relative, content)
        environment = os.environ.copy()
        environment.update(
            GIT_AUTHOR_NAME="Ephy Fixture",
            GIT_AUTHOR_EMAIL="fixture@invalid.example",
            GIT_COMMITTER_NAME="Ephy Fixture",
            GIT_COMMITTER_EMAIL="fixture@invalid.example",
        )
        commands = (
            ["git", "init", "-q", "-b", "main"],
            ["git", "add", "--", "."],
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture base"],
        )
        for command in commands:
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise CodingExecutionError(
                    "repository_unavailable",
                    "repository",
                    (result.stderr or result.stdout).strip()[:1000],
                )
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        ).stdout.strip()
        return root, revision
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def fixture_job(
    fixture: CodingFixture,
    repository: Path,
    revision: str,
    profile: CodingModelProfile,
    *,
    suffix: str,
) -> CodingJob:
    validation = [sys.executable if part == "{python}" else part for part in fixture.validation_command]
    return CodingJob(
        job_id=f"eval-{fixture.task_id}-{suffix}",
        task_id=fixture.task_id,
        goal=fixture.description,
        repository_path=repository,
        base_revision=revision,
        validation_command=validation,
        timeout_seconds=fixture.timeout_seconds,
        network_policy="offline",
        model_profile=profile.profile_id,
        mock_changes=fixture.mock_changes,
    )


async def run_evaluation_suite(
    suite: CodingSuite,
    profile: CodingModelProfile,
    artifact_root: Path,
    *,
    repeat: int = 1,
    cancel_event: asyncio.Event | None = None,
) -> list[tuple[CodingResult, Path]]:
    if repeat < 1 or repeat > 100:
        raise ValueError("repeat must be between 1 and 100")
    cancel_event = cancel_event or asyncio.Event()
    results: list[tuple[CodingResult, Path]] = []
    executor = CodingExecutor()
    for iteration in range(1, repeat + 1):
        for fixture in suite.tasks:
            repository, revision = create_fixture_repository(fixture)
            suffix = f"r{iteration}-{uuid4().hex[:12]}"
            run_id = f"eval-{suite.suite_id}-{fixture.task_id}-{suffix}"
            try:
                job = fixture_job(fixture, repository, revision, profile, suffix=suffix)
                result = await executor.execute(
                    job,
                    profile,
                    artifact_root,
                    run_id=run_id,
                    cancel_event=cancel_event,
                )
                results.append(result)
            finally:
                shutil.rmtree(repository, ignore_errors=True)
            if cancel_event.is_set():
                break
        if cancel_event.is_set():
            break
    return results
