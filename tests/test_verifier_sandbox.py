"""Offline controls for native verifier descriptor and command construction.

Synthetic transcripts exercise binding logic only; actual OS isolation requires
capture_capability and is never inferred from these unit tests.
"""

import copy
import json
from pathlib import Path

import pytest

from ephy_worker import verifier_sandbox as sandbox
from ephy_worker.formal_artifacts import GateFailure, digest, file_hash, write_json


@pytest.fixture(autouse=True)
def synthetic_private_home(tmp_path, monkeypatch):
    # Binding-unit fixtures use benign disposable paths. The real OS controls
    # independently require the actual Windows home to remain inaccessible.
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)


def descriptor_fixture(tmp_path):
    codex, python = tmp_path / "codex.exe", tmp_path / "python.exe"
    codex.write_bytes(b"synthetic codex")
    python.write_bytes(b"synthetic python")
    private, runtime = tmp_path / "private", tmp_path / "runtime"
    private.mkdir()
    runtime.mkdir()
    candidate, checker, temporary = private / "candidate", private / "checker" / "checker.py", private / "temp"
    candidate.mkdir()
    checker.parent.mkdir()
    checker.write_text("synthetic checker")
    temporary.mkdir()
    descriptor = {
        "schema": sandbox.SCHEMA,
        "network_policy": "windows_all_network_denied",
        "codex": {"path": str(codex), "sha256": file_hash(codex)},
        "python": {"path": str(python), "sha256": file_hash(python)},
        "acl_helper": {"path": str(python), "sha256": file_hash(python)},
        "read_roots": [str(runtime)], "denied_roots": [str(Path.home()), str(private)],
        "capability": {"path": str(tmp_path / "capability.json"), "sha256": ""},
    }
    stdout, stderr = tmp_path / "stdout.log", tmp_path / "stderr.log"
    stdout.write_text(json.dumps({"nonce": "synthetic-nonce", "controls": {
        name: True for name in sandbox.CONTROL_KEYS
    }}))
    stderr.write_text("")
    profile = sandbox.profile_override(descriptor, candidate=candidate, checker=checker, temporary=temporary)
    inner = [str(python), "-V"]
    record = {
        "schema": "ephy.native-verifier-capability.v1", "codex": descriptor["codex"],
        "python": descriptor["python"], "read_roots": descriptor["read_roots"],
        "acl_helper": descriptor["acl_helper"], "network_policy": descriptor["network_policy"],
        "denied_roots": descriptor["denied_roots"], "probe_sha256": digest(sandbox.PROBE.encode()),
        "acl_script_sha256": digest(sandbox.ACL_SCRIPT.encode()),
        "nonce": "synthetic-nonce", "exit_code": 0, "candidate": str(candidate),
        "profile": profile, "profile_sha256": digest(profile.encode()), "inner_argv": inner,
        "argv": sandbox.sandbox_argv(descriptor, candidate, profile, inner),
        "stdout": {"path": str(stdout), "sha256": file_hash(stdout)},
        "stderr": {"path": str(stderr), "sha256": file_hash(stderr)},
        "protected_before": {"synthetic-protected": "a" * 64},
        "protected_after": {"synthetic-protected": "a" * 64},
    }
    write_json(Path(descriptor["capability"]["path"]), record)
    descriptor["capability"]["sha256"] = file_hash(Path(descriptor["capability"]["path"]))
    return descriptor


def test_native_descriptor_binds_measured_controls_and_runtime(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    assert sandbox.validate_descriptor(descriptor)["exit_code"] == 0
    assert sandbox.identity(descriptor)["descriptor_sha256"]
    assert sandbox.identity(descriptor)["codex"] == descriptor["codex"]


@pytest.mark.parametrize("mutation", ["codex", "python", "capability", "stdout", "stderr"])
def test_changed_isolation_runtime_or_transcript_rejected(tmp_path, mutation):
    descriptor = descriptor_fixture(tmp_path)
    if mutation in ("stdout", "stderr"):
        record = sandbox.validate_descriptor(descriptor)
        target = Path(record[mutation]["path"])
    else:
        target = Path(descriptor[mutation]["path"])
    target.write_bytes(target.read_bytes() + b"changed")
    with pytest.raises(GateFailure, match="changed"):
        sandbox.validate_descriptor(descriptor)


@pytest.mark.parametrize("control", sorted(sandbox.CONTROL_KEYS))
def test_each_missing_native_control_prevents_admission(tmp_path, control):
    descriptor = descriptor_fixture(tmp_path)
    record = sandbox.validate_descriptor(descriptor)
    stdout = Path(record["stdout"]["path"])
    output = json.loads(stdout.read_text())
    output["controls"][control] = False
    stdout.write_text(json.dumps(output))
    record["stdout"]["sha256"] = file_hash(stdout)
    write_json(Path(descriptor["capability"]["path"]), record, exclusive=False)
    descriptor["capability"]["sha256"] = file_hash(Path(descriptor["capability"]["path"]))
    with pytest.raises(GateFailure, match="controls failed"):
        sandbox.validate_descriptor(descriptor)


@pytest.mark.parametrize("field", ["exit_code", "profile_sha256", "probe_sha256", "acl_script_sha256", "argv", "nonce"])
def test_capability_command_and_probe_binding_is_required(tmp_path, field):
    descriptor = descriptor_fixture(tmp_path)
    record = sandbox.validate_descriptor(descriptor)
    record[field] = 1 if field == "exit_code" else [] if field == "argv" else "invalid"
    write_json(Path(descriptor["capability"]["path"]), record, exclusive=False)
    descriptor["capability"]["sha256"] = file_hash(Path(descriptor["capability"]["path"]))
    with pytest.raises(GateFailure):
        sandbox.validate_descriptor(descriptor)


def test_descriptor_cannot_reopen_the_private_home(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    descriptor["read_roots"].append(str(Path.home()))
    with pytest.raises(GateFailure, match="reopens"):
        sandbox.validate_descriptor(descriptor)


def test_descriptor_must_deny_private_home(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    descriptor["denied_roots"] = [str(unrelated)]
    with pytest.raises(GateFailure, match="private Windows account"):
        sandbox.validate_descriptor(descriptor)


def test_profile_is_one_command_override_with_network_disabled(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    candidate, checker, temporary = tmp_path / "candidate", tmp_path / "checker" / "check.py", tmp_path / "temp"
    candidate.mkdir()
    checker.parent.mkdir()
    checker.write_text("checker")
    temporary.mkdir()
    override = sandbox.profile_override(descriptor, candidate=candidate, checker=checker, temporary=temporary)
    assert override.startswith("permissions.ephy_verifier={filesystem={")
    assert 'network={enabled=false}' in override
    assert override.count('="write"') == 1
    argv = sandbox.sandbox_argv(descriptor, candidate, override, ["python", "-V"])
    assert argv[-3:] == ["--", "python", "-V"]
    assert argv.count("--include-managed-config") == 1
    assert "exec" not in argv


def test_default_descriptor_cannot_be_admitted_by_a_pass_boolean(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    bad = copy.deepcopy(descriptor)
    bad["capability"] = {"passed": True}
    with pytest.raises(GateFailure, match="artifact pin"):
        sandbox.validate_descriptor(bad)


def test_timeout_or_refused_network_result_is_not_a_pass():
    assert "getattr(exc,'winerror',None)==10013" in sandbox.PROBE
    assert "except OSError: return True" not in sandbox.PROBE
    for key in ("loopback_denied", "child_loopback_denied", "child_network_denied"):
        assert key in sandbox.CONTROL_KEYS


def test_explicit_limited_policy_records_loopback_without_claiming_denial(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    record = sandbox.validate_descriptor(descriptor)
    output_path = Path(record["stdout"]["path"])
    output = json.loads(output_path.read_text())
    output["controls"].update(loopback_denied=False, child_loopback_denied=False)
    output_path.write_text(json.dumps(output))
    record["stdout"]["sha256"] = file_hash(output_path)
    descriptor["network_policy"] = record["network_policy"] = "windows_external_network_denied"
    write_json(Path(descriptor["capability"]["path"]), record, exclusive=False)
    descriptor["capability"]["sha256"] = file_hash(Path(descriptor["capability"]["path"]))
    assert sandbox.validate_descriptor(descriptor)["network_policy"] == "windows_external_network_denied"
    assert "not guaranteed" in sandbox.identity(descriptor)["loopback_isolation"]


def test_network_policy_has_no_implicit_limited_fallback(tmp_path):
    descriptor = descriptor_fixture(tmp_path)
    del descriptor["network_policy"]
    with pytest.raises(GateFailure):
        sandbox.validate_descriptor(descriptor)


def test_metadata_ancestor_scope_excludes_unrelated_and_public_roots(tmp_path):
    private = tmp_path / "private"
    candidate = private / "jobs" / "candidate"
    candidate.mkdir(parents=True)
    runtime = tmp_path / "public-runtime"
    runtime.mkdir()
    descriptor = {"denied_roots": [str(private)]}
    assert sandbox.metadata_ancestors(descriptor, [candidate, runtime]) == [private, private / "jobs"]
