"""Exact-head review gates are independent of model claims．"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker.formal_artifacts import GateFailure, digest, encode, file_hash, write_json
from ephy_worker.owner_repair import OWNER_USER_ID, RepairStore
from ephy_worker.owner_repair_release import GitHubRelease, current_head_ready, public_evidence

HEAD = "a" * 40


def evidence():
    checks = {"total_count": 1, "check_runs": [{"id": 1, "name": "tests", "head_sha": HEAD,
        "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]}
    state = {"code_review": {"commit_sha": HEAD, "status": "completed", "has_unresolved_findings": False}}
    comments = [{"id": 2, "user": {"login": "chatgpt-codex-connector[bot]", "type": "Bot"},
        "body": "<!-- codex-review-state:v1 " + json.dumps(state) + " -->"}]
    threads = {"pageInfo": {"hasNextPage": False}, "nodes": [{"isResolved": True}]}
    return checks, comments, threads


def test_only_exact_final_head_evidence_can_open_release_gate():
    checks, comments, threads = evidence()
    assert current_head_ready(HEAD, checks, comments, threads, ("tests",))
    assert not current_head_ready("b" * 40, checks, comments, threads, ("tests",))
    assert not current_head_ready(HEAD, checks, comments, threads, ("tests", "lint"))
    assert not current_head_ready(HEAD, checks, comments, threads, ())


@pytest.mark.parametrize("fault", ["stale_ci", "failed_ci", "running_ci", "latest_rerun_failed", "ci_truncated",
    "stale_review", "review_running", "unresolved_finding", "spoofed_comment", "review_absent",
    "unresolved_thread", "thread_pagination", "malformed_review", "spoofed_ci"])
def test_stale_missing_or_untrusted_evidence_never_merges(fault):
    checks, comments, threads = copy.deepcopy(evidence())
    if fault == "stale_ci":
        checks["check_runs"][0]["head_sha"] = "b" * 40
    elif fault == "failed_ci":
        checks["check_runs"][0]["conclusion"] = "failure"
    elif fault == "running_ci":
        checks["check_runs"][0]["status"] = "in_progress"
    elif fault == "latest_rerun_failed":
        checks["check_runs"].append({**checks["check_runs"][0], "id": 10, "conclusion": "failure"})
    elif fault == "ci_truncated":
        checks["total_count"] = 200
    elif fault == "stale_review":
        comments[0]["body"] = comments[0]["body"].replace(HEAD, "b" * 40)
    elif fault == "review_running":
        comments[0]["body"] = comments[0]["body"].replace("completed", "in_progress")
    elif fault == "unresolved_finding":
        comments[0]["body"] = comments[0]["body"].replace("false", "true")
    elif fault == "spoofed_comment":
        comments[0]["user"]["login"] = "kirimine170"
    elif fault == "review_absent":
        comments.clear()
    elif fault == "unresolved_thread":
        threads["nodes"][0]["isResolved"] = False
    elif fault == "thread_pagination":
        threads["pageInfo"]["hasNextPage"] = True
    elif fault == "spoofed_ci":
        checks["check_runs"][0]["app"]["slug"] = "untrusted-app"
    else:
        comments[0]["body"] = "<!-- codex-review-state:v1 invalid -->"
    assert not current_head_ready(HEAD, checks, comments, threads, ("tests",))


@pytest.mark.parametrize("result", [{"deployed_commit": "b" * 40, "healthy": False, "model_identity_unchanged": True},
    {"deployed_commit": "b" * 40, "healthy": True, "model_identity_unchanged": False},
    {"deployed_commit": "c" * 40, "healthy": True, "model_identity_unchanged": True}])
def test_deployment_failure_never_marks_completed(tmp_path, monkeypatch, result):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=100, author_id=OWNER_USER_ID, text="ephyを修正して")
    directory = store.root / "execution" / identifier
    directory.mkdir(parents=True)
    adapter = tmp_path / "release.ps1"
    adapter.write_text("# fixed operator adapter")
    source = tmp_path / "source"
    source.mkdir()
    profile = SimpleNamespace(source=source, origin="https://github.com/kirimine170/ephy-worker.git",
        github_repository="kirimine170/ephy-worker", branch="main", required_checks=("tests",), production=tmp_path / "production")
    config = SimpleNamespace(state_dir=store.root, repositories={"ephy-worker": profile},
        release_adapter=adapter, release_adapter_sha256=file_hash(adapter), powershell="powershell.exe")
    release = GitHubRelease(config)
    patch = directory / "candidate.patch"
    patch.write_text("frozen audited patch")
    frozen = {"repoRoot": str(source), "baseRevision": "d" * 40}
    monkeypatch.setattr(release, "_proposal", lambda _: (frozen, patch, tmp_path / "candidate"))
    monkeypatch.setattr("ephy_worker.owner_repair_release.public_evidence", lambda *_: {"head": HEAD})
    monkeypatch.setattr(release, "publish_evidence", lambda *_: None)
    monkeypatch.setattr("ephy_worker.owner_repair_release.snapshot", lambda _: {})
    def git(_root, *args):
        if args[:2] == ("remote", "get-url"):
            return profile.origin.encode()
        return HEAD.encode() if args[0] == "rev-parse" else b""
    monkeypatch.setattr(release, "git", git)
    monkeypatch.setattr(release, "api", lambda *_: {"head": {"sha": HEAD}, "base": {"ref": "main"},
        "merged": True, "merge_commit_sha": "b" * 40})
    monkeypatch.setattr(release, "_operator", lambda *_, **__: json.dumps(result).encode())
    checks, comments, threads = evidence()
    write_json(directory / "release-evidence.json", {"head": HEAD, "checks": checks, "comments": comments, "threads": threads})
    store.transition(identifier, "deployment_pending", repository="ephy-worker", release_head=HEAD,
        pr_number=1, pr_url="https://github.com/kirimine170/ephy-worker/pull/1", evidence_head=HEAD,
        release_evidence_sha256=file_hash(directory / "release-evidence.json"))
    _review_v11_release_fixture(release, store, identifier, monkeypatch, merged=True)
    with pytest.raises(GateFailure, match="deployment_not_verified"):
        release.advance(store, store.get(identifier))
    assert store.get(identifier)["stage"] == "deployment_pending"
    assert store.pending(guild_id=11, channel_ids=(20,)) == []


def test_successful_deployment_atomically_persists_completion_and_message(tmp_path, monkeypatch):
    # Reuse the same operator path as the failure tests with the one valid result．
    result = {"deployed_commit": "b" * 40, "healthy": True, "model_identity_unchanged": True}
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=100, author_id=OWNER_USER_ID, text="ephyを修正して")
    directory = store.root / "execution" / identifier
    directory.mkdir(parents=True)
    adapter = tmp_path / "release.ps1"
    adapter.write_text("# operator adapter")
    source = tmp_path / "source"
    source.mkdir()
    profile = SimpleNamespace(source=source, origin="https://github.com/kirimine170/ephy-worker.git",
        github_repository="kirimine170/ephy-worker", branch="main", required_checks=("tests",))
    release = GitHubRelease(SimpleNamespace(state_dir=store.root, repositories={"ephy-worker": profile},
        release_adapter=adapter, release_adapter_sha256=file_hash(adapter), powershell="powershell.exe"))
    patch = directory / "candidate.patch"
    patch.write_text("audited")
    monkeypatch.setattr(release, "_proposal", lambda _: ({"repoRoot": str(source)}, patch, tmp_path / "candidate"))
    monkeypatch.setattr("ephy_worker.owner_repair_release.public_evidence", lambda *_: {"head": HEAD})
    monkeypatch.setattr(release, "publish_evidence", lambda *_: None)
    monkeypatch.setattr("ephy_worker.owner_repair_release.snapshot", lambda _: {})
    monkeypatch.setattr(release, "git", lambda _root, *args: profile.origin.encode() if args[0] == "remote"
        else HEAD.encode() if args[0] == "rev-parse" else b"")
    monkeypatch.setattr(release, "api", lambda *_: {"head": {"sha": HEAD}, "base": {"ref": "main"},
        "merged": True, "merge_commit_sha": "b" * 40})
    monkeypatch.setattr(release, "_operator", lambda *_, **__: json.dumps(result).encode())
    checks, comments, threads = evidence()
    write_json(directory / "release-evidence.json", {"head": HEAD, "checks": checks, "comments": comments, "threads": threads})
    store.transition(identifier, "deployment_pending", repository="ephy-worker", release_head=HEAD,
        pr_number=1, pr_url="https://github.com/kirimine170/ephy-worker/pull/1", evidence_head=HEAD,
        release_evidence_sha256=file_hash(directory / "release-evidence.json"))
    _review_v11_release_fixture(release, store, identifier, monkeypatch, merged=True)
    release.advance(store, store.get(identifier))
    assert store.get(identifier)["stage"] == "completed" and len(store.pending(guild_id=11, channel_ids=(20,))) == 1


def relay_fixture(tmp_path):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=101, author_id=OWNER_USER_ID,
        text="private natural repair directive never exported")
    config = SimpleNamespace(state_dir=store.root)
    for name in ("review_adapter", "review_adapter_python", "review_adapter_config"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        setattr(config, name, path)
        setattr(config, name + "_sha256", file_hash(path))
    return store, identifier, GitHubRelease(config)


def relay_result(identifier):
    return {"schema": "ephy.owner-review-result.v1", "repair_id": identifier,
        "repository": "kirimine170/ephy-worker", "pr_number": 9, "head_sha": HEAD,
        "requested": True, "owner_id": 78987919, "owner_login": "kirimine170",
        "comment_id": 100, "deduplicated": False}


def test_owner_review_relay_exact_request_and_validated_result_are_durable(tmp_path, monkeypatch):
    store, identifier, release = relay_fixture(tmp_path)
    calls = []
    def operator(argv, cwd, **kwargs):
        calls.append((argv, cwd, kwargs))
        request = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        assert request == {key: value for key, value in relay_result(identifier).items()
            if key in {"repair_id", "repository", "pr_number", "head_sha"}} | {"schema": "ephy.owner-review-request.v1"}
        assert store.get(identifier)["text"] not in json.dumps(request)
        return json.dumps(relay_result(identifier)).encode()
    monkeypatch.setattr(release, "_operator", operator)
    release.request_owner_review(store.get(identifier), "kirimine170/ephy-worker", 9, HEAD, store)
    argv, directory, kwargs = calls[0]
    assert argv == [str(release.config.review_adapter_python), "-I", str(release.config.review_adapter),
        "--config", str(release.config.review_adapter_config), "--request", str(directory / "review-request.json")]
    assert kwargs == {"timeout": 300}
    data = store.get(identifier)["data"]
    assert data["review_requested"] is True
    assert data["owner_review_result_sha256"] == file_hash(directory / "review-result.json")


@pytest.mark.parametrize("fault", ["head", "owner", "repository", "id", "requested", "extra", "invalid", "duplicate",
    "failure", "timeout", "script_changed", "python_changed", "config_changed", "changed_after_call"])
def test_owner_review_relay_failure_never_records_review_requested(tmp_path, monkeypatch, fault):
    import subprocess

    from ephy_worker.owner_repair_release import ReviewPending
    store, identifier, release = relay_fixture(tmp_path)
    result = relay_result(identifier)
    if fault in {"head", "owner", "repository", "id", "requested", "extra"}:
        key, value = {"head": ("head_sha", "b" * 40), "owner": ("owner_id", 1),
            "repository": ("repository", "untrusted/repo"), "id": ("repair_id", "repair-" + "0" * 32),
            "requested": ("requested", False), "extra": ("untrusted", "payload")}[fault]
        result[key] = value
    def operator(*args, **kwargs):
        if fault == "failure":
            raise GateFailure("operator_command_failed")
        if fault == "timeout":
            raise subprocess.TimeoutExpired("fixed", 300)
        if fault == "changed_after_call":
            release.config.review_adapter.write_text("changed")
        if fault == "invalid":
            return b"not-json"
        if fault == "duplicate":
            return json.dumps(result).replace('"requested": true', '"requested": true, "requested": true').encode()
        return json.dumps(result).encode()
    monkeypatch.setattr(release, "_operator", operator)
    if fault in {"script_changed", "python_changed", "config_changed"}:
        name = {"script_changed": "review_adapter", "python_changed": "review_adapter_python",
            "config_changed": "review_adapter_config"}[fault]
        getattr(release.config, name).write_text("changed")
    with pytest.raises((GateFailure, ReviewPending)):
        release.request_owner_review(store.get(identifier), "kirimine170/ephy-worker", 9, HEAD, store)
    assert not store.get(identifier)["data"].get("review_requested")
    assert not (store.root / "reviews" / identifier / "review-result.json").exists()


def test_review_adapter_config_is_optional_but_selection_requires_all_pins(tmp_path):
    from pydantic import ValidationError

    from ephy_worker.owner_repair_pipeline import ControllerConfig
    configuration = {"state_dir": tmp_path, "guild_id": 1, "channel_ids": [2],
        "python": str(tmp_path / "python.exe"), "git": str(tmp_path / "git.exe"), "gh": str(tmp_path / "gh.exe"),
        "powershell": str(tmp_path / "powershell.exe"), "release_adapter": str(tmp_path / "release.ps1"),
        "release_adapter_sha256": "a" * 64, "repositories": {"ephy-worker": {
            "source": tmp_path / "source", "production": tmp_path / "production",
            "origin": "https://github.com/kirimine170/ephy-worker.git", "github_repository": "kirimine170/ephy-worker",
            "required_checks": ["tests"], "formal_template": tmp_path / "template.json", "formal_template_sha256": "b" * 64}}}
    assert ControllerConfig.model_validate(configuration).review_adapter is None
    with pytest.raises(ValidationError, match="complete_review_adapter_pins_required"):
        ControllerConfig.model_validate({**configuration, "review_adapter": str(tmp_path / "relay.py")})
    selection = {}
    for name in ("review_adapter", "review_adapter_python", "review_adapter_config"):
        selection[name], selection[name + "_sha256"] = str(tmp_path / name), "c" * 64
    assert ControllerConfig.model_validate({**configuration, **selection}).review_adapter == tmp_path / "review_adapter"


@pytest.mark.parametrize("mode", ["default", "relay", "relay_failure"])
def test_review_relay_selection_preserves_exact_head_gate_and_has_no_cli_fallback(tmp_path, monkeypatch, mode):
    from ephy_worker.owner_repair_release import ReviewPending
    store, identifier, release = relay_fixture(tmp_path)
    if mode == "default":
        release.config.review_adapter = None
    source = tmp_path / "source"
    source.mkdir()
    profile = SimpleNamespace(source=source, origin="https://github.com/kirimine170/ephy-worker.git",
        github_repository="kirimine170/ephy-worker", branch="main", required_checks=("tests",))
    release.config.repositories = {"ephy-worker": profile}
    directory = store.root / "execution" / identifier
    directory.mkdir(parents=True)
    patch = directory / "candidate.patch"
    patch.write_text("frozen patch")
    frozen = {"repoRoot": str(source), "baseRevision": "d" * 40, "contract": {"allowed_files": ["src/example.py"]}}
    monkeypatch.setattr(release, "_proposal", lambda _: (frozen, patch, tmp_path / "candidate"))
    monkeypatch.setattr(release, "publish_evidence", lambda *_: None)
    monkeypatch.setattr("ephy_worker.owner_repair_release.public_evidence", lambda *_: {"head": HEAD})
    monkeypatch.setattr("ephy_worker.owner_repair_release.snapshot", lambda _: {})
    monkeypatch.setattr(release, "git", lambda _, *args: profile.origin.encode() if args[0] == "remote"
        else HEAD.encode() if args[0] == "rev-parse" else b"")
    posted, relayed = [], []
    def api(method, path, payload=None):
        if "/pulls/" in path:
            return {"head": {"sha": HEAD}, "base": {"ref": "main"}, "state": "open", "merged": False}
        if path.endswith("/graphql"):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": evidence()[2]}}}}
        if method == "POST":
            posted.append(payload)
            return {"id": 1}
        if "check-runs" in path:
            return {"total_count": 0, "check_runs": []}
        return []
    monkeypatch.setattr(release, "api", api)
    def relay(job, repository, number, head, actual_store):
        relayed.append((job["id"], repository, number, head))
        if mode == "relay_failure":
            raise ReviewPending("relay unavailable")
        actual_store.transition(identifier, "review_pending", review_requested=True)
    monkeypatch.setattr(release, "request_owner_review", relay)
    store.transition(identifier, "review_pending", repository="ephy-worker", release_head=HEAD,
        pr_number=9, pr_url="https://github.com/kirimine170/ephy-worker/pull/9")
    _review_v11_release_fixture(release, store, identifier, monkeypatch, merged=False)
    with pytest.raises(ReviewPending):
        release.advance(store, store.get(identifier))
    assert bool(posted) == (mode == "default")
    assert bool(relayed) == (mode != "default")
    assert bool(store.get(identifier)["data"].get("review_requested")) == (mode != "relay_failure")
    assert store.get(identifier)["stage"] == "review_pending"
    assert not (directory / "release-evidence.json").exists()


def public_fixture(tmp_path):
    directory, source, checker_root = tmp_path / "formal", tmp_path / "repository", tmp_path / "discovery"
    bundle = directory / "audit-bundle"
    bundle.mkdir(parents=True)
    (source / "src").mkdir(parents=True)
    (source / "src/example.py").write_text("value = 0\n", encoding="utf-8")
    checker_root.mkdir()
    checker, acceptance, guard = checker_root / "checker.py", checker_root / "acceptance.py", checker_root / "checker-guard.json"
    checker.write_text("import ast, json\n# Trusted guard fixture source\n", encoding="utf-8")
    acceptance.write_text("assert 1 == 1\n# Behavior checker source fixture\n", encoding="utf-8")
    write_json(guard, {"allowed": ["src/example.py"], "tests": {}, "authority": {}})
    prepared = {"known_good": {"src/example.py": "value = 1\n"}, "wrong_answer": {"src/example.py": "value = -1\n"}}
    write_json(checker_root / "discovery.json", {"prepared": prepared,
        "source_sha256": {"src/example.py": file_hash(source / "src/example.py")}})
    controls = []
    for name in ("known_good", "unchanged", "wrong_answer", "test_weakening", "scope_escape"):
        replacement = prepared.get(name, prepared["known_good"])["src/example.py"]
        specimen = {"src/example.py": digest(replacement.encode())}
        path = checker_root / ("control-" + name + ".json")
        write_json(path, {"exit_code": 0 if name == "known_good" else 1, "specimen_snapshot": specimen})
        controls.append({"name": name, "actual": 0 if name == "known_good" else 1,
            "expected": 0 if name == "known_good" else 1, "exit_code": 0 if name == "known_good" else 1,
            "evidence_sha256": file_hash(path), "specimen_snapshot_sha256": digest(encode(specimen))})
    control_path = checker_root / "checker-controls.json"
    write_json(control_path, controls)
    (directory / "test.stdout").write_text("3 passed in 0.01s\n", encoding="utf-8")
    (directory / "test.stderr").write_text("", encoding="utf-8")
    measurement = {"passed": True, "baseline": False, "snapshot_sha256": "a" * 64,
        "verifier_identity": {"executor_id": "actual-test-fixture"}, "changed_files": ["src/example.py"], "checks": [{
            "id": "target", "argv": ["python", "-m", "pytest", "tests/test_example.py"], "exit_code": 0, "passed": True,
            "stdout": "test.stdout", "stderr": "test.stderr", "stdout_sha256": file_hash(directory / "test.stdout"),
            "stderr_sha256": file_hash(directory / "test.stderr")}]}
    write_json(bundle / "preflight_result.txt", {**measurement, "baseline": True})
    write_json(bundle / "verification_results.txt", measurement)
    write_json(bundle / "audit-input.json", {"final_bindings": {"candidate_patch_sha256": "b" * 64}})
    write_json(bundle / "environment_contract.txt", {"python_version": "3.12 fixture", "dependencies": {"pytest": "fixture"},
        "offline": True, "locale": "UTF-8"})
    write_json(bundle / "candidate_snapshot_manifest.txt", {"src/example.py": {"sha256": "c" * 64}})
    write_json(bundle / "evidence-manifest.json", {"artifacts": [{"artifact_id": "checker_source", "sha256": file_hash(checker), "size_bytes": checker.stat().st_size}]})
    provenance = []
    for index, role in enumerate(("planner", "implementer", "auditor")):
        session = role + "-session.jsonl"
        (directory / session).write_text(json.dumps({"type": "session", "id": role + "-observed-id"}) + "\n"
            + json.dumps({"type": "message", "content": "private-chain-of-thought-do-not-export"}) + "\n", encoding="utf-8")
        provenance.append({"role": role, "model_id": "fixture-model", "identity": {"model_id": "fixture-model"},
            "pid": index + 10, "session_path": session, "session_sha256": file_hash(directory / session),
            "trace_sha256": "d" * 64, "trace_valid": True, "requests": 2, "output_tokens": 20,
            "governance_ack": {"kind": "governance_result", "isError": False, "details": {"role": role,
                "acknowledged": True, "policyId": "fixture-policy", "policySha256": "e" * 64},
                "content": [{"text": "private-tool-result-not-exported"}]}})
    write_json(bundle / "model_provenance.txt", provenance[:2])
    attestation = {key: True for key in ("schema_valid", "candidate_unchanged", "bundle_unchanged", "passed", "proposal_stopped")}
    attestation.update({key: "f" * 64 for key in ("audit_result_sha256", "audit_input_sha256", "post_audit_workflow_sha256")})
    write_json(directory / "audit-execution-attestation.json", {**attestation, "observed_auditor": provenance[2]})
    audit = {"decision": "ACCEPT_PROPOSAL", "reason_codes": ["ALL_GATES_PASSED"], "bound_inputs": {"audit_input_sha256": "f" * 64}}
    audit.update({domain: {"status": "PASS", "checks": [{"check_id": domain + "01", "status": "PASS",
        "summary": "Fixture inspected behavior and measurement evidence", "evidence": [{"artifact_id": "verification_results", "location": "complete checks", "sha256": "a" * 64}]}]}
        for domain in ("integrity", "candidate", "workflow")})
    write_json(directory / "audit-result.json", audit)
    patch = directory / "candidate.patch"
    patch.write_text("public source patch fixture", encoding="utf-8")
    frozen = {"jobDir": str(directory), "repoRoot": str(source), "worktreePath": str(tmp_path / "candidate"),
        "baseRevision": "b" * 40, "runtime": {"governance_root": str(source)}, "contract": {
            "semantic_scope": "Correct public response delivery behavior", "allowed_files": ["src/example.py"],
            "execution_profile": "native_windows_code_v1", "verifier_sandbox": {
                "network_policy": "windows_external_network_denied", "capability": {"sha256": "a" * 64}},
            "checker": str(checker), "checker_sha256": file_hash(checker), "checker_controls": str(control_path),
            "runtime_hashes": {str(path): file_hash(path) for path in (acceptance, guard, checker_root / "discovery.json")}}}
    job = {"text": "private-discord-directive-never-export", "previous": "private-prior-answer-never-export",
        "data": {"repository": "ephy-worker"}}
    return job, frozen, patch


def test_public_report_contains_inspectable_measurements_and_sources_without_private_messages(tmp_path):
    job, frozen, patch = public_fixture(tmp_path)
    report = public_evidence(job, frozen, patch, HEAD, "c" * 40)
    rendered = json.dumps(report, ensure_ascii=False)
    assert job["text"] not in rendered and job["previous"] not in rendered
    assert "private-chain-of-thought" not in rendered and "private-tool-result" not in rendered
    assert report["final"]["checks"][0]["stdout"]["text"] == "3 passed in 0.01s\n"
    assert "Behavior checker source fixture" in report["checker"]["acceptance_source"]
    assert "+value = 1" in report["checker"]["control_changes"]["known_good"]["src/example.py"]["unified_diff"]
    assert report["model_roles"][2]["session_id"] == "auditor-observed-id"
    assert report["audit"]["candidate"]["checks"][0]["summary"]


@pytest.mark.parametrize("fault", ["secret_checker", "raw_directive", "changed_output", "changed_control"])
def test_unsafe_or_stale_public_evidence_is_rejected_before_external_post(tmp_path, fault):
    job, frozen, patch = public_fixture(tmp_path)
    if fault == "secret_checker":
        acceptance = Path(frozen["contract"]["checker"]).with_name("acceptance.py")
        fake_prefix = "ghp_"
        acceptance.write_text(f"credential = '{fake_prefix}ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'\n", encoding="utf-8")
        frozen["contract"]["runtime_hashes"][str(acceptance)] = file_hash(acceptance)
    elif fault == "raw_directive":
        frozen["contract"]["semantic_scope"] = job["text"]
    elif fault == "changed_output":
        Path(frozen["jobDir"], "test.stdout").write_text("fabricated new output")
    else:
        Path(frozen["contract"]["checker"]).with_name("control-known_good.json").write_text("{}")
    with pytest.raises(GateFailure):
        public_evidence(job, frozen, patch, HEAD, "c" * 40)


def test_public_report_chunking_preserves_utf8_content_and_recorded_binding(tmp_path, monkeypatch):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=100, author_id=OWNER_USER_ID, text="私的な修正依頼")
    (store.root / "execution" / identifier).mkdir(parents=True)
    release = GitHubRelease(SimpleNamespace(state_dir=store.root))
    posted = []
    def api(method, _path, payload=None):
        if method == "GET":
            return []
        posted.append(payload["body"])
        return {"id": len(posted)}
    monkeypatch.setattr(release, "api", api)
    report = {"head": HEAD, "public_material": "公開された検証記録．" * 10000}
    release.publish_evidence("/repos/kirimine170/ephy-worker", 1, store.get(identifier), report, store)
    assert len(posted) > 1 and all(len(body.encode()) < 60000 for body in posted)
    combined = "".join(body.split("```json\n", 1)[1].rsplit("\n```", 1)[0] for body in posted)
    assert json.loads(combined) == report
    assert store.get(identifier)["data"]["public_evidence_sha256"] == digest(combined.encode())
    assert file_hash(store.root / "execution" / identifier / "public-evidence.json") == digest(combined.encode())


def _review_v11_release_fixture(release, store, identifier, monkeypatch, merged=True):
    """Actual pinned synthetic authority；never read operator-private artifacts．"""
    job = store.get(identifier)
    release.config.guild_id = int(job["guild"])
    release.config.channel_ids = (int(job["channel"]),)
    profile = release.config.repositories[job["data"]["repository"]]
    profile.production = getattr(profile, "production", profile.source.parent / "production")
    grant = {"schema": "ephy.owner-standing-release-grant.v1", "grant_id": "synthetic-test-standing-grant",
        "mode": "standing-owner-full-cycle", "owner_user_id": OWNER_USER_ID,
        "repositories": [profile.github_repository], "branch": "main",
        "actions": ["commit", "push", "pull_request", "merge", "deploy", "restart"],
        "source": {"kind": "operator-recorded-existing-human-instructions",
            "recorded_by": "integration-release-operator", "reference": "synthetic-fixture-only-not-a-human-event",
            "statements": ["Synthetic test records the standing full-cycle authority; no real approval is asserted."]}}
    grant_path = store.root / "authorization" / "standing-human-grant.json"
    grant_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(grant_path, grant)
    profile.formal_template = store.root / "synthetic-template.json"
    write_json(profile.formal_template, {"contract": {"runtime_hashes": {str(grant_path): file_hash(grant_path)}}})
    profile.formal_template_sha256 = file_hash(profile.formal_template)
    original_proposal = release._proposal
    frozen, patch, _candidate = original_proposal(job)
    frozen.setdefault("id", "synthetic-proposal-only")
    frozen.setdefault("baseRevision", "d" * 40)
    frozen.setdefault("contract", {"allowed_files": ["src/example.py"]})
    def proposal(actual):
        observed, actual_patch, actual_candidate = original_proposal(actual)
        return {**frozen, **observed}, actual_patch, actual_candidate
    monkeypatch.setattr(release, "_proposal", proposal)
    formal = store.root / "execution" / identifier / "synthetic-formal.json"
    write_json(formal, frozen)
    store.transition(identifier, job["stage"], formal_job=str(formal), patch_sha256=file_hash(patch))
    original_api = release.api
    def api(method, path, payload=None):
        result = original_api(method, path, payload)
        if method == "GET" and "/pulls/" in path and isinstance(result, dict):
            result = copy.deepcopy(result)
            result.setdefault("number", store.get(identifier)["data"]["pr_number"])
            for side in ("head", "base"):
                side_data = result.setdefault(side, {})
                if isinstance(side_data, dict):
                    repo = side_data.setdefault("repo", {})
                    if isinstance(repo, dict):
                        repo.setdefault("full_name", profile.github_repository)
        return result
    monkeypatch.setattr(release, "api", api)
    release._owner_context(store, store.get(identifier))
    if merged:
        release._authorization(store, store.get(identifier), frozen, patch, HEAD, HEAD,
            store.get(identifier)["data"]["pr_number"], create=True)


def _review_v11_case(tmp_path, monkeypatch, *, merged=False):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=123, author_id=OWNER_USER_ID,
        text="synthetic private directive", previous_answer="synthetic private context")
    directory = store.root / "execution" / identifier
    directory.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    adapter = tmp_path / "adapter.ps1"
    adapter.write_text("# synthetic inert adapter", encoding="utf-8")
    profile = SimpleNamespace(source=source, production=tmp_path / "production", branch="main",
        origin="https://github.com/kirimine170/ephy-worker.git", github_repository="kirimine170/ephy-worker",
        required_checks=("tests",))
    release = GitHubRelease(SimpleNamespace(state_dir=store.root, repositories={"ephy-worker": profile},
        release_adapter=adapter, release_adapter_sha256=file_hash(adapter), powershell="synthetic-powershell"))
    patch = directory / "candidate.patch"
    patch.write_text("synthetic frozen patch", encoding="utf-8")
    frozen = {"id": "synthetic-formal-job", "repoRoot": str(source), "baseRevision": "d" * 40,
        "contract": {"allowed_files": ["src/example.py"]}}
    monkeypatch.setattr(release, "_proposal", lambda _: (frozen, patch, tmp_path / "candidate"))
    monkeypatch.setattr("ephy_worker.owner_repair_release.snapshot", lambda _: {})
    monkeypatch.setattr("ephy_worker.owner_repair_release.public_evidence", lambda *_: {"head": HEAD})
    monkeypatch.setattr(release, "publish_evidence", lambda *_: None)
    calls = []
    def git(_root, *args):
        calls.append(("git", args))
        return profile.origin.encode() if args[0] == "remote" else HEAD.encode() if args[0] == "rev-parse" else b""
    monkeypatch.setattr(release, "git", git)
    remote = {"merged": merged}
    def api(method, path, payload=None):
        calls.append((method, path))
        if method == "PUT":
            data = store.get(identifier)["data"]
            authorization = directory / "release-authorization.json"
            assert data["release_authorization_sha256"] == file_hash(authorization)
            assert data["release_authorization_head"] == payload["sha"] == HEAD
            remote["merged"] = True
            return {"merged": True, "sha": "b" * 40}
        if "/pulls/" in path:
            return {"number": 1, "head": {"sha": HEAD, "repo": {"full_name": profile.github_repository}},
                "base": {"ref": "main", "repo": {"full_name": profile.github_repository}}, "state": "open",
                "merged": remote["merged"], "merge_commit_sha": "b" * 40}
        if "check-runs" in path:
            return evidence()[0]
        if path == "/graphql":
            return {"data": {"repository": {"pullRequest": {"reviewThreads": evidence()[2]}}}}
        return evidence()[1]
    monkeypatch.setattr(release, "api", api)
    def operator(argv, *_args, **_kwargs):
        calls.append(("deploy", argv))
        request = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        assert set(request) == {"schema", "repair_id", "owner_user_id", "repository", "deployed_commit", "pr_number", "head_sha"}
        return json.dumps({"deployed_commit": "b" * 40, "healthy": True, "model_identity_unchanged": True}).encode()
    monkeypatch.setattr(release, "_operator", operator)
    store.transition(identifier, "review_pending", repository="ephy-worker", release_head=HEAD,
        pr_number=1, pr_url="https://github.com/kirimine170/ephy-worker/pull/1", review_requested=True)
    if merged:
        checks, comments, threads = evidence()
        write_json(directory / "release-evidence.json", {"head": HEAD, "checks": checks, "comments": comments, "threads": threads})
        store.transition(identifier, "deployment_pending", evidence_head=HEAD,
            release_evidence_sha256=file_hash(directory / "release-evidence.json"))
    _review_v11_release_fixture(release, store, identifier, monkeypatch, merged=merged)
    calls.clear()
    return store, identifier, release, calls


def test_review_v11_standing_authority_is_durable_before_merge_and_resumes(tmp_path, monkeypatch):
    store, identifier, release, calls = _review_v11_case(tmp_path, monkeypatch)
    release.advance(store, store.get(identifier))
    directory = store.root / "execution" / identifier
    record = json.loads((directory / "release-authorization.json").read_text(encoding="utf-8"))
    assert record["new_head_specific_human_approval"] is False
    assert record["event"]["author"] == str(OWNER_USER_ID)
    assert record["event"]["text_sha256"] == digest(store.get(identifier)["text"].encode())
    assert record["head"] == record["tree"] == HEAD
    assert record["standing_grant"]["sha256"] == file_hash(store.root / "authorization" / "standing-human-grant.json")
    assert any(call[0] == "PUT" for call in calls) and any(call[0] == "deploy" for call in calls)
    original = (directory / "release-authorization.json").read_bytes()
    frozen, patch, _ = release._proposal(store.get(identifier))
    release._authorization(RepairStore(store.root), store.get(identifier), frozen, patch, HEAD, HEAD, 1)
    assert (directory / "release-authorization.json").read_bytes() == original
    assert sum(call[0] == "PUT" for call in calls) == 1
    assert store.get(identifier)["stage"] == "completed"


@pytest.mark.parametrize("fault", ["missing_grant", "unpinned_grant", "owner_bool", "extra_head", "duplicate_key",
    "wrong_guild", "wrong_channel", "noncanonical_owner", "message_changed", "text_changed", "context_changed",
    "grant_revoked", "authorization_changed", "authorization_missing", "formal_changed", "evidence_changed"])
def test_review_v11_changed_or_incomplete_authority_never_merges_or_deploys(tmp_path, monkeypatch, fault):
    store, identifier, release, calls = _review_v11_case(tmp_path, monkeypatch, merged=True)
    grant_path = store.root / "authorization" / "standing-human-grant.json"
    profile = release.config.repositories["ephy-worker"]
    if fault == "missing_grant":
        grant_path.unlink()
    elif fault in {"unpinned_grant", "owner_bool", "extra_head", "duplicate_key", "grant_revoked"}:
        grant = json.loads(grant_path.read_text(encoding="utf-8"))
        if fault == "owner_bool":
            grant["owner_user_id"] = True
        elif fault == "extra_head":
            grant["head"] = HEAD
        elif fault == "grant_revoked":
            grant["actions"].remove("merge")
        else:
            grant["grant_id"] = "changed-grant"
        grant_path.write_text(json.dumps(grant), encoding="utf-8")
        if fault == "duplicate_key":
            grant_path.write_text('{"schema":"duplicate",' + grant_path.read_text(encoding="utf-8")[1:], encoding="utf-8")
        if fault != "unpinned_grant":
            profile.formal_template.write_text(json.dumps({"contract": {"runtime_hashes": {str(grant_path): file_hash(grant_path)}}}), encoding="utf-8")
            profile.formal_template_sha256 = file_hash(profile.formal_template)
    elif fault in {"wrong_guild", "wrong_channel", "noncanonical_owner", "message_changed", "text_changed", "context_changed"}:
        column, value = {"wrong_guild": ("guild", "12"), "wrong_channel": ("channel", "21"),
            "noncanonical_owner": ("author", "0" + str(OWNER_USER_ID)), "message_changed": ("message", "124"),
            "text_changed": ("text", "changed directive"), "context_changed": ("previous", "changed context")}[fault]
        with store.db() as db:
            db.execute("UPDATE jobs SET " + column + "=? WHERE id=?", (value, identifier))
    else:
        filename = {"authorization_changed": "release-authorization.json", "authorization_missing": "release-authorization.json",
            "formal_changed": "synthetic-formal.json", "evidence_changed": "release-evidence.json"}[fault]
        path = store.root / "execution" / identifier / filename
        if fault == "authorization_missing":
            path.unlink()
        else:
            path.write_text("{}", encoding="utf-8")
    with pytest.raises((GateFailure, ValueError, KeyError)):
        release.advance(store, store.get(identifier))
    assert not any(call[0] in {"PUT", "deploy"} for call in calls)
    assert store.get(identifier)["stage"] != "completed"


def test_review_v11_premerged_evidence_without_authorization_cannot_deploy(tmp_path, monkeypatch):
    store, identifier, release, calls = _review_v11_case(tmp_path, monkeypatch, merged=True)
    with store.db() as db:
        data = store.get(identifier)["data"]
        data.pop("release_authorization_sha256")
        db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(data), identifier))
    with pytest.raises(GateFailure, match="authorization"):
        release.advance(store, store.get(identifier))
    assert not any(call[0] in {"PUT", "deploy"} for call in calls)


def _review_v12_api_fixture(tmp_path, monkeypatch, callback):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=321, author_id=OWNER_USER_ID,
        text="synthetic fixture integrity directive")
    source = tmp_path / "source"
    source.mkdir()
    directory = store.root / "execution" / identifier
    directory.mkdir(parents=True)
    patch = directory / "fixture.patch"
    patch.write_text("synthetic inert patch", encoding="utf-8")
    profile = SimpleNamespace(source=source, origin="https://github.com/kirimine170/ephy-worker.git",
        github_repository="kirimine170/ephy-worker", branch="main", required_checks=("tests",))
    release = GitHubRelease(SimpleNamespace(state_dir=store.root, repositories={"ephy-worker": profile}))
    monkeypatch.setattr(release, "_proposal", lambda _: ({"repoRoot": str(source)}, patch, tmp_path / "candidate"))
    monkeypatch.setattr(release, "api", callback)
    store.transition(identifier, "review_pending", repository="ephy-worker", pr_number=9)
    _review_v11_release_fixture(release, store, identifier, monkeypatch, merged=False)
    assert store.get(identifier)["stage"] == "review_pending"
    assert not (directory / "release-evidence.json").exists()
    assert not (directory / "release-authorization.json").exists()
    return release


@pytest.mark.parametrize("response", [
    {"number": None}, {"number": "9"}, {"number": 77}, {"number": True},
    {"head": None}, {"head": []}, {"base": "wrong-shape"},
    {"head": {"sha": "f" * 40, "repo": None}},
    {"head": {"repo": []}}, {"head": {"repo": {"full_name": None}}},
    {"base": {"ref": "other-branch", "repo": {"full_name": "wrong/repository", "id": 17}}},
    {"number": 99, "head": {"sha": "f" * 40, "repo": {"full_name": "wrong/repo", "id": None}},
        "base": {"ref": "wrong", "repo": False}, "state": "closed", "merged": False, "fault": [None, "retained"]},
])
def test_review_v12_pr_fixture_preserves_existing_identity_and_fault_values(tmp_path, monkeypatch, response):
    original = copy.deepcopy(response)
    calls = []
    payload = {"sentinel": "original-callback-payload"}
    def callback(method, path, actual_payload=None):
        calls.append((method, path, actual_payload))
        return response
    release = _review_v12_api_fixture(tmp_path, monkeypatch, callback)
    path = "/repos/kirimine170/ephy-worker/pulls/9"
    returned = release.api("GET", path, payload)
    assert calls == [("GET", path, payload)] and calls[0][2] is payload
    assert response == original and returned is not response
    def preserve(before, after):
        if isinstance(before, dict):
            assert isinstance(after, dict)
            for key, value in before.items():
                assert key in after
                preserve(value, after[key])
        else:
            assert type(before) is type(after) and before == after
    preserve(original, returned)
    def paths(value, prefix=""):
        result = set()
        if isinstance(value, dict):
            for key, item in value.items():
                current = prefix + "." + key
                result.add(current)
                result.update(paths(item, current))
        return result
    assert paths(returned) - paths(original) <= {".number", ".head", ".base", ".head.repo", ".base.repo",
        ".head.repo.full_name", ".base.repo.full_name"}
    with pytest.raises((GateFailure, AttributeError, TypeError)):
        release._pr_identity(returned, "kirimine170/ephy-worker", 9, HEAD)


def test_review_v12_pr_fixture_fills_only_missing_identity_without_mutating_original(tmp_path, monkeypatch):
    response = {"head": {"sha": HEAD, "other": [1, None]}, "base": {"ref": "main"},
        "state": "open", "merged": False, "sentinel": "unchanged"}
    original = copy.deepcopy(response)
    calls = []
    def callback(*args):
        calls.append(args)
        return response
    release = _review_v12_api_fixture(tmp_path, monkeypatch, callback)
    path = "/repos/kirimine170/ephy-worker/pulls/9"
    result = release.api("GET", path)
    assert calls == [("GET", path, None)]
    assert response == original
    assert result == {**original, "number": 9,
        "head": {**original["head"], "repo": {"full_name": "kirimine170/ephy-worker"}},
        "base": {**original["base"], "repo": {"full_name": "kirimine170/ephy-worker"}}}
    release._pr_identity(result, "kirimine170/ephy-worker", 9, HEAD)


@pytest.mark.parametrize("text", ["修正して，マージしないで", "本番には反映しないで修正して", "コミットはしないで修正して",
    "再起動はしないで", "fix without push", "fix it; do not deploy", "proposal-only"])
def test_review_v13_canonical_directive_limits_precede_all_release_side_effects(tmp_path, monkeypatch, text):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(guild_id=11, channel_id=20, message_id=100, author_id=OWNER_USER_ID, text=text)
    release = GitHubRelease(SimpleNamespace(guild_id=11, channel_ids=(20,), state_dir=store.root))
    calls = []
    monkeypatch.setattr(release, "api", lambda *_: calls.append("api"))
    monkeypatch.setattr(release, "git", lambda *_: calls.append("git"))
    monkeypatch.setattr(release, "_proposal", lambda *_: calls.append("proposal"))
    with pytest.raises(GateFailure, match="request_release_restricted"):
        release.advance(store, store.get(identifier))
    assert calls == [] and store.get(identifier)["text"] == text


@pytest.mark.parametrize("method,path,response", [
    ("POST", "/repos/kirimine170/ephy-worker/pulls/9", {"number": None}),
    ("PUT", "/repos/kirimine170/ephy-worker/pulls/9/merge", {"merged": False}),
    ("GET", "/repos/kirimine170/ephy-worker/issues/9", {"number": None}),
    ("GET", "/repos/kirimine170/ephy-worker/pulls/9", [None, {"number": 77}]),
    ("GET", "/repos/kirimine170/ephy-worker/pulls/9", None),
    ("GET", "/repos/kirimine170/ephy-worker/pulls/9", "wrong-result"),
])
def test_review_v12_nonmatching_api_fixture_returns_original_object_and_calls_once(tmp_path, monkeypatch, method, path, response):
    calls = []
    payload = {"original": True}
    def callback(*args):
        calls.append(args)
        return response
    release = _review_v12_api_fixture(tmp_path, monkeypatch, callback)
    assert release.api(method, path, payload) is response
    assert calls == [(method, path, payload)] and calls[0][2] is payload


def test_review_v12_original_api_exception_and_call_arguments_are_unchanged(tmp_path, monkeypatch):
    calls = []
    original_error = GateFailure("synthetic original callback failure")
    def callback(*args):
        calls.append(args)
        raise original_error
    release = _review_v12_api_fixture(tmp_path, monkeypatch, callback)
    path = "/repos/kirimine170/ephy-worker/pulls/9"
    with pytest.raises(GateFailure) as caught:
        release.api("GET", path)
    assert caught.value is original_error
    assert calls == [("GET", path, None)]
