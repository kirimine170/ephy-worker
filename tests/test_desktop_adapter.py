"""Offline contract tests; these never contact an inference service."""

import asyncio
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ephy_worker.coding_executor import CodingExecutionError, PiRpcRunner
from ephy_worker.coding_schema import AgentOutcome
from ephy_worker.desktop.adapter import DesktopAdapter, NavigationPiRunner
from ephy_worker.desktop.config import DesktopConfig


def _directory_link(link: Path, target: Path):
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    node = shutil.which("node")
    assert node, "Node.js is required to exercise actual Windows junctions"
    subprocess.run(
        [node, "-e", "require('node:fs').symlinkSync(process.argv[1], process.argv[2], 'junction')",
         str(target), str(link)],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def _assert_pi_not_started(worktree, monkeypatch, *, detail=None):
    run = AsyncMock(return_value=AgentOutcome(exit_code=0))
    monkeypatch.setattr(PiRpcRunner, "run", run)
    with pytest.raises(CodingExecutionError, match="sandbox_unavailable") as raised:
        await NavigationPiRunner().run(None, None, worktree, asyncio.Event())
    if detail:
        assert detail in raised.value.detail
    run.assert_not_awaited()


@pytest.mark.parametrize("endpoint", [
    "https://example.com/v1", "http://127.0.0.1.evil.test/v1", "http://user:pass@127.0.0.1/v1",
    "http://127.0.0.1/v1?key=value", "http://[::1]/v1#secret", "file:///tmp/model", "http://0.0.0.0/v1",
])
def test_config_rejects_nonlocal_or_credential_endpoints(tmp_path, endpoint):
    with pytest.raises(ValueError, match="loopback"):
        DesktopConfig(tmp_path, endpoint=endpoint)


def test_config_file_is_strict_and_paths_resolve_relative(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"repository_path": "repository", "artifact_root": "runs"}), encoding="utf-8")
    config = DesktopConfig.from_file(path)
    assert config.repository_path == tmp_path / "repository"
    assert config.artifact_root == tmp_path / "runs"
    path.write_text(json.dumps({"repository_path": ".", "pi_args": ["--tools", "bash"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        DesktopConfig.from_file(path)


@pytest.mark.parametrize("field,value", [
    ("base_revision", "--all"), ("context_window", True), ("context_window", 100),
    ("provider", "local\nother"), ("model_id", "--option"), ("karte_binary", "only-one.exe"),
])
def test_config_rejects_invalid_selection(tmp_path, field, value):
    with pytest.raises(ValueError):
        DesktopConfig(tmp_path, **{field: value})


def test_models_use_only_pinned_registration_and_no_credential_files(tmp_path):
    config = DesktopConfig(tmp_path / "source")
    generated = config.models_document()["providers"]["strata-local"]
    assert generated["api"] == "openai-completions"
    assert generated["apiKey"] == "local-no-auth"
    assert generated["compat"]["thinkingFormat"] == "qwen-chat-template"
    for credential in ("local-static-key", "!execute", "API_SECRET", "$DESKTOP_TEST_KEY",
                       "${DESKTOP_TEST_KEY}", "prefix-${DESKTOP_TEST_KEY}"):
        path = tmp_path / "models.json"
        path.write_text(json.dumps({"apiKey": credential}), encoding="utf-8")
        with pytest.raises(ValueError, match="custom models_file"):
            DesktopConfig(tmp_path / "source", models_file=path)
    for provider in ("local", "arbitrary", "mock"):
        with pytest.raises(ValueError, match="strata-local"):
            DesktopConfig(tmp_path, provider=provider)


@pytest.mark.asyncio
async def test_adapter_uses_existing_worker_fixed_argv_and_fresh_private_runtime(tmp_path, monkeypatch):
    config = DesktopConfig(tmp_path / "source", artifact_root=tmp_path / "runs")
    captures = []
    expected_result = SimpleNamespace(result=SimpleNamespace(failure=None))

    async def execute(executor, job, profile, artifact_root, **kwargs):
        runner = executor.runner
        runtime = Path(runner.child_environment["PI_CODING_AGENT_DIR"])
        captures.append(runtime)
        assert runtime.is_dir()
        assert not runtime.is_relative_to(config.repository_path)
        assert not (runtime / "models.json").exists()
        settings = json.loads((runtime / "settings.json").read_text(encoding="utf-8"))
        assert settings["retry"]["enabled"] is False
        assert settings["retry"]["provider"]["maxRetries"] == 0
        assert settings["compaction"]["enabled"] is False
        assert settings["cacheWarming"] == "off"
        assert runner.child_environment["DUAL_GOVERNANCE_ROLE"] == "planner"
        assert runner.child_environment["DUAL_JOB_RUNNER"] == ""
        assert isinstance(runner, NavigationPiRunner)
        assert runner.required_ready_event == "ephy_navigation_guard_ready"
        assert runner.child_environment["NODE_OPTIONS"] == ""
        assert runner.child_environment["TMP"] == str(runtime.parent)
        assert runner.child_environment["TEMP"] == str(runtime.parent)
        assert Path(runner.child_environment["TMP"]).is_absolute()
        assert job.source_state == "revision"
        assert job.task_id == "repository-navigation"
        assert job.validation_command == ["git", "diff", "--check"]
        assert job.network_policy == "offline"
        args = runner.invocation(profile)
        for flag in ("--offline", "--no-extensions", "--no-skills", "--no-context-files", "--no-approve"):
            assert flag in args
        assert args[args.index("--tools") + 1] == "read,governance_ack"
        extensions = [Path(args[i + 1]).name for i, item in enumerate(args) if item == "-e"]
        assert extensions == ["strata-provider.ts", "formal-stage-guard.ts",
                              "navigation_guard.ts", "governance-gate.ts"]
        assert job.timeout_seconds == 240
        assert "bash" not in args
        artifact_root.mkdir(exist_ok=True)
        return expected_result, artifact_root

    monkeypatch.setattr("ephy_worker.desktop.adapter.CodingExecutor.execute", execute)
    adapter = DesktopAdapter()
    for index in range(2):
        result, _ = await adapter.execute(
            config, "Inspect README", f"test-{index}", asyncio.Event(), lambda e: None, lambda p: None,
        )
        assert result is expected_result
        assert not captures[-1].exists()
    assert captures[0] != captures[1]


@pytest.mark.asyncio
async def test_navigation_refuses_worktree_changes(tmp_path, monkeypatch):
    async def run(*args):
        return AgentOutcome(exit_code=0)

    monkeypatch.setattr(PiRpcRunner, "run", run)
    monkeypatch.setattr("ephy_worker.desktop.adapter.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=" M x"))
    with pytest.raises(CodingExecutionError, match="validation_failure"):
        await NavigationPiRunner().run(None, None, tmp_path, asyncio.Event())


def test_guard_violation_fails_the_live_runner():
    runner = NavigationPiRunner()
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure"):
        runner.observer({"type": "ephy_navigation_guard_violation", "version": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ephy_navigation_guard_violation", "ephy_navigation_guard_failed"])
@pytest.mark.parametrize("reason_code", ["path_normalized_space", "tool_path_rewrite", "windows_attribute_check",
                                        None, "unknown_code", "PRIVATE_CODE /private/fixture.txt", ["path_escape"],
                                        {"secret": "PRIVATE_CODE"}, 42])
async def test_guard_reason_code_is_allowlisted_before_log_capture(tmp_path, monkeypatch, kind, reason_code):
    from ephy_worker.desktop.adapter import NAVIGATION_REASON_CODES

    original = {"type": kind, "version": 1, "reason_code": reason_code, "existing_metadata": "preserved"}
    event = dict(original)

    async def failed_run(runner, *args):
        runner.observer(event)

    monkeypatch.setattr(PiRpcRunner, "run", failed_run)
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure") as raised:
        await NavigationPiRunner().run(None, None, tmp_path, asyncio.Event())
    permitted = isinstance(reason_code, str) and reason_code in NAVIGATION_REASON_CODES
    expected = dict(original)
    if not permitted:
        expected.pop("reason_code")
    assert [json.loads(line) for line in raised.value.log.splitlines()] == [expected]
    assert raised.value.detail == "navigation guard rejected tool access" + (
        f" ({reason_code})" if permitted else ""
    )
    assert event == original
    assert "PRIVATE_CODE" not in raised.value.log and "PRIVATE_CODE" not in raised.value.detail


def test_reason_code_sanitizing_does_not_change_unrelated_events():
    event = {"type": "unrelated_fixture", "reason_code": {"fixture": "unchanged"}, "version": 1}
    observed = []
    runner = NavigationPiRunner(observer=observed.append)
    runner.observer(event)
    assert observed == [event]
    assert json.loads(runner._captured_rpc_log()) == event


@pytest.mark.asyncio
async def test_startup_allows_regular_project_data_without_migrations(tmp_path, monkeypatch):
    project = tmp_path / ".pi"
    (project / "prompts").mkdir(parents=True)
    (project / "prompts" / "inspect.md").write_text("synthetic prompt", encoding="utf-8")
    (project / "extensions").mkdir()
    (project / "extensions" / "example.ts").write_text("// synthetic fixture", encoding="utf-8")
    (project / "settings.json").write_text("{}\n", encoding="utf-8")
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    run = AsyncMock(return_value=AgentOutcome(exit_code=0))
    monkeypatch.setattr(PiRpcRunner, "run", run)
    monkeypatch.setattr("ephy_worker.desktop.adapter.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=""))
    outcome = await NavigationPiRunner().run(None, None, tmp_path, asyncio.Event())
    assert outcome.exit_code == 0
    run.assert_awaited_once()
    assert before == {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}


@pytest.mark.asyncio
@pytest.mark.parametrize("commands_is_directory", [True, False])
async def test_startup_blocks_legacy_commands_without_migrating(tmp_path, monkeypatch, commands_is_directory):
    project = tmp_path / ".pi"
    project.mkdir()
    commands = project / "commands"
    if commands_is_directory:
        commands.mkdir()
        marker = commands / "fixture.md"
    else:
        marker = commands
    marker.write_text("synthetic legacy command", encoding="utf-8")
    await _assert_pi_not_started(tmp_path, monkeypatch, detail=".pi/commands")
    assert marker.read_text(encoding="utf-8") == "synthetic legacy command"
    assert not (project / "prompts").exists()


@pytest.mark.asyncio
async def test_startup_rejects_project_file(tmp_path, monkeypatch):
    project = tmp_path / ".pi"
    project.write_text("synthetic project file", encoding="utf-8")
    await _assert_pi_not_started(tmp_path, monkeypatch, detail="normal directory")
    assert project.read_text(encoding="utf-8") == "synthetic project file"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [".pi", ".pi/commands", ".pi/extensions"])
async def test_startup_rejects_project_junctions_without_touching_outside(tmp_path, monkeypatch, location):
    worktree, outside = tmp_path / "worktree", tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    (outside / "commands").mkdir()
    marker = outside / "commands" / "fixture.md"
    marker.write_text("synthetic outside marker", encoding="utf-8")
    link = worktree / location
    link.parent.mkdir(parents=True, exist_ok=True)
    _directory_link(link, outside)
    try:
        await _assert_pi_not_started(worktree, monkeypatch, detail="link or reparse")
        assert marker.read_text(encoding="utf-8") == "synthetic outside marker"
        assert not (outside / "prompts").exists()
        assert sorted(path.name for path in outside.iterdir()) == ["commands"]
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove this fixture junction itself, never its target.
        else:
            link.unlink()


@pytest.mark.asyncio
async def test_startup_rejects_broken_project_link(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    missing = tmp_path / "missing-target"
    link = worktree / ".pi"
    _directory_link(link, missing)
    try:
        assert not missing.exists()
        assert link.lstat()
        await _assert_pi_not_started(worktree, monkeypatch, detail="link or reparse")
        assert not missing.exists()
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.asyncio
async def test_startup_rejects_symlink_metadata_without_following(tmp_path, monkeypatch):
    original = Path.lstat

    def symlink_metadata(path):
        if path == tmp_path / ".pi":
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0, st_nlink=1)
        return original(path)

    monkeypatch.setattr(Path, "lstat", symlink_metadata)
    await _assert_pi_not_started(tmp_path, monkeypatch, detail="link or reparse")


@pytest.mark.asyncio
async def test_startup_rejects_hardlinked_project_file(tmp_path, monkeypatch):
    worktree, outside = tmp_path / "worktree", tmp_path / "outside.txt"
    (worktree / ".pi").mkdir(parents=True)
    outside.write_text("synthetic outside marker", encoding="utf-8")
    os.link(outside, worktree / ".pi" / "settings.json")
    await _assert_pi_not_started(worktree, monkeypatch, detail="hard link")
    assert outside.read_text(encoding="utf-8") == "synthetic outside marker"


@pytest.mark.asyncio
async def test_startup_rejects_ancestor_junction(tmp_path, monkeypatch):
    outside, alias = tmp_path / "outside", tmp_path / "alias"
    worktree = outside / "worktree"
    worktree.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("synthetic outside marker", encoding="utf-8")
    _directory_link(alias, outside)
    try:
        await _assert_pi_not_started(alias / "worktree", monkeypatch, detail="link or reparse")
        assert marker.read_text(encoding="utf-8") == "synthetic outside marker"
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


@pytest.mark.asyncio
async def test_startup_metadata_error_is_not_treated_as_absence(tmp_path, monkeypatch):
    original = Path.lstat

    def deny(path):
        if path == tmp_path / ".pi":
            raise PermissionError("synthetic denied metadata")
        return original(path)

    monkeypatch.setattr(Path, "lstat", deny)
    await _assert_pi_not_started(tmp_path, monkeypatch, detail="cannot be inspected")


@pytest.mark.asyncio
async def test_startup_scan_errors_and_entry_limit_fail_closed(tmp_path, monkeypatch):
    project = tmp_path / ".pi"
    project.mkdir()
    for index in range(3):
        (project / f"{index}.md").write_text("synthetic fixture", encoding="utf-8")
    monkeypatch.setattr("ephy_worker.desktop.adapter.MAX_PI_STARTUP_ENTRIES", 2)
    await _assert_pi_not_started(tmp_path, monkeypatch, detail="preflight limit")
    monkeypatch.setattr("ephy_worker.desktop.adapter.MAX_PI_STARTUP_ENTRIES", 10_000)
    original = os.scandir

    def deny(path):
        if path == project:
            raise PermissionError("synthetic denied directory")
        return original(path)

    monkeypatch.setattr("ephy_worker.desktop.adapter.os.scandir", deny)
    await _assert_pi_not_started(tmp_path, monkeypatch, detail="cannot be scanned")


@pytest.mark.asyncio
async def test_startup_rejects_unicode_path_normalization_before_pi(tmp_path, monkeypatch):
    root = tmp_path / "work\u00a0tree"
    root.mkdir()
    await _assert_pi_not_started(root, monkeypatch, detail="normalized Unicode spaces")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["navigation", "governance"])
async def test_observer_failure_retains_preceding_and_rejected_rpc(tmp_path, monkeypatch, failure_source):
    accepted = {"type": "ephy_desktop_governance_envelope", "nonce": "synthetic-nonce"}
    rejected = {"type": "ephy_navigation_guard_violation", "version": 1}
    governance = None
    if failure_source == "governance":
        rejected = {"type": "message_end", "message": {"role": "assistant", "content": []}}

        def observe(event):
            if event == rejected:
                raise CodingExecutionError("pi_rpc_failure", "agent", "synthetic acknowledgement rejected")

        governance = SimpleNamespace(ack_verified=False, observe=observe, prepare=lambda path: None,
                                     check_hashes=lambda: None)

    async def failed_run(runner, *args):
        runner.observer(accepted)
        runner.observer(rejected)
        pytest.fail("Rejected observer event must terminate the runner")

    monkeypatch.setattr(PiRpcRunner, "run", failed_run)
    runner = NavigationPiRunner(governance=governance)
    with pytest.raises(CodingExecutionError, match="pi_rpc_failure") as raised:
        await runner.run(None, None, tmp_path, asyncio.Event())
    assert raised.value.stage == "agent"
    assert [json.loads(line) for line in raised.value.log.splitlines()] == [accepted, rejected]


@pytest.mark.asyncio
async def test_existing_error_log_is_not_replaced_by_desktop_capture(tmp_path, monkeypatch):
    original = CodingExecutionError("pi_rpc_failure", "agent", "synthetic failure", log="original trace\n")

    async def failed_run(runner, *args):
        runner.observer({"type": "turn_start"})
        raise original

    monkeypatch.setattr(PiRpcRunner, "run", failed_run)
    with pytest.raises(CodingExecutionError) as raised:
        await NavigationPiRunner().run(None, None, tmp_path, asyncio.Event())
    assert raised.value is original
    assert raised.value.log == "original trace\n"


@pytest.mark.parametrize("mode", ["bytes", "count", "oversized"])
def test_failure_rpc_capture_is_bounded_complete_utf8_json_lines(mode):
    from ephy_worker.desktop.adapter import MAX_RPC_LOG_BYTES

    runner = NavigationPiRunner()
    if mode == "bytes":
        for index in range(30):
            runner.observer({"type": "fixture", "index": index, "text": "日本語" * 12000})
    elif mode == "count":
        for index in range(2100):
            runner.observer({"type": "fixture", "index": index})
    else:
        runner.observer({"type": "fixture", "text": "x" * MAX_RPC_LOG_BYTES})
    runner.observer({"type": "last_fixture_event"})
    captured = runner._captured_rpc_log()
    assert len(captured.encode("utf-8")) <= MAX_RPC_LOG_BYTES
    assert captured.endswith("\n")
    events = [json.loads(line) for line in captured.splitlines()]
    assert events[0] == {"type": "desktop_rpc_log_truncated", "reason": "bounded_capture"}
    assert events[-1] == {"type": "last_fixture_event"}
    assert len(events) <= 2049


@pytest.fixture
def interrupted_desktop(tmp_path, monkeypatch):
    from ephy_worker.coding_executor import _run_agent_with_limits
    from ephy_worker.desktop.adapter import _fixture

    config = DesktopConfig(_fixture(tmp_path), artifact_root=tmp_path / "runs")
    started, stopped = asyncio.Event(), asyncio.Event()
    events = [{"type": "turn_start"}, {"type": "fixture", "text": "保存する日本語"}]

    async def waiting_runner(runner, *args):
        for event in events:
            runner.observer(event)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def short_test_limit(runner, job, profile, worktree, cancel_event, timeout):
        assert job.timeout_seconds == 240
        assert 0 < timeout <= 240
        return await _run_agent_with_limits(runner, job, profile, worktree, cancel_event, 0.05)

    monkeypatch.setattr(PiRpcRunner, "run", waiting_runner)
    monkeypatch.setattr("ephy_worker.coding_executor._run_agent_with_limits", short_test_limit)
    return SimpleNamespace(config=config, started=started, stopped=stopped, events=events)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["timeout", "cancel_event", "outer_cancel"])
async def test_interrupted_executor_retains_rpc_log_without_changing_failure(interrupted_desktop, disposition):
    fixture = interrupted_desktop
    cancel_event = asyncio.Event()
    run = asyncio.create_task(DesktopAdapter().execute(
        fixture.config, "Inspect the synthetic fixture", "interrupted-run", cancel_event,
        lambda event: None, lambda pid: None,
    ))
    await asyncio.wait_for(fixture.started.wait(), 5)
    if disposition == "cancel_event":
        cancel_event.set()
    elif disposition == "outer_cancel":
        run.cancel()
    result, directory = await asyncio.wait_for(run, 5)

    expected_code = "execution_timeout" if disposition == "timeout" else "cancelled"
    assert result.result.failure.code == expected_code
    assert result.result.status == ("failed" if disposition == "timeout" else "cancelled")
    assert result.result.failure.stage == ("worker" if disposition == "outer_cancel" else "agent")
    assert fixture.stopped.is_set()
    assert result.worktree_cleaned is True
    assert result.repository.worktree_path is None
    assert result.changed_files == []
    assert result.metrics.input_tokens is None and result.metrics.output_tokens is None
    assert result.metrics.turns is None and result.metrics.tool_calls is None
    assert json.loads((directory / "result.json").read_text(encoding="utf-8")) == result.model_dump(mode="json")
    assert [json.loads(line) for line in (directory / "agent.log").read_text(encoding="utf-8").splitlines()] == (
        fixture.events
    )


@pytest.mark.asyncio
async def test_timeout_log_persistence_preserves_bounded_utf8_jsonl(interrupted_desktop):
    from ephy_worker.desktop.adapter import MAX_RPC_LOG_BYTES

    fixture = interrupted_desktop
    fixture.events[:] = [
        {"type": "fixture", "index": index, "text": "日本語" * 3000} for index in range(100)
    ]
    result, directory = await DesktopAdapter().execute(
        fixture.config, "Inspect the synthetic fixture", "bounded-run", asyncio.Event(),
        lambda event: None, lambda pid: None,
    )
    assert result.result.failure.code == "execution_timeout"
    data = (directory / "agent.log").read_bytes()
    assert 0 < len(data) <= MAX_RPC_LOG_BYTES
    assert data.endswith(b"\n")
    events = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    assert events[0] == {"type": "desktop_rpc_log_truncated", "reason": "bounded_capture"}
    assert events[-1] == fixture.events[-1]
    assert len(events) <= 2049


@pytest.mark.asyncio
@pytest.mark.parametrize("code,original_log", [
    ("execution_timeout", "existing timeout trace\n"),
    ("cancelled", "existing cancellation trace\n"),
    ("internal_worker_failure", ""),
])
async def test_interrupted_log_preserves_existing_or_ineligible_result(
    interrupted_desktop, monkeypatch, code, original_log,
):
    fixture = interrupted_desktop

    async def failed_runner(runner, *args):
        runner.observer(fixture.events[0])
        if code == "internal_worker_failure":
            raise RuntimeError("synthetic non-cancellation failure")
        raise CodingExecutionError(code, "agent", "synthetic failure", log=original_log)

    monkeypatch.setattr(PiRpcRunner, "run", failed_runner)
    result, directory = await DesktopAdapter().execute(
        fixture.config, "Inspect the synthetic fixture", "preserved-run", asyncio.Event(),
        lambda event: None, lambda pid: None,
    )
    assert result.result.failure.code == code
    assert (directory / "agent.log").read_text(encoding="utf-8") == original_log


@pytest.mark.asyncio
async def test_timeout_log_persistence_error_remains_visible(interrupted_desktop, monkeypatch):
    fixture = interrupted_desktop
    attempted = []

    def denied_write(path, text):
        attempted.append(path)
        assert text and path.stat().st_size == 0
        raise PermissionError("synthetic log write denied")

    monkeypatch.setattr("ephy_worker.desktop.adapter.atomic_write", denied_write)
    with pytest.raises(PermissionError, match="synthetic log write denied"):
        await DesktopAdapter().execute(
            fixture.config, "Inspect the synthetic fixture", "denied-log-run", asyncio.Event(),
            lambda event: None, lambda pid: None,
        )
    assert fixture.stopped.is_set()
    assert attempted == [fixture.config.artifact_root / "denied-log-run" / "agent.log"]
    assert attempted[0].stat().st_size == 0
    saved = json.loads(attempted[0].with_name("result.json").read_text(encoding="utf-8"))
    assert saved["result"]["status"] == "failed"
    assert saved["result"]["failure"]["code"] == "execution_timeout"
    assert saved["worktree_cleaned"] is True
