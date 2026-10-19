"""Pinned, unchanged governance resources and host-side RPC acknowledgement checks.

This integrates an ordinary read-only desktop task. It is not a formal audit or
self-improvement workflow, and it grants no additional model tools.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from ..coding_executor import CodingExecutionError

POLICY_ID = "ephy.system-development-governance.v1"
END_MARKER = "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1"
GATE_PATH = ".pi/extensions/governance-gate.ts"
POLICY_PATH = "docs/system-development-governance.md"
PINNED_HASHES = {
    GATE_PATH: "2b30673ac73b9d861f67f00caba981c4bd677915be2082bd53fe008135e46da1",
    "tools/pi-local/strata-provider.ts": "37f0d110f1395755bfb07d1df4733f78b5d8a9ef2f9d0b3e604c6df982ac80a0",
    "tools/pi-local/formal-stage-guard.ts": "8665a2ee041e6de01a9e4b500583b46ab3dad3ee932e59e6c9942de440c3433b",
    "tools/pi-local/formal-stage-stop.ts": "fd953b62358b95ccf327c0b497ea5353ad12a26654835c793e68a5a95c43ef0f",
    POLICY_PATH: "182a113709764e505c25e37909ce685333c9bc513688b0845ec3520d680229b1",
    "docs/self-improvement-mvp.md": "58f6ede28428ab6ea3daf8b0115d875eaaeb153a2a9d86769e968f55fbddf618",
    ".agents/skills/ephy-worker-self-improvement/SKILL.md":
        "64dc0b0b87d853746391897b852b22cf282d28abbf1ad645dd7066fefac4b0f4",
    ".agents/skills/ephy-worker-self-improvement/references/eval-contract.md":
        "5581be75186e3757871b14f819ab7b3eaca75642ab2ebc3de18d9953c1b7f5f4",
    ".agents/skills/ephy-worker-self-improvement/references/audit-contract.md":
        "d16437ac92a56210a2a797a022a88ea1a58b215d69cdbf8fea1d8215ed6e770d",
    ".pi/prompts/audit-ephy-worker.md": "3d96ee749e4ccbd0f98bf67afa21d04fe78a03fae79d3ca5a6f0358b532db69b",
}
CONTEXT_PATHS = tuple(PINNED_HASHES)[5:]
LIMITS = {"role": "planner", "allowed_files": [], "provider_id": "strata-local", "max_requests": 8,
          "output_token_budget": 8192, "max_response_tokens": 2048, "wall_seconds": 240,
          "agent_retries": 0, "provider_retries": 0}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _regular(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("governance resource must be an ordinary unlinked file")
    if info.st_size > 128 * 1024:
        raise ValueError("governance resource exceeds the bounded file size")


def validate_runtime(root: Path | None = None) -> dict:
    """Read-only verification usable by source and frozen-distribution builders."""
    root = (root or Path(__file__).with_name("governance_runtime")).absolute()
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("governance runtime contains a link or reparse point")
    present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if present != set(PINNED_HASHES) | {"manifest.json"}:
        raise ValueError("governance runtime file set differs from the pinned manifest")
    _regular(root / "manifest.json")
    manifest = json.loads((root / "manifest.json").read_bytes().decode("utf-8"))
    expected = []
    for name, digest in PINNED_HASHES.items():
        path = root / name
        _regular(path)
        data = path.read_bytes()
        if _sha(data) != digest:
            raise ValueError(f"governance resource hash mismatch: {name}")
        data.decode("utf-8", errors="strict")
        expected.append({"path": name, "sha256": digest, "bytes": len(data)})
    if manifest != {"version": 1, "files": expected}:
        raise ValueError("governance manifest does not match pinned bytes")
    return manifest


class GovernanceSession:
    """Own one snapshot and verify actual final RPC events, never model prose."""

    def __init__(self, root: Path, config):
        origin = urlsplit(config.endpoint)
        if (config.provider != "strata-local" or config.models_file is not None
                or origin.scheme != "http" or origin.hostname != "127.0.0.1"
                or not origin.port or origin.path.rstrip("/") != "/v1"):
            raise ValueError("Live desktop requires provider strata-local and http://127.0.0.1:PORT/v1; "
                             "custom models_file is unsupported")
        source = Path(__file__).with_name("governance_runtime")
        manifest = validate_runtime(source)
        self.root = root / "controller"
        self.root.mkdir()
        self.runtime = self.root / "runtime"
        for item in [*manifest["files"], {"path": "manifest.json"}]:
            target = self.runtime / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / item["path"]).read_bytes())
        validate_runtime(self.runtime)
        self.context = []
        for name in CONTEXT_PATHS:
            data = (self.runtime / name).read_bytes()
            self.context.append({"path": name, "sha256": _sha(data), "bytes": len(data),
                                 "lines": len(re.split(r"\r?\n", data.decode("utf-8")))})
        self.manifest = {
            "version": 1, "role": "planner", "runtime_root": str(self.runtime),
            "gate_path": str(self.runtime / GATE_PATH), "gate_sha256": PINNED_HASHES[GATE_PATH],
            "policy_path": str(self.runtime / POLICY_PATH), "policy_sha256": PINNED_HASHES[POLICY_PATH],
            "context_root": str(self.runtime), "files": manifest["files"], "context_manifest": self.context,
        }
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(_json(self.manifest), encoding="utf-8")
        self.manifest_hash = _sha(self.manifest_path.read_bytes())
        self.identity_path = self.root / "provider-identity.json"
        self.identity_path.write_text(_json({"base_url": f"http://127.0.0.1:{origin.port}",
                                            "model_id": config.model_id, "context": config.context_window}),
                                      encoding="utf-8")
        self.stage_path = self.root / "stage-config.json"
        self.trace_path = self.root / "stage-trace.jsonl"
        self.config = config
        self.nonce = None
        self.ack_call_id = None
        self.ack_started = False
        self.ack_verified = False
        self.final_answer = False
        self.settled = False
        self.failed = None
        self.stage_verified = False
        self.resources_checked_after = False
        self.read_calls = 0
        self.owned_files = {self.manifest_path: self.manifest_hash,
                            self.identity_path: _sha(self.identity_path.read_bytes())}

    @property
    def environment(self) -> dict[str, str]:
        return {
            "DUAL_GOVERNANCE_ROLE": "planner", "DUAL_GOVERNANCE_POLICY": str(self.runtime / POLICY_PATH),
            "DUAL_GOVERNANCE_CONTEXT_ROOT": str(self.runtime), "DUAL_JOB_RUNNER": "",
            "DUAL_RUNNER_SMOKE_TEST": "", "DUAL_AUDIT_BUNDLE_ROOT": "",
            "EPHY_STRATA_IDENTITY": str(self.identity_path), "EPHY_FORMAL_STAGE_CONFIG": str(self.stage_path),
            "EPHY_FORMAL_TRACE": str(self.trace_path),
            "EPHY_DESKTOP_GOVERNANCE_MANIFEST": str(self.manifest_path),
            "EPHY_DESKTOP_GOVERNANCE_MANIFEST_SHA256": self.manifest_hash,
        }

    def prepare(self, worktree: Path):
        self.stage_path.write_text(_json({**LIMITS, "root": str(worktree.resolve()),
                                         "model_id": self.config.model_id}), encoding="utf-8")
        self.owned_files[self.stage_path] = _sha(self.stage_path.read_bytes())
        self.check_hashes()

    def check_hashes(self):
        try:
            validate_runtime()
            validate_runtime(self.runtime)
            for path, digest in self.owned_files.items():
                _regular(path)
                if _sha(path.read_bytes()) != digest:
                    self.reject("controller resource changed during execution")
        except (OSError, ValueError):
            self.reject("pinned governance resource unavailable or changed during execution")

    def reject(self, reason: str):
        self.failed = self.failed or reason
        raise CodingExecutionError("pi_rpc_failure", "agent", f"governance: {self.failed}")

    def expected_bundle(self) -> list[dict]:
        documents = []
        for document in self.context:
            name, digest = document["path"], document["sha256"]
            documents.append("\n".join([
                (f"BEGIN REQUIRED GOVERNANCE DOCUMENT path={name} sha256={digest} "
                 f"bytes={document['bytes']} lines={document['lines']}"),
                (self.runtime / name).read_bytes().decode("utf-8"),
                f"END REQUIRED GOVERNANCE DOCUMENT path={name} sha256={digest}",
            ]))
        text = "\n".join([
            (f"Governance acknowledged: {POLICY_ID} {PINNED_HASHES[POLICY_PATH]} "
             "role=planner phase=context_delivery_pending"),
            f"BEGIN REQUIRED GOVERNANCE CONTEXT BUNDLE {_json(self.context)}", *documents,
            f"END REQUIRED GOVERNANCE CONTEXT BUNDLE total={len(documents)}",
        ])
        return [{"type": "text", "text": text}]

    def observe(self, event: dict):
        if self.failed:
            self.reject(self.failed)
        kind = event.get("type")
        if kind == "ephy_desktop_governance_envelope":
            if (self.nonce is not None or event.get("version") != 1 or event.get("role") != "planner"
                    or event.get("policy_sha256") != PINNED_HASHES[POLICY_PATH]
                    or event.get("context_manifest") != self.context
                    or not isinstance(event.get("nonce"), str)
                    or not re.fullmatch(r"[0-9a-f-]{36}", event["nonce"])):
                self.reject("invalid governance envelope observation")
            self.nonce = event["nonce"]
        elif kind == "extension_error" or kind == "auto_retry_start":
            self.reject("extension failure or disallowed retry")
        elif kind == "agent_end" and event.get("willRetry"):
            self.reject("automatic retry is outside the fixed stage contract")
        elif kind in {"turn_start", "message_start"}:
            if kind == "turn_start" or event.get("message", {}).get("role") == "assistant":
                self.final_answer = False
        elif kind == "message_end" and event.get("message", {}).get("role") == "assistant":
            message = event["message"]
            content = message.get("content", [])
            if (len(content) == 1 and content[0].get("type") == "text"
                    and content[0].get("text", "").startswith("GOVERNANCE_GATE_FAILURE: ")
                    and content[0].get("text", "").endswith(
                        "This session is permanently latched; start a fresh session."
                    )):
                self.reject("canonical gate rejected the acknowledgement or session")
            if message.get("stopReason") in {"error", "aborted", "length"}:
                self.reject("provider response failed or exceeded its response limit")
            if message.get("provider") != "strata-local" or message.get("model") != self.config.model_id:
                self.reject("assistant model identity mismatch")
            if not self.ack_verified:
                expected = {"policyId": POLICY_ID, "policySha256": PINNED_HASHES[POLICY_PATH],
                            "endMarker": END_MARKER, "nonce": self.nonce, "role": "planner"}
                if (not self.nonce or self.ack_call_id or len(content) != 1
                        or content[0].get("type") != "toolCall" or content[0].get("name") != "governance_ack"
                        or not isinstance(content[0].get("id"), str) or not content[0]["id"]
                        or content[0].get("arguments") != expected or message.get("stopReason") != "toolUse"):
                    self.reject("assistant did not produce the exact one-shot acknowledgement")
                self.ack_call_id = content[0]["id"]
            else:
                self.final_answer = message.get("stopReason") == "stop" and any(
                    item.get("type") == "text" and bool(item.get("text", "").strip())
                    for item in content if isinstance(item, dict)
                )
        elif kind == "tool_execution_start":
            name = event.get("toolName")
            if name == "governance_ack":
                if (self.ack_verified or self.ack_started or not self.ack_call_id
                        or event.get("toolCallId") != self.ack_call_id):
                    self.reject("unexpected acknowledgement tool execution")
                self.ack_started = True
            elif name == "read" and self.ack_verified:
                self.read_calls += 1
            else:
                self.reject("tool executed before verified acknowledgement or outside read-only scope")
        elif kind == "tool_execution_end":
            if event.get("toolName") == "governance_ack":
                result = event.get("result", {})
                details = result.get("details", {})
                context = details.get("governanceContext", {})
                if (self.ack_verified or not self.ack_started or not self.ack_call_id
                        or event.get("toolCallId") != self.ack_call_id
                        or event.get("isError") is not False
                        or details.get("policyId") != POLICY_ID
                        or details.get("policySha256") != PINNED_HASHES[POLICY_PATH]
                        or details.get("endMarker") != END_MARKER or details.get("nonce") != self.nonce
                        or details.get("role") != "planner" or details.get("acknowledged") is not True
                        or details.get("phase") != "ready" or details.get("activeTools") != ["read"]
                        or details.get("requiredContext") != self.context or context.get("complete") is not True
                        or context.get("documents") != self.context or context.get("activeTools") != ["read"]
                        or result.get("content") != self.expected_bundle()):
                    self.reject("final acknowledgement or context delivery receipt is invalid")
                self.ack_verified = True
            elif event.get("toolName") != "read" or not self.ack_verified or event.get("isError"):
                self.reject("read tool failed or an unpermitted tool completed")
        elif kind == "agent_settled":
            if not self.ack_verified or not self.final_answer:
                self.reject("session settled without verified acknowledgement and a final task answer")
            self.settled = True

    def complete(self):
        self.check_hashes()
        self.resources_checked_after = True
        if self.failed or not self.settled:
            self.reject("session did not settle with a verified final answer")
        if not self.trace_path.is_file() or self.trace_path.stat().st_size > 1024 * 1024:
            self.reject("bounded controller trace is missing")
        entries = [json.loads(line) for line in self.trace_path.read_text(encoding="utf-8").splitlines() if line]
        if any(item.get("kind") in {"violation", "hard_stop"}
               or (item.get("kind") == "stage_end" and item.get("failed") is not False) for item in entries):
            self.reject("controller trace records a rejected or stopped stage")
        if not any(item.get("kind") == "stage_end" and item.get("failed") is False for item in entries):
            self.reject("controller stage completion evidence is missing")
        self.stage_verified = True

    def save(self, directory: Path):
        """Persist identities/status only; private model text stays in existing worker logs."""
        document = {"version": 1, "ordinary_read_only_task": True, "formal_audit": False,
                    "role": "planner", "policy_sha256": PINNED_HASHES[POLICY_PATH],
                    "runtime_manifest_sha256": self.manifest_hash, "canonical_files": self.manifest["files"],
                    "context_manifest": self.context, "ack_verified": self.ack_verified,
                    "envelope_nonce": self.nonce, "ack_call_id": self.ack_call_id,
                    "post_ack_answer": self.final_answer,
                    "settled": self.settled, "failure": self.failed, "read_calls": self.read_calls,
                    "stage_verified": self.stage_verified, "resources_checked_after": self.resources_checked_after,
                    "controller_files": {path.name: digest for path, digest in self.owned_files.items()}}
        (directory / "governance-attestation.json").write_text(_json(document) + "\n", encoding="utf-8")
        for path in self.owned_files:
            if path.is_file() and path.stat().st_size <= 128 * 1024:
                (directory / f"governance-{path.name}").write_bytes(path.read_bytes())
        # The trace includes pinned public policy and read paths; no credential file is copied.
        if self.trace_path.is_file() and self.trace_path.stat().st_size <= 1024 * 1024:
            (directory / "governance-stage-trace.jsonl").write_bytes(self.trace_path.read_bytes())
