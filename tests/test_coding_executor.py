from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest
from pydantic import ValidationError

from ephy_worker.cli import parser
from ephy_worker.coding_cli import run_coding_verify_command
from ephy_worker.coding_executor import (
    CodingExecutionError,
    CodingExecutor,
    FakePiRunner,
    _macos_write_guard,
    _stop_process,
)
from ephy_worker.coding_review import verify_source_manifest
from ephy_worker.coding_schema import CodingJob, CodingModelProfile, MockFileChange


def _run_git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run_git(repository, "init", "-q", "-b", "main")
    (repository / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repository / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run_git(repository, "add", "--", ".")
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid.example",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    return repository, _run_git(repository, "rev-parse", "HEAD")


def _profile(**updates) -> CodingModelProfile:
    values = {
        "profile_id": "mock",
        "provider": "mock",
        "model_id": "deterministic-file-writer",
        "server_type": "mock",
        "context_window": 32768,
        "reasoning_level": "none",
    }
    values.update(updates)
    return CodingModelProfile(**values)


def test_retired_server_type_is_rejected():
    with pytest.raises(ValidationError):
        _profile(server_type="ollama", provider="old-local-provider")


def _job(repository: Path, revision: str, **updates) -> CodingJob:
    values = {
        "job_id": "coding-job-1",
        "task_id": "fixture-task",
        "goal": "Set VALUE to 2.",
        "repository_path": repository,
        "base_revision": revision,
        "validation_command": [
            sys.executable,
            "-c",
            "from value import VALUE; assert VALUE == 2",
        ],
        "timeout_seconds": 10,
        "network_policy": "offline",
        "model_profile": "mock",
        "mock_changes": [MockFileChange(path="value.py", content="VALUE = 2\n")],
    }
    values.update(updates)
    return CodingJob(**values)


@pytest.mark.asyncio
async def test_mock_job_isolated_validated_and_saved(tmp_path):
    repository, revision = _repository(tmp_path)
    before_status = _run_git(repository, "status", "--porcelain")

    result, artifacts = await CodingExecutor().execute(
        _job(repository, revision), _profile(), tmp_path / "artifacts"
    )

    assert result.result.status == "succeeded"
    assert result.result.validation_passed is True
    assert result.repository.base_revision == revision
    assert result.repository.resulting_revision == revision
    assert result.changed_files == ["value.py"]
    assert result.metrics.diff_additions == 1
    assert result.metrics.diff_deletions == 1
    assert result.worktree_cleaned is True
    assert result.repository.worktree_path is None
    assert (repository / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _run_git(repository, "status", "--porcelain") == before_status
    assert "VALUE = 2" in (artifacts / "patch.diff").read_text(encoding="utf-8")
    assert "mock_agent_end" in (artifacts / "agent.log").read_text(encoding="utf-8")
    assert (artifacts / "validation.log").exists()
    saved = json.loads((artifacts / "result.json").read_text(encoding="utf-8"))
    assert saved["result"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_working_tree_snapshot_includes_dirty_and_selected_new_files(tmp_path, capsys):
    repository, revision = _repository(tmp_path)
    (repository / "value.py").write_text("VALUE = 3\n", encoding="utf-8")
    (repository / ".gitignore").write_text("__pycache__/\nignored.txt\n", encoding="utf-8")
    (repository / "helper.py").write_text("HELPER = True\n", encoding="utf-8")
    (repository / "ignored.txt").write_text("private\n", encoding="utf-8")
    before_status = _run_git(repository, "status", "--porcelain")
    job = _job(
        repository,
        revision,
        source_state="working-tree",
        snapshot_untracked_paths=["helper.py"],
        validation_command=[
            sys.executable,
            "-c",
            "from value import VALUE; from helper import HELPER; assert VALUE == 4 and HELPER",
        ],
        mock_changes=[MockFileChange(path="value.py", content="VALUE = 4\n")],
    )

    result, artifacts = await CodingExecutor().execute(job, _profile(), tmp_path / "artifacts")

    assert result.result.status == "succeeded"
    assert result.repository.source_revision == revision
    assert result.repository.source_state == "working-tree"
    assert result.repository.base_revision != revision
    assert result.changed_files == ["value.py"]
    assert "VALUE = 3" in (artifacts / "patch.diff").read_text(encoding="utf-8")
    assert "VALUE = 4" in (artifacts / "patch.diff").read_text(encoding="utf-8")
    manifest = json.loads((artifacts / "source-manifest.json").read_text(encoding="utf-8"))
    assert "helper.py" in {item["path"] for item in manifest["files"]}
    assert "ignored.txt" not in {item["path"] for item in manifest["files"]}
    assert _run_git(repository, "status", "--porcelain") == before_status
    assert (repository / "value.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert result.worktree_cleaned is True
    assert verify_source_manifest(artifacts, repository)["ok"] is True
    command = parser().parse_args(
        ["coding", "verify-source", str(artifacts), "--repository", str(repository)]
    )
    assert run_coding_verify_command(command) == 0
    assert '"ok": true' in capsys.readouterr().out
    (repository / "value.py").write_text("VALUE = 5\n", encoding="utf-8")
    verification = verify_source_manifest(artifacts, repository)
    assert verification["ok"] is False
    assert verification["changed_files"] == ["value.py"]


def test_working_tree_snapshot_requires_explicit_untracked_files(tmp_path):
    repository, revision = _repository(tmp_path)
    (repository / "new.py").write_text("NEW = True\n", encoding="utf-8")
    job = _job(
        repository,
        revision,
        source_state="working-tree",
        snapshot_untracked_paths=["new.py"],
    )

    plan = CodingExecutor().plan(job, _profile())

    assert plan.source_state == "working-tree"
    assert plan.snapshot_files == 3
    assert _run_git(repository, "worktree", "list", "--porcelain").count("worktree ") == 1
    with pytest.raises(CodingExecutionError) as raised:
        CodingExecutor().plan(
            job.model_copy(update={"snapshot_untracked_paths": ["missing.py"]}),
            _profile(),
        )
    assert raised.value.code == "worktree_creation_failure"


@pytest.mark.asyncio
async def test_validation_failure_is_independent_result(tmp_path):
    repository, revision = _repository(tmp_path)
    job = _job(
        repository,
        revision,
        validation_command=[sys.executable, "-c", "raise SystemExit(7)"],
    )

    result, artifacts = await CodingExecutor().execute(job, _profile(), tmp_path / "artifacts")

    assert result.result.status == "failed"
    assert result.result.validation_passed is False
    assert result.result.exit_code == 7
    assert result.result.failure.code == "validation_failure"
    assert (artifacts / "patch.diff").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_timeout_cleans_worktree_and_records_patch_files(tmp_path):
    repository, revision = _repository(tmp_path)
    executor = CodingExecutor(runner=FakePiRunner(delay_seconds=1))
    job = _job(repository, revision, timeout_seconds=0.05)

    result, _ = await executor.execute(job, _profile(), tmp_path / "artifacts")

    assert result.result.status == "failed"
    assert result.result.failure.code == "execution_timeout"
    assert result.worktree_cleaned is True
    assert _run_git(repository, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.asyncio
async def test_agent_cancellation_cleans_worktree(tmp_path):
    repository, revision = _repository(tmp_path)
    event = asyncio.Event()
    execution = asyncio.create_task(
        CodingExecutor(runner=FakePiRunner(delay_seconds=2)).execute(
            _job(repository, revision),
            _profile(),
            tmp_path / "artifacts",
            cancel_event=event,
        )
    )
    await asyncio.sleep(0.05)
    event.set()

    result, _ = await execution

    assert result.result.status == "cancelled"
    assert result.result.failure.code == "cancelled"
    assert result.worktree_cleaned is True


@pytest.mark.asyncio
async def test_validation_timeout_terminates_child(tmp_path):
    repository, revision = _repository(tmp_path)
    job = _job(
        repository,
        revision,
        validation_command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.2,
    )

    result, _ = await CodingExecutor().execute(job, _profile(), tmp_path / "artifacts")

    assert result.result.status == "failed"
    assert result.result.failure.code == "execution_timeout"
    assert result.result.failure.stage == "validation"
    assert result.worktree_cleaned is True


@pytest.mark.asyncio
async def test_missing_pi_has_specific_diagnostic(tmp_path):
    repository, revision = _repository(tmp_path)
    profile = _profile(
        profile_id="real",
        provider="openai",
        model_id="example-model",
        server_type="pi",
        pi_executable="definitely-missing-pi-executable",
    )
    job = _job(repository, revision, model_profile="real", mock_changes=[])

    result, _ = await CodingExecutor().execute(job, profile, tmp_path / "artifacts")

    assert result.result.failure.code == "pi_executable_unavailable"
    assert result.result.failure.stage == "agent"


@pytest.mark.asyncio
async def test_unreachable_model_endpoint_has_specific_diagnostic(tmp_path):
    repository, revision = _repository(tmp_path)
    profile = _profile(
        profile_id="real",
        provider="openai",
        model_id="example-model",
        server_type="pi",
        pi_executable=sys.executable,
        endpoint="http://127.0.0.1:1/v1",
    )

    result, _ = await CodingExecutor().execute(
        _job(repository, revision, model_profile="real", mock_changes=[]),
        profile,
        tmp_path / "artifacts",
    )

    assert result.result.failure.code == "model_endpoint_unavailable"


@pytest.mark.asyncio
async def test_pi_model_error_has_specific_diagnostic(tmp_path):
    repository, revision = _repository(tmp_path)
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type': 'turn_start'}), flush=True)\n"
        "print(json.dumps({'type': 'tool_execution_start'}), flush=True)\n"
        "print(json.dumps({'type': 'response', 'command': 'prompt', "
        "'success': False, 'error': 'model not found'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    profile = _profile(
        profile_id="real",
        provider="openai",
        model_id="missing-model",
        server_type="pi",
        pi_executable=str(fake_pi),
    )

    result, _ = await CodingExecutor().execute(
        _job(repository, revision, model_profile="real", mock_changes=[]),
        profile,
        tmp_path / "artifacts",
    )

    assert result.result.failure.code == "model_unavailable"
    assert result.metrics.turns == 1
    assert result.metrics.tool_calls == 1


@pytest.mark.asyncio
async def test_pi_retryable_model_error_is_cleared_by_terminal_success(tmp_path):
    repository, revision = _repository(tmp_path)
    fake_pi = tmp_path / "retrying-pi"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "sys.stdin.readline()\n"
        "pathlib.Path('value.py').write_text('VALUE = 2\\n')\n"
        "events = [\n"
        "    {'type': 'message_end', 'message': {'role': 'assistant', "
        "'stopReason': 'error', 'errorMessage': 'temporary stream error'}},\n"
        "    {'type': 'agent_end', 'willRetry': True, 'messages': ["
        "{'role': 'assistant', 'stopReason': 'error', 'errorMessage': 'temporary stream error'}]},\n"
        "    {'type': 'auto_retry_start'},\n"
        "    {'type': 'auto_retry_end'},\n"
        "    {'type': 'agent_end', 'willRetry': False, 'messages': ["
        "{'role': 'assistant', 'stopReason': 'stop'}]},\n"
        "    {'type': 'agent_settled'},\n"
        "]\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    profile = _profile(
        profile_id="real",
        provider="llama_router",
        model_id="local-model",
        server_type="llama.cpp",
        pi_executable=str(fake_pi),
    )

    result, _ = await CodingExecutor().execute(
        _job(repository, revision, model_profile="real", mock_changes=[]),
        profile,
        tmp_path / "artifacts",
    )

    assert result.result.status == "succeeded"
    assert result.result.validation_passed is True
    assert result.metrics.retries == 1


@pytest.mark.asyncio
async def test_pi_process_is_terminated_on_timeout(tmp_path, monkeypatch):
    repository, revision = _repository(tmp_path)
    pid_file = tmp_path / "pi.pid"
    fake_pi = tmp_path / "sleeping-pi"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        "pathlib.Path(os.environ['FAKE_PI_PID_FILE']).write_text(str(os.getpid()))\n"
        "sys.stdin.readline()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    monkeypatch.setenv("FAKE_PI_PID_FILE", str(pid_file))
    profile = _profile(
        profile_id="real",
        provider="openai",
        model_id="slow-model",
        server_type="pi",
        pi_executable=str(fake_pi),
    )

    result, _ = await CodingExecutor().execute(
        _job(
            repository,
            revision,
            model_profile="real",
            mock_changes=[],
            timeout_seconds=0.8,
        ),
        profile,
        tmp_path / "artifacts",
    )

    assert result.result.failure.code == "execution_timeout"
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_dry_run_resolves_revision_without_allocating_worktree(tmp_path):
    repository, revision = _repository(tmp_path)

    plan = CodingExecutor().plan(_job(repository, "HEAD"), _profile())

    assert plan.base_revision == revision
    assert plan.pi_invocation[0] == "mock-pi"
    assert plan.worktree == "<worker-owned-temporary-git-worktree>"
    assert _run_git(repository, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_macos_write_guard_is_explicit_and_fails_closed_off_mac(tmp_path, monkeypatch):
    repository, revision = _repository(tmp_path)
    job = _job(repository, revision, macos_sandbox="write-guard")
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with pytest.raises(CodingExecutionError, match="sandbox_unavailable"):
        _macos_write_guard([sys.executable, "-c", "pass"], job)
    with pytest.raises(CodingExecutionError, match="sandbox_unavailable"):
        CodingExecutor().plan(job, _profile())


def _macos_sandbox_available() -> bool:
    if platform.system() != "Darwin":
        return False
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.asyncio
async def test_macos_write_guard_blocks_pi_and_validation_writes(tmp_path):
    if not _macos_sandbox_available():
        pytest.skip("sandbox-exec cannot be nested in this test environment")
    repository, revision = _repository(tmp_path)
    protected = tmp_path / "protected"
    protected.mkdir()
    fake_pi = tmp_path / "sandboxed-pi"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "sys.stdin.readline()\n"
        f"for root in ({str(repository)!r}, {str(protected)!r}):\n"
        "    try:\n"
        "        (pathlib.Path(root) / 'pi-escaped').write_text('bad')\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        raise RuntimeError('Pi escaped write guard')\n"
        "pathlib.Path('value.py').write_text('VALUE = 2\\n')\n"
        "print(json.dumps({'type': 'agent_settled'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    validation = (
        "import pathlib; from value import VALUE; assert VALUE == 2; "
        f"roots = ({str(repository)!r}, {str(protected)!r}); "
        "[(pathlib.Path(root) / 'validation-escaped').write_text('bad') for root in roots]"
    )
    job = _job(
        repository,
        revision,
        model_profile="real",
        mock_changes=[],
        macos_sandbox="write-guard",
        macos_protected_roots=[protected],
        validation_command=[sys.executable, "-c", validation],
    )
    profile = _profile(
        profile_id="real", provider="openai", model_id="example-model", server_type="pi",
        pi_executable=str(fake_pi),
    )

    plan = CodingExecutor().plan(job, profile)
    assert plan.pi_invocation[0] == "/usr/bin/sandbox-exec"
    assert plan.validation_command[0] == "/usr/bin/sandbox-exec"
    result, _ = await CodingExecutor().execute(job, profile, tmp_path / "artifacts")

    assert result.result.failure is not None
    assert result.result.failure.code == "validation_failure"
    assert result.changed_files == ["value.py"]
    assert not (repository / "pi-escaped").exists()
    assert not (repository / "validation-escaped").exists()
    assert not (protected / "pi-escaped").exists()
    assert not (protected / "validation-escaped").exists()


def test_dry_run_rejects_invalid_revision(tmp_path):
    repository, _ = _repository(tmp_path)

    with pytest.raises(CodingExecutionError) as raised:
        CodingExecutor().plan(_job(repository, "missing-revision"), _profile())

    assert raised.value.code == "invalid_revision"


def test_ephy_worker_itself_can_be_a_dry_run_target():
    repository = Path(__file__).resolve().parents[1]
    revision = _run_git(repository, "rev-parse", "HEAD")
    job = _job(repository, revision)

    plan = CodingExecutor().plan(job, _profile())

    assert Path(plan.repository_path) == repository
    assert plan.base_revision == revision


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
async def test_stop_process_cleans_child_after_group_leader_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        os.kill(child_pid, 0)

        await _stop_process(process)

        for _ in range(40):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("child process survived its exited process-group leader")
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
async def test_pi_runner_cleans_child_after_agent_settles(tmp_path, monkeypatch):
    repository, revision = _repository(tmp_path)
    pid_file = tmp_path / "pi-child.pid"
    fake_pi = tmp_path / "pi-with-child"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, subprocess, sys\n"
        "sys.stdin.readline()\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(os.environ['FAKE_PI_CHILD_PID']).write_text(str(child.pid))\n"
        "pathlib.Path('value.py').write_text('VALUE = 2\\n')\n"
        "print(json.dumps({'type': 'agent_settled'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    monkeypatch.setenv("FAKE_PI_CHILD_PID", str(pid_file))
    profile = _profile(
        profile_id="real",
        provider="openai",
        model_id="example-model",
        server_type="pi",
        pi_executable=str(fake_pi),
    )

    result, _ = await CodingExecutor().execute(
        _job(repository, revision, model_profile="real", mock_changes=[]),
        profile,
        tmp_path / "artifacts",
    )

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        assert result.result.status == "succeeded"
        for _ in range(40):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("Pi child survived agent settlement")
    finally:
        with suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)
