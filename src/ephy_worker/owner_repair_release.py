"""Separate integration/release role；models never receive its credentials．"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from pathlib import Path

import httpx

from .formal_artifacts import GateFailure, digest, encode, file_hash, read_json, safe_path, write_json
from .formal_runtime import command_environment, snapshot
from .owner_repair import OWNER_USER_ID
from .search import QueryPrivacyError, validate_public_text

SHA = re.compile(r"[0-9a-f]{40}\Z")
REVIEWERS = {"chatgpt-codex-connector[bot]", "chatgpt-codex-connector"}


class ReviewPending(RuntimeError):
    pass


class DeploymentPending(RuntimeError):
    pass


def public_evidence(job: dict, frozen: dict, patch: Path, head: str, tree: str) -> dict:
    """Export reviewable measurements with a strict field allowlist, never sessions．"""
    directory = Path(frozen["jobDir"])
    bundle = directory / "audit-bundle"
    contract = frozen["contract"]
    checker_root = Path(contract["checker"]).parent
    roots = {str(directory): "JOB", frozen["repoRoot"]: "REPOSITORY",
        frozen["worktreePath"]: "CANDIDATE", str(checker_root): "CHECKER_ROOT",
        frozen["runtime"]["governance_root"]: "FRAMEWORK"}
    for name, value in frozen["runtime"].items():
        if isinstance(value, str) and Path(value).is_absolute():
            roots.setdefault(value, "RUNTIME_" + name.upper())
    def normalise(value):
        for original, replacement in sorted(roots.items(), key=lambda pair: len(pair[0]), reverse=True):
            for variant in {original, original.replace("\\", "/"), original.replace("/", "\\")}:
                value = value.replace(variant, replacement)
        return value
    def output(name: str, expected_hash: str) -> dict:
        path = safe_path(directory, name)
        if file_hash(path) != expected_hash:
            raise GateFailure("public_execution_output_binding_changed")
        text = normalise(path.read_text(encoding="utf-8"))
        if len(text) > 32768:
            raise GateFailure("public_execution_output_too_large")
        if "Traceback (most recent call last)" in text:
            # Expected baseline/control rejection can include a private stack．
            # Publish its actual final diagnostic, clearly labelled as an excerpt．
            lines = [line for line in text.splitlines() if line.strip()]
            return {"raw_sha256": expected_hash, "format": "normalised final diagnostic excerpt",
                "text": lines[-1] if lines else ""}
        return {"raw_sha256": expected_hash, "format": "complete normalised output", "text": text}
    def measurement(result: dict) -> dict:
        records = []
        for check in result["checks"]:
            records.append({"id": check["id"], "argv": [normalise(arg) for arg in check["argv"]],
                "exit_code": check["exit_code"], "passed": check["passed"],
                "stdout": output(check["stdout"], check["stdout_sha256"]),
                "stderr": output(check["stderr"], check["stderr_sha256"])})
        return {"passed": result["passed"], "baseline": result["baseline"],
            "snapshot_sha256": result["snapshot_sha256"], "verifier_identity": result["verifier_identity"],
            "changed_files": result["changed_files"], "checks": records}
    provenance = read_json(bundle / "model_provenance.txt")
    attestation = read_json(directory / "audit-execution-attestation.json")
    provenance.append(attestation["observed_auditor"])
    public_provenance = []
    for entry in provenance:
        ack = entry["governance_ack"]
        details = ack["details"]
        public_provenance.append({key: entry[key] for key in ("role", "model_id", "identity", "pid",
            "session_sha256", "trace_sha256", "trace_valid", "requests", "output_tokens")})
        public_provenance[-1]["governance_ack"] = {"kind": ack["kind"], "is_error": ack.get("isError", False),
            "details": {key: details[key] for key in ("role", "acknowledged", "policyId", "policySha256", "terminalMarker")
                if key in details}}
        with safe_path(directory, entry["session_path"]).open(encoding="utf-8") as stream:
            header = json.loads(stream.readline())
        if header.get("type") != "session" or not isinstance(header.get("id"), str):
            raise GateFailure("public_session_identity_unavailable")
        public_provenance[-1]["session_id"] = header["id"]
    audit = read_json(directory / "audit-result.json")
    manifest = read_json(bundle / "evidence-manifest.json")
    discovery = read_json(checker_root / "discovery.json")
    guard = read_json(checker_root / "checker-guard.json")
    environment = read_json(bundle / "environment_contract.txt")
    repository = "kirimine170/" + job["data"]["repository"]
    if repository not in {"kirimine170/ephy-worker", "kirimine170/ephy-runtime"}:
        raise GateFailure("public_repository_identity_invalid")
    for path in (checker_root / "acceptance.py", checker_root / "checker-guard.json", checker_root / "discovery.json"):
        if contract["runtime_hashes"].get(str(path)) != file_hash(path):
            raise GateFailure("public_checker_dependency_changed")
    if file_hash(Path(contract["checker"])) != contract["checker_sha256"]:
        raise GateFailure("public_checker_source_changed")
    controls = read_json(Path(contract["checker_controls"]))
    control_records = {}
    for control in controls:
        control_path = checker_root / ("control-" + control["name"] + ".json")
        if file_hash(control_path) != control["evidence_sha256"]:
            raise GateFailure("public_control_evidence_changed")
        measured = read_json(control_path)
        if digest(encode(measured["specimen_snapshot"])) != control["specimen_snapshot_sha256"]:
            raise GateFailure("public_control_specimen_binding_changed")
        control_records[control["name"]] = measured
    control_changes = {}
    for control in ("known_good", "wrong_answer"):
        control_changes[control] = {}
        for name, replacement in discovery["prepared"][control].items():
            base_path = safe_path(Path(frozen["repoRoot"]), name, missing=True)
            original = base_path.read_text(encoding="utf-8") if base_path.exists() else ""
            expected = discovery["source_sha256"][name]
            if (file_hash(base_path) if base_path.exists() else None) != expected:
                raise GateFailure("public_control_base_changed")
            control_changes[control][name] = {"base_sha256": expected,
                "expanded_sha256": digest(replacement.encode()), "unified_diff": "".join(difflib.unified_diff(
                    original.splitlines(keepends=True), replacement.splitlines(keepends=True),
                    fromfile="a/" + name, tofile="b/" + name, n=0))}
            if control_records[control]["specimen_snapshot"].get(name) != digest(replacement.encode()):
                raise GateFailure("public_control_replacement_binding_changed")
    report = {"schema": "ephy.owner-repair-public-evidence.v1", "normalisation":
        "This public report is a field-selected, path-normalised derivative. Its bytes and SHA-256 differ "
        "from private raw artifacts. No model prompts, chain of thought, session bodies or Discord directives are included.",
        "public_goal": contract["semantic_scope"], "base": frozen["baseRevision"], "head": head, "tree": tree,
        "source_links": {"head": "https://github.com/" + repository + "/commit/" + head,
            "current_head_ci": "https://github.com/" + repository + "/commit/" + head + "/checks",
            "base_files": {name: "https://github.com/" + repository + "/blob/" + frozen["baseRevision"] + "/" + name
                for name in contract["allowed_files"]}},
        "allowed_files": contract["allowed_files"], "patch_sha256": file_hash(patch),
        "environment": {key: environment[key] for key in ("python_version", "dependencies", "offline", "locale")},
        "executable_verification": {"profile": contract["execution_profile"],
            "network_policy": contract["verifier_sandbox"]["network_policy"],
            "capability_sha256": contract["verifier_sandbox"]["capability"]["sha256"],
            "limitations": ("The selected external-network-denied Windows profile permits loopback access."
                if contract["verifier_sandbox"]["network_policy"] == "windows_external_network_denied"
                else "Network denial is bound to the measured frozen capability controls.")},
        "final_bindings": read_json(bundle / "audit-input.json")["final_bindings"],
        "candidate_files": read_json(bundle / "candidate_snapshot_manifest.txt"),
        "baseline": measurement(read_json(bundle / "preflight_result.txt")),
        "final": measurement(read_json(bundle / "verification_results.txt")),
        "checker": {"entry_source": Path(contract["checker"]).read_text(encoding="utf-8"),
            "source_text_format": "UTF-8 source text with universal-newline decoding; SHA-256 values bind the original stored bytes.",
            "entry_sha256": contract["checker_sha256"], "acceptance_source":
                (checker_root / "acceptance.py").read_text(encoding="utf-8"),
            "acceptance_sha256": file_hash(checker_root / "acceptance.py"),
            "guard_sha256": file_hash(checker_root / "checker-guard.json"),
            "guard_recipe": "Rebuild original tests and authority AST with entry_source against the public base commit; "
                "the guard snapshot is the public base's complete tracked-file SHA-256 map.",
            "guard_allowed": guard["allowed"], "original_test_files": list(guard["tests"]),
            "authority_files": list(guard["authority"]),
            "control_changes": control_changes,
            "control_recipe": "Decode the bound public base as UTF-8 with universal newlines; apply the listed scoped "
                "unified diffs and encode UTF-8 without newline translation. unchanged=public base; "
                "known_good/wrong_answer=the expanded scoped replacements; "
                "test_weakening=known_good with its first tests file replaced by '# original tests removed'; "
                "scope_escape=known_good plus out-of-scope.txt.",
            "controls": controls},
        "policy_and_artifact_manifest": [{key: entry[key] for key in ("artifact_id", "sha256", "size_bytes")}
            for entry in manifest["artifacts"]], "model_roles": public_provenance,
        "audit": {key: audit[key] for key in ("decision", "reason_codes", "bound_inputs", "integrity", "candidate", "workflow")},
        "audit_attestation": {key: attestation[key] for key in ("schema_valid", "candidate_unchanged", "bundle_unchanged",
            "passed", "proposal_stopped", "audit_result_sha256", "audit_input_sha256", "post_audit_workflow_sha256")}}
    # Audit summaries and citations may contain absolute paths；normalise strings
    # there without rewriting executable checker/control source．
    def normalise_nested(value):
        if isinstance(value, str):
            return normalise(value)
        if isinstance(value, list):
            return [normalise_nested(item) for item in value]
        if isinstance(value, dict):
            return {key: normalise_nested(item) for key, item in value.items()}
        return value
    report["audit"] = normalise_nested(report["audit"])
    private = [value for value in (job["text"], job.get("previous")) if isinstance(value, str) and value]
    def inspect_strings(value):
        if isinstance(value, str):
            if any(raw in value for raw in private) or re.search(r"</?(?:think|analysis|reasoning)\b", value, re.IGNORECASE):
                raise GateFailure("private_material_in_public_evidence")
            if value.strip():
                try:
                    validate_public_text(value, max_chars=524288)
                except QueryPrivacyError:
                    raise GateFailure("unsafe_public_evidence") from None
        elif isinstance(value, (list, tuple)):
            for item in value:
                inspect_strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                inspect_strings(item)
    inspect_strings(report)
    if len(encode(report)) > 524288:
        raise GateFailure("public_evidence_too_large")
    return report


def current_head_ready(head: str, checks: dict, comments: list, threads: dict,
                       required_checks: tuple[str, ...]) -> bool:
    """Only machine-readable evidence from the exact head can open the gate．"""
    if (not SHA.fullmatch(head) or not required_checks or not isinstance(checks.get("check_runs"), list)
            or type(checks.get("total_count")) is not int
            or checks["total_count"] != len(checks["check_runs"])
            or not isinstance(threads.get("nodes"), list)):
        return False
    runs = checks.get("check_runs", [])
    for name in required_checks:
        matches = [run for run in runs if run.get("name") == name and run.get("head_sha") == head]
        latest = max(matches, key=lambda run: run.get("id", 0), default={})
        if (latest.get("status") != "completed" or latest.get("conclusion") != "success"
                or latest.get("app", {}).get("slug") != "github-actions"):
            return False
    reviews = []
    for comment in comments:
        if (comment.get("user", {}).get("login") not in REVIEWERS
                or comment.get("user", {}).get("type") != "Bot"):
            continue
        match = re.search(r"<!-- codex-review-state:v1 (.*?) -->", comment.get("body", ""), re.DOTALL)
        if not match:
            continue
        try:
            state = json.loads(match.group(1))["code_review"]
        except (ValueError, KeyError, TypeError):
            continue
        if state.get("commit_sha") == head:
            reviews.append((comment.get("id", 0), state))
    review = max(reviews, key=lambda pair: pair[0], default=(0, {}))[1]
    if (review.get("status") != "completed" or review.get("has_unresolved_findings") is not False
            or threads.get("pageInfo", {}).get("hasNextPage") is not False):
        return False
    # Every unresolved thread blocks；this includes findings lacking severity labels．
    return all(thread.get("isResolved") is True for thread in threads.get("nodes", []))


class GitHubRelease:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.transport = transport

    def _operator(self, argv: list[str], cwd: Path, *, input_bytes: bytes | None = None,
                  timeout: int = 300) -> bytes:
        temporary = self.config.state_dir / "operator-temp"
        temporary.mkdir(parents=True, exist_ok=True)
        env = command_environment(cwd, temporary)
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(argv, cwd=cwd, env=env, input=input_bytes, capture_output=True,
            timeout=timeout, check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            # stderr may contain credential/helper details；keep the durable error nonsecret．
            raise GateFailure("operator_command_failed:" + Path(argv[0]).name)
        if len(result.stdout) > 4194304:
            raise GateFailure("operator_output_too_large")
        return result.stdout

    def git(self, root: Path, *args: str) -> bytes:
        helper = '!"' + self.config.gh.replace("\\", "/") + '" auth git-credential'
        return self._operator([self.config.git, "-c", "credential.helper=", "-c", "credential.helper=" + helper,
            "-C", str(root), *args], root)

    def api(self, method: str, path: str, payload: dict | None = None):
        token = self._operator([self.config.gh, "auth", "token", "--hostname", "github.com"],
            self.config.state_dir).decode().strip()
        if not token or len(token) > 512:
            raise GateFailure("github_operator_auth_unavailable")
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False, timeout=30) as client:
            response = client.request(method, "https://api.github.com" + path,
                headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"}, json=payload)
        if response.status_code not in {200, 201}:
            raise GateFailure("github_operator_request_failed:" + str(response.status_code))
        if len(response.content) > 4194304:
            raise GateFailure("github_response_too_large")
        return response.json()

    def _proposal(self, job: dict) -> tuple[dict, Path, Path]:
        if int(job["author"]) != OWNER_USER_ID:
            raise GateFailure("release_owner_authorization_missing")
        formal = Path(job["data"]["formal_job"])
        from .owner_repair_pipeline import verify_in_interpreter
        patch = verify_in_interpreter(formal, self.config.state_dir / "execution" / job["id"], git=self.config.git)
        frozen = read_json(formal)
        if job["data"].get("patch_sha256") not in {None, file_hash(patch)}:
            raise GateFailure("release_patch_changed")
        return frozen, patch, Path(frozen["worktreePath"])

    def request_owner_review(self, job: dict, repository: str, number: int, head: str, store) -> None:
        """The pinned host relay can request review；it cannot open a release gate．"""
        paths = [(getattr(self.config, name), getattr(self.config, name + "_sha256"))
            for name in ("review_adapter", "review_adapter_python", "review_adapter_config")]
        def check_pins():
            if any(file_hash(path) != pin for path, pin in paths):
                raise GateFailure("review_adapter_identity_changed")
        check_pins()
        if (int(job["author"]) != OWNER_USER_ID or repository not in
                {"kirimine170/ephy-worker", "kirimine170/ephy-runtime"}
                or type(number) is not int or number <= 0 or not SHA.fullmatch(head)
                or not re.fullmatch(r"repair-[0-9a-f]{32}", job["id"])):
            raise GateFailure("review_adapter_request_invalid")
        request = {"schema": "ephy.owner-review-request.v1", "repair_id": job["id"],
            "repository": repository, "pr_number": number, "head_sha": head}
        directory = self.config.state_dir / "reviews" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        request_file = directory / "review-request.json"
        if request_file.exists() and read_json(request_file) != request:
            raise GateFailure("review_request_binding_changed")
        write_json(request_file, request)
        try:
            output = self._operator([str(self.config.review_adapter_python), "-I", str(self.config.review_adapter),
                "--config", str(self.config.review_adapter_config), "--request", str(request_file)], directory, timeout=300)
        except (subprocess.TimeoutExpired, GateFailure):
            raise ReviewPending("owner_review_relay_not_confirmed") from None
        check_pins()
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        try:
            result = json.loads(output, object_pairs_hook=pairs)
        except (ValueError, UnicodeError):
            raise ReviewPending("owner_review_relay_result_invalid") from None
        expected = {**request, "schema": "ephy.owner-review-result.v1", "requested": True,
            "owner_id": 78987919, "owner_login": "kirimine170"}
        if (not isinstance(result, dict) or set(result) != set(expected) | {"comment_id", "deduplicated"}
                or any(result.get(key) != value or type(result.get(key)) is not type(value)
                    for key, value in expected.items())
                or type(result.get("comment_id")) is not int or result["comment_id"] <= 0
                or type(result.get("deduplicated")) is not bool):
            raise ReviewPending("owner_review_relay_result_binding_invalid")
        write_json(directory / "review-result.json", result)
        store.transition(job["id"], "review_pending", review_requested=True,
            owner_review_result_sha256=file_hash(directory / "review-result.json"))

    def publish_evidence(self, prefix: str, number: int, job: dict, report: dict, store) -> None:
        text = json.dumps(report, ensure_ascii=False, indent=2)
        public_hash = digest(text.encode())
        directory = self.config.state_dir / "execution" / job["id"]
        report_file = directory / "public-evidence.json"
        if report_file.exists() and report_file.read_text(encoding="utf-8") != text:
            raise GateFailure("public_evidence_changed_after_freeze")
        if not report_file.exists():
            report_file.write_text(text, encoding="utf-8", newline="")
        comments = self.api("GET", prefix + "/issues/" + str(number) + "/comments?per_page=100")
        if len(comments) >= 100:
            raise GateFailure("public_evidence_comment_pagination_required")
        comment_ids = []
        parts, current, size = [], [], 0
        for char in text:
            byte_count = len(char.encode("utf-8"))
            if size + byte_count > 45000:
                parts.append("".join(current))
                current, size = [], 0
            current.append(char)
            size += byte_count
        if current:
            parts.append("".join(current))
        for ordinal, part in enumerate(parts):
            marker = "<!-- ephy-owner-evidence:" + job["id"] + ":" + report["head"] + ":" + str(ordinal) + " -->"
            body = (marker + "\nPublic evidence " + str(ordinal + 1) + "/" + str(len(parts))
                + "; report SHA-256 `" + public_hash + "`; exact reviewed head `" + report["head"]
                + "`. Concatenate the JSON blocks in ordinal order to reconstruct the report.\n\n```json\n" + part + "\n```")
            existing = [comment for comment in comments if marker in comment.get("body", "")]
            if len(existing) > 1 or existing and existing[0].get("body") != body:
                raise GateFailure("public_evidence_comment_changed")
            comment = existing[0] if existing else self.api("POST", prefix + "/issues/" + str(number) + "/comments",
                {"body": body})
            comment_ids.append(comment["id"])
        store.transition(job["id"], "review_pending", public_evidence_sha256=public_hash,
            public_evidence_comments=comment_ids)

    def advance(self, store, job: dict) -> None:
        frozen, patch, candidate = self._proposal(job)
        profile = self.config.repositories[job["data"]["repository"]]
        if Path(frozen["repoRoot"]).resolve() != profile.source.resolve():
            raise GateFailure("release_repository_binding_changed")
        actual_origin = self.git(profile.source, "remote", "get-url", "origin").decode().strip()
        if actual_origin != profile.origin:
            raise GateFailure("release_origin_changed")
        directory = self.config.state_dir / "execution" / job["id"]
        branch = "ephy/" + job["id"]
        prefix = "/repos/" + profile.github_repository
        integration = directory / "integration"
        data = dict(job["data"])
        if "release_head" not in data:
            if integration.exists():
                # A crash during commit creation is reconciled from its exact parent/patch．
                head = self.git(integration, "rev-parse", "HEAD").decode().strip()
                if head == frozen["baseRevision"]:
                    raise GateFailure("interrupted_integration_retained")
            else:
                self.git(profile.source, "worktree", "add", "--detach", str(integration), frozen["baseRevision"])
                self.git(integration, "apply", "--index", "--binary", str(patch))
                if snapshot(integration) != snapshot(candidate):
                    raise GateFailure("integration_candidate_bytes_differ")
                self.git(integration, "-c", "user.name=Ephy", "-c", "user.email=ephy@users.noreply.github.com",
                    "commit", "-m", "Implement owner repair " + job["id"])
                head = self.git(integration, "rev-parse", "HEAD").decode().strip()
            parents = self.git(integration, "rev-list", "--parents", "-n", "1", head).decode().split()
            if parents != [head, frozen["baseRevision"]] or snapshot(integration) != snapshot(candidate):
                raise GateFailure("integration_commit_binding_failed")
            store.transition(job["id"], "release_pending", release_head=head, patch_sha256=file_hash(patch))
            data.update(release_head=head, patch_sha256=file_hash(patch))
        head = data["release_head"]
        if (self.git(integration, "rev-parse", "HEAD").decode().strip() != head
                or self.git(integration, "status", "--porcelain").strip()
                or snapshot(integration) != snapshot(candidate)):
            raise GateFailure("release_candidate_changed")
        tree = self.git(integration, "rev-parse", "HEAD^{tree}").decode().strip()
        if not SHA.fullmatch(tree):
            raise GateFailure("integration_tree_identity_invalid")
        published = directory / "public-evidence.json"
        if data.get("public_evidence_sha256"):
            text = published.read_text(encoding="utf-8")
            if digest(text.encode()) != data["public_evidence_sha256"]:
                raise GateFailure("frozen_public_evidence_changed")
            report = json.loads(text)
            if (report.get("head") != head or report.get("tree") != tree
                    or report.get("patch_sha256") != file_hash(patch) or report.get("base") != frozen["baseRevision"]):
                raise GateFailure("frozen_public_evidence_binding_changed")
        else:
            report = public_evidence(job, frozen, patch, head, tree)
        # Complete safety/measurement export happens before publishing source．
        if "pr_number" not in data:
            self.git(integration, "push", "origin", head + ":refs/heads/" + branch)
            owner = profile.github_repository.split("/")[0]
            existing = self.api("GET", prefix + "/pulls?state=all&head=" + owner + ":" + branch)
            if len(existing) > 1:
                raise GateFailure("ambiguous_release_pull_request")
            pr = existing[0] if existing else self.api("POST", prefix + "/pulls", {
                "title": "Ephy owner repair " + job["id"], "head": branch, "base": profile.branch,
                "body": "Owner-authorized natural-language repair.\n\n"
                    + "Formal proposal: `" + frozen["id"] + "`\n"
                    + "Audited patch SHA-256: `" + file_hash(patch) + "`\n"
                    + "Final head: `" + head + "`\n"
                    + "Scope: " + ", ".join(frozen["contract"]["allowed_files"])
                    + "\n\nManaged Pi implementation, isolated fixed verification and fresh read-only audit completed. "
                    + "Merge remains gated on this head's CI and independent Codex Review."})
            if pr.get("head", {}).get("sha") != head:
                raise GateFailure("pull_request_head_mismatch")
            store.transition(job["id"], "review_pending", pr_number=pr["number"], pr_url=pr["html_url"])
            data.update(pr_number=pr["number"], pr_url=pr["html_url"])
        number = data["pr_number"]
        self.publish_evidence(prefix, number, job, report, store)
        pr = self.api("GET", prefix + "/pulls/" + str(number))
        if pr.get("head", {}).get("sha") != head or pr.get("base", {}).get("ref") != profile.branch:
            raise GateFailure("reviewed_pull_request_identity_changed")
        if not pr.get("merged"):
            if pr.get("state") != "open":
                raise GateFailure("repair_pull_request_closed")
            if not data.get("review_requested"):
                # GitHub's automatic reviewer may also run；lack of entitlement stays pending．
                if getattr(self.config, "review_adapter", None) is not None:
                    self.request_owner_review(job, profile.github_repository, number, head, store)
                else:
                    self.api("POST", prefix + "/issues/" + str(number) + "/comments", {"body":
                        "@codex review\n\nReview only current head `" + head + "`. Apply AGENTS.md Code Review Rules. "
                        + "Patch SHA-256 `" + file_hash(patch) + "`; scope "
                        + ", ".join(frozen["contract"]["allowed_files"])
                        + ". Check unchanged assertions, scope, role boundaries and exact-head evidence. "
                        + "Do not edit, commit or merge. Missing evidence is blocking."})
                    store.transition(job["id"], "review_pending", review_requested=True)
            checks = self.api("GET", prefix + "/commits/" + head + "/check-runs?per_page=100")
            comments = self.api("GET", prefix + "/issues/" + str(number) + "/comments?per_page=100")
            owner, name = profile.github_repository.split("/")
            query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved} pageInfo{hasNextPage}}}}}"
            response = self.api("POST", "/graphql", {"query": query,
                "variables": {"owner": owner, "name": name, "number": number}})
            try:
                threads = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (KeyError, TypeError):
                raise GateFailure("review_thread_evidence_unavailable") from None
            if len(comments) >= 100 or not current_head_ready(head, checks, comments, threads, profile.required_checks):
                raise ReviewPending("waiting_for_exact_head_ci_and_codex_review")
            self._proposal(store.get(job["id"]))
            evidence = directory / "release-evidence.json"
            write_json(evidence, {"head": head, "checks": checks, "comments": comments, "threads": threads})
            store.transition(job["id"], "merge_pending", evidence_head=head,
                release_evidence_sha256=file_hash(evidence))
            merged = self.api("PUT", prefix + "/pulls/" + str(number) + "/merge",
                {"sha": head, "merge_method": "squash"})
            if merged.get("merged") is not True or not SHA.fullmatch(merged.get("sha", "")):
                raise GateFailure("github_merge_not_confirmed")
            pr = self.api("GET", prefix + "/pulls/" + str(number))
        current = store.get(job["id"])["data"]
        evidence = directory / "release-evidence.json"
        if (current.get("evidence_head") != head or not evidence.is_file()
                or current.get("release_evidence_sha256") != file_hash(evidence)):
            raise GateFailure("merged_without_frozen_release_evidence")
        saved = read_json(evidence)
        if saved.get("head") != head or not current_head_ready(head, saved["checks"], saved["comments"],
                saved["threads"], profile.required_checks):
            raise GateFailure("merged_release_evidence_invalid")
        commit = pr.get("merge_commit_sha", "")
        if not pr.get("merged") or not SHA.fullmatch(commit):
            raise GateFailure("merged_commit_identity_missing")
        store.transition(job["id"], "deployment_pending", merged_commit=commit)
        adapter = self.config.release_adapter
        if file_hash(adapter) != self.config.release_adapter_sha256:
            raise GateFailure("release_adapter_changed")
        request = self.config.state_dir / "releases" / job["id"] / "release-request.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        write_json(request, {"schema": "ephy.owner-repair-release.v1", "repair_id": job["id"],
            "owner_user_id": OWNER_USER_ID, "repository": profile.github_repository,
            "deployed_commit": commit, "pr_number": number, "head_sha": head})
        try:
            output = self._operator([self.config.powershell, "-NoProfile", "-NonInteractive", "-File",
                str(adapter), "-RepairRequest", str(request)], directory, timeout=7200)
        except subprocess.TimeoutExpired:
            raise DeploymentPending("deployment_callback_result_not_yet_confirmed") from None
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise GateFailure("release_adapter_result_invalid") from None
        write_json(directory / "release-result.json", result)
        if (result.get("deployed_commit") != commit or result.get("healthy") is not True
                or result.get("model_identity_unchanged") is not True):
            raise GateFailure("production_deployment_not_verified")
        # Persist the result before the bot's independent outbox drain can send it．
        store.finish(job["id"], "completed", "修正とテスト，独立レビューを終え，本番へ反映して再起動しました．\n"
            + "変更内容と検証結果はこちらです：" + data["pr_url"], deployed_commit=commit,
            release_result_sha256=file_hash(directory / "release-result.json"))
