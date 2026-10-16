"""Synthetic offline packet controls; no Pi, live service, or trial artifacts."""

import copy
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import test_formal_runtime

from ephy_worker.formal_artifacts import (
    CONTROL_PATHS,
    GateFailure,
    digest,
    encode,
    file_hash,
    read_json,
)
from ephy_worker.formal_runtime import (
    DELIVERED_DOCUMENTS,
    MAX_FILE_BYTES,
    MAX_PACKET_BYTES,
    build_packet,
    git,
    snapshot,
    validate_contract,
    validate_selection,
)

planner_prompt_case = test_formal_runtime.planner_prompt_case


@pytest.fixture(autouse=True)
def no_communications(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline context control attempted external communication")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr("httpx.Client.send", blocked)
    monkeypatch.setattr("httpx.AsyncClient.send", blocked)


def selection(root, revision, extra=()):
    return {
        "version": 1, "source_revision": revision, "max_packet_bytes": MAX_PACKET_BYTES,
        "files": [
            {"path": path, "sha256": file_hash(root / path), "bytes": (root / path).stat().st_size,
             "category": category}
            for path, category in [("AGENTS.md", "additional_required"),
                                   ("README.md", "additional_required"), *extra]
        ],
    }


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "Synthetic source pin")
    return git(root, "rev-parse", "HEAD").decode().strip()


def source(root):
    git(root, "config", "core.autocrlf", "false")
    (root / "AGENTS.md").write_bytes("Complete instructions，計画．\r\nSecond line\n".encode())
    (root / "README.md").write_bytes(b"\xef\xbb\xbfComplete README\n")
    for path in DELIVERED_DOCUMENTS:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Existing gate delivers the whole document: " + path + "\n", encoding="utf-8")
    return {path: file_hash(root / path) for path in DELIVERED_DOCUMENTS}


@pytest.fixture
def packet_case(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init")
    pins = source(root)
    revision = commit(root)
    spec = selection(root, revision)
    return root, pins, revision, spec


def build(case):
    root, pins, revision, spec = case
    return build_packet(spec, root, revision, pins, git)


def test_exact_utf8_text_manifest_and_determinism(packet_case):
    root, pins, revision, spec = packet_case
    first = build(packet_case)
    spec["files"].reverse()
    second = build(packet_case)
    assert first == second
    assert first.byte_count == len(first.text.encode("utf-8"))
    assert first.sha256 == digest(first.text.encode("utf-8"))
    packet = json.loads(first.text)
    assert packet["source_revision"] == revision and packet["model_token_impact"] is None
    entries = {entry["path"]: entry for entry in packet["files"]}
    for path in ("AGENTS.md", "README.md"):
        assert entries[path]["text"].encode("utf-8") == (root / path).read_bytes()
        assert entries[path]["sha256"] == file_hash(root / path)
    for path in DELIVERED_DOCUMENTS:
        assert entries[path]["category"] == "already_delivered"
        assert entries[path]["sha256"] == pins[path] and "text" not in entries[path]
    assert packet["source_bytes_by_category"]["additional_required"] == sum(e["bytes"] for e in spec["files"])


@pytest.mark.parametrize("path,category", [("docs/課題.md", "additional_required"),
                                          ("src/ephy_worker/example.py", "optional")])
def test_explicit_selected_context_and_unicode_paths(packet_case, path, category):
    root, pins, _, _ = packet_case
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text("Task text with an emoji: 🧪\n", encoding="utf-8")
    revision = commit(root)
    spec = selection(root, revision, [(path, category)])
    packet = json.loads(build_packet(spec, root, revision, pins, git).text)
    entry = next(e for e in packet["files"] if e["path"] == path)
    assert entry["category"] == category
    assert entry["text"].encode("utf-8") == (root / path).read_bytes()


@pytest.mark.parametrize("bad", [None, False, {}, {"version": 1}])
def test_incomplete_spec_rejected_without_reads(bad):
    with pytest.raises(GateFailure):
        validate_selection(bad)


@pytest.mark.parametrize("mutation", ["missing_root", "optional_root", "duplicate", "case_duplicate",
    "unknown_field", "unknown_file_field", "bad_hash", "bad_category", "bad_version", "bool_version",
    "bad_revision", "zero_size", "bool_size", "oversize", "zero_cap", "bool_cap", "oversize_cap"])
def test_invalid_selection_rejected_before_any_read(packet_case, mutation):
    root, pins, revision, spec = packet_case
    entry = spec["files"][0]
    if mutation == "missing_root":
        spec["files"].pop()
    elif mutation == "optional_root":
        entry["category"] = "optional"
    elif mutation in ("duplicate", "case_duplicate"):
        duplicate = copy.deepcopy(entry)
        if mutation == "case_duplicate":
            duplicate["path"] = "docs/A.md"
            entry["path"] = "docs/a.md"
        spec["files"].append(duplicate)
    elif mutation == "unknown_field":
        spec["unknown"] = True
    elif mutation == "unknown_file_field":
        entry["text"] = "Caller cannot substitute text"
    elif mutation == "bad_hash":
        entry["sha256"] = "not a hash"
    elif mutation == "bad_category":
        entry["category"] = "already_delivered"
    elif mutation in ("bad_version", "bool_version"):
        spec["version"] = 2 if mutation == "bad_version" else True
    elif mutation == "bad_revision":
        spec["source_revision"] = "main"
    elif mutation in ("zero_size", "bool_size", "oversize"):
        entry["bytes"] = {"zero_size": 0, "bool_size": True, "oversize": MAX_FILE_BYTES + 1}[mutation]
    else:
        spec["max_packet_bytes"] = {"zero_cap": 0, "bool_cap": True, "oversize_cap": MAX_PACKET_BYTES + 1}[mutation]
    def no_git(*args):
        pytest.fail("Invalid selection reached a read")
    with pytest.raises(GateFailure):
        build_packet(spec, root, revision, pins, no_git)


@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "C:/outside.md", "docs/../README.md",
    "docs//task.md", "docs\\task.md", ".git/config", ".aws/credentials", "configs/private.md",
    "docs/.secret.md", "docs/task.txt", "docs/system-development-governance.md"])
def test_closed_path_scope(packet_case, path):
    root, pins, revision, spec = packet_case
    spec["files"].append({"path": path, "sha256": "a" * 64, "bytes": 1, "category": "optional"})
    with pytest.raises(GateFailure):
        build_packet(spec, root, revision, pins, lambda *args: pytest.fail("Unsafe path reached Git"))


@pytest.mark.parametrize("mutation", ["missing", "hash", "size", "source", "untracked", "utf8",
    "too_large", "directory", "base", "head", "delivered_hash", "partial_delivered"])
def test_content_and_source_fail_closed(packet_case, mutation):
    root, pins, revision, spec = packet_case
    target = root / "README.md"
    if mutation == "missing":
        target.unlink()
    elif mutation == "hash":
        spec["files"][1]["sha256"] = "a" * 64
    elif mutation == "size":
        spec["files"][1]["bytes"] += 1
    elif mutation == "source":
        target.write_bytes(b"Different content\n")
        spec = selection(root, revision)
    elif mutation == "untracked":
        target = root / "docs/untracked.md"
        target.write_text("Untracked source\n", encoding="utf-8")
        spec = selection(root, revision, [("docs/untracked.md", "optional")])
    elif mutation == "utf8":
        target.write_bytes(b"\xff\xfe\n")
        revision = commit(root)
        spec = selection(root, revision)
    elif mutation == "too_large":
        target.write_bytes(b"X" * (MAX_FILE_BYTES + 1))
    elif mutation == "directory":
        target.unlink()
        target.mkdir()
    elif mutation == "base":
        spec["source_revision"] = "a" * 40
    elif mutation == "head":
        target.write_text("New committed source\n", encoding="utf-8")
        commit(root)
    elif mutation == "delivered_hash":
        pins[DELIVERED_DOCUMENTS[0]] = "a" * 64
    else:
        pins.pop(DELIVERED_DOCUMENTS[0])
    with pytest.raises(GateFailure):
        build_packet(spec, root, revision, pins, git)


def test_total_packet_limit_has_no_partial_or_truncated_delivery(packet_case):
    root, pins, revision, spec = packet_case
    size = build(packet_case).byte_count
    # Changing the cap's decimal width can change the serialized byte count.
    spec["max_packet_bytes"] = size - 100
    with pytest.raises(GateFailure, match="non-truncating byte cap"):
        build_packet(spec, root, revision, pins, git)


def test_selected_file_count_is_bounded(packet_case):
    spec = packet_case[3]
    spec["files"].extend({"path": f"docs/task-{index}.md", "sha256": "a" * 64, "bytes": 1,
                          "category": "optional"} for index in range(15))
    with pytest.raises(GateFailure, match="2..16"):
        validate_selection(spec)


def test_hard_link_rejected(packet_case):
    root, pins, revision, spec = packet_case
    os.link(root / "README.md", root / "alias.md")
    with pytest.raises(GateFailure, match="Hard link"):
        build_packet(spec, root, revision, pins, git)


def test_swapped_file_is_rejected_before_reading_its_contents(packet_case, tmp_path, monkeypatch):
    root, pins, revision, spec = packet_case
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"Secret outside selection\n")
    original_open = Path.open
    def substituted_open(path, *args, **kwargs):
        if path == root / "AGENTS.md":
            return original_open(outside, *args, **kwargs)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", substituted_open)
    with pytest.raises(GateFailure, match="changed before read"):
        build_packet(spec, root, revision, pins, git)


def test_symlink_or_windows_junction_rejected(packet_case, tmp_path):
    root, pins, revision, spec = packet_case
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "task.md").write_text("Outside root\n", encoding="utf-8")
    link = root / "docs/link"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    spec["files"].append({"path": "docs/link/task.md", "sha256": file_hash(outside / "task.md"),
                          "bytes": (outside / "task.md").stat().st_size, "category": "optional"})
    with pytest.raises(GateFailure, match="Link/reparse"):
        build_packet(spec, root, revision, pins, git)


def prepare_runner(case):
    runner = case.runner
    pins = source(runner.candidate)
    revision = commit(runner.candidate)
    runner.job["baseRevision"] = revision
    runner.runtime["governance_root"] = str(runner.candidate)
    runner.job["controls"] = {name: pins[path] for name, path in CONTROL_PATHS.items() if path in pins}
    runner.contract["runtime_hashes"][str(runner.candidate / "docs/self-improvement-mvp.md")] = (
        pins["docs/self-improvement-mvp.md"]
    )
    runner.contract["planner_context"] = selection(runner.candidate, revision)
    runner.contract_sha = digest(encode(runner.contract))
    return runner


def test_default_prompt_byte_regression(planner_prompt_case):
    case = planner_prompt_case()
    case.runner.run()
    # Measured from the unmodified d36960b controller with this frozen fixture contract.
    assert hashlib.sha256(case.calls[0].prompt.encode()).hexdigest() == (
        "3cfb8305a979cbe48b7cc64acf7bf5322bb2a0447477ae9b8465d1430ea734eb"
    )
    assert not (case.runner.directory / "planner-context-packet.json").exists()


def test_opt_in_prompt_complete_and_deterministic(planner_prompt_case):
    case = planner_prompt_case()
    runner = prepare_runner(case)
    before = snapshot(runner.candidate)
    runner.run()
    packet = read_json(runner.directory / "planner-context-packet.json")
    prompt = case.calls[0].prompt
    assert "Controller-verified planner context packet SHA-256=" in prompt
    assert encode(packet).decode() in prompt
    assert case.calls[0].role == "planner" and case.calls[0].kwargs == {}
    assert snapshot(runner.candidate) == before
    (runner.directory / "planner-context-packet.json").unlink()
    (runner.directory / "BACKLOG.json").unlink()
    (runner.directory / "retained-candidate-snapshot.json").unlink()
    runner.contract["planner_context"]["files"].reverse()
    runner.run()
    assert case.calls[1].prompt == prompt


@pytest.mark.parametrize("mutation", ["missing", "hash", "cap", "partial"])
def test_rejected_packet_never_starts_stage_or_invents_a_plan(planner_prompt_case, mutation):
    case = planner_prompt_case()
    runner = prepare_runner(case)
    if mutation == "missing":
        (runner.candidate / "AGENTS.md").unlink()
    elif mutation == "hash":
        runner.contract["planner_context"]["files"][0]["sha256"] = "a" * 64
    elif mutation == "cap":
        runner.contract["planner_context"]["max_packet_bytes"] = 1
    else:
        runner.contract["planner_context"]["files"].pop()
    with pytest.raises(GateFailure):
        runner.run()
    assert case.calls == []
    assert read_json(runner.job_file)["outcome"] == "infrastructure_failed"
    assert not (runner.directory / "lead-plan.txt").exists()
    assert not (runner.directory / "BACKLOG.json").exists()
    assert not (runner.directory / "planner-context-packet.json").exists()


def test_contract_accepts_explicit_packet_but_rejects_null(packet_case):
    contract = test_formal_runtime.contract()
    contract["planner_context"] = packet_case[3]
    validate_contract(contract)
    contract["planner_context"] = None
    with pytest.raises(GateFailure):
        validate_contract(contract)


def test_packet_selection_binds_invocation_identity(packet_case, monkeypatch):
    from ephy_worker.formal_runtime import invocation_identity

    runtime = {name: "/offline/" + name for name in (
        "pi", "server", "models_ini", "provider", "stage_guard", "governance_gate",
    )}
    contract = test_formal_runtime.contract()
    monkeypatch.setattr("ephy_worker.formal_runtime.file_hash", lambda _: "a" * 64)
    default = invocation_identity(runtime, contract, "planner")
    contract["planner_context"] = packet_case[3]
    selected = invocation_identity(runtime, contract, "planner")
    assert selected != default
    contract["planner_context"]["files"][0]["sha256"] = "b" * 64
    assert invocation_identity(runtime, contract, "planner") != selected
