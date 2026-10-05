"""Real kernel locking and unchanged production pin checks; no model service."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ephy_worker import verifier_triage_consumer as consumer
from ephy_worker.formal_artifacts import CONTROL_PATHS, GateFailure, digest, encode, file_hash, write_json
from ephy_worker.formal_runtime import (
    FormalRunner,
    exclusive_lock,
    runtime_artifact_paths,
    validate_resource_lock_pins,
)


def job_fixture(tmp_path, *, pin_lock=False):
    lock = tmp_path / "existing-resource.lock"
    lock.write_bytes(b"0")
    artifact = tmp_path / "artifact.txt"
    artifact.write_bytes(b"frozen")
    governance = Path(__file__).resolve().parents[1]
    runtime = {"resource_lock": str(lock), "governance_root": str(governance)}
    pins = {str(artifact): file_hash(artifact)}
    if pin_lock:
        pins[str(lock)] = file_hash(lock)
    contract = {"runtime_hashes": pins, "checker": str(artifact), "checker_sha256": file_hash(artifact),
                "checker_controls": str(artifact), "checker_controls_sha256": file_hash(artifact), "timeout_seconds": 60}
    controls = {name: file_hash(governance / relative) for name, relative in CONTROL_PATHS.items()}
    job = {"contract": contract, "runtime": runtime, "controls": controls, "model_identities": {},
           "verifier_identity": {}, "baseRevision": "fixed", "repoRoot": str(tmp_path),
           "worktreePath": str(tmp_path / "candidate"), "jobDir": str(tmp_path)}
    path = tmp_path / "job.json"
    write_json(path, job)
    return path, job, lock, artifact


def test_windows_mandatory_lock_reproduces_owner_second_handle_failure(tmp_path):
    lock = tmp_path / "lock"
    lock.write_bytes(b"0")
    expected = file_hash(lock)
    with exclusive_lock(lock):
        if os.name == "nt":
            with pytest.raises(PermissionError):
                file_hash(lock)
        else:
            assert file_hash(lock) == expected
    assert file_hash(lock) == expected


def test_existing_lock_is_only_runtime_path_not_content(tmp_path):
    path, job, lock, artifact = job_fixture(tmp_path)
    paths = runtime_artifact_paths({**job["runtime"], "provider": str(artifact)})
    assert paths == [artifact]
    runner = FormalRunner(path)
    with exclusive_lock(lock):
        runner.intact()
        assert runner.contract_sha == digest(encode(job["contract"]))


def test_formal_runner_rejects_lock_pin_before_acquisition(tmp_path):
    path, _, lock, _ = job_fixture(tmp_path, pin_lock=True)
    with pytest.raises(GateFailure, match="coordination path"):
        FormalRunner(path)
    with exclusive_lock(lock):
        pass


def test_existing_kernel_lock_remains_exclusive_between_processes(tmp_path):
    path, _, lock, _ = job_fixture(tmp_path)
    runner = FormalRunner(path)
    code = ("from pathlib import Path\nfrom ephy_worker.formal_runtime import exclusive_lock\n"
            f"with exclusive_lock(Path({str(lock)!r})): print('unexpected')\n")
    with exclusive_lock(lock):
        runner.intact()
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10, check=False)
        assert child.returncode != 0 and b"owns the resource lock" in child.stderr
    with exclusive_lock(lock):
        pass


def test_changed_runtime_lock_path_is_rejected_by_unchanged_identity_gate(tmp_path):
    path, job, lock, _ = job_fixture(tmp_path)
    runner = FormalRunner(path)
    job["runtime"]["resource_lock"] = str(tmp_path / "replacement.lock")
    path.write_bytes(encode(job))
    with exclusive_lock(lock), pytest.raises(GateFailure, match="Frozen Job identity changed"):
        runner.intact()


def test_nonlock_content_pin_still_rejects_modified_file_while_locked(tmp_path):
    path, _, lock, artifact = job_fixture(tmp_path)
    runner = FormalRunner(path)
    artifact.write_bytes(b"changed")
    with exclusive_lock(lock), pytest.raises(GateFailure, match="Runtime/config changed"):
        runner.intact()


def test_resource_lock_alias_cannot_be_used_as_content_pin(tmp_path):
    _, _, lock, _ = job_fixture(tmp_path)
    alias = tmp_path / "alias.lock"
    os.link(lock, alias)
    with pytest.raises(GateFailure, match="coordination path"):
        validate_resource_lock_pins(lock, [str(alias)])
    with pytest.raises(GateFailure, match="coordination path"):
        runtime_artifact_paths({"resource_lock": str(lock), "configuration": str(alias)})


def test_consumer_rejects_mixed_lock_pin_before_any_read_or_launch(tmp_path, monkeypatch):
    _, _, lock, _ = job_fixture(tmp_path)
    contract = {"resource_lock": str(lock), "pins": {str(lock): file_hash(lock)}}
    monkeypatch.setattr(consumer, "run_session", lambda *_: pytest.fail("Session launched"))
    with exclusive_lock(lock), pytest.raises(GateFailure, match="coordination path"):
        consumer.intact(contract)


def test_consumer_other_pins_are_checked_under_existing_lock(tmp_path):
    _, _, lock, artifact = job_fixture(tmp_path)
    from importlib.metadata import version

    contract = {"resource_lock": str(lock), "pins": {str(artifact): file_hash(artifact)},
                "dependency_versions": {name: version(name) for name in ("httpx", "psutil", "jsonschema")}}
    with exclusive_lock(lock):
        consumer.intact(contract)
        artifact.write_bytes(b"changed")
        with pytest.raises(ValueError, match="Frozen artifact changed"):
            consumer.intact(contract)
