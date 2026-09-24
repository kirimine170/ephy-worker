from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POLICY_ID = "ephy.system-development-governance.v1"
POLICY_MARKER = "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1"
AUDIT_CONTRACT_ID = "ephy.independent-audit.v1"
AUDIT_MARKER = "END-OF-EPHY-INDEPENDENT-AUDIT-CONTRACT-V1"


def read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def valid_audit_result() -> dict[str, object]:
    digest = "a" * 64

    def evidence(artifact_id: str) -> dict[str, str]:
        return {"artifact_id": artifact_id, "sha256": digest, "location": "line 1"}

    def domain(check_id: str, artifact_id: str) -> dict[str, object]:
        return {
            "status": "PASS",
            "checks": [
                {
                    "check_id": check_id,
                    "status": "PASS",
                    "summary": "Verified from frozen evidence.",
                    "evidence": [evidence(artifact_id)],
                }
            ],
        }

    return {
        "schema_version": "ephy.audit-result.v1",
        "audit_id": "audit-test",
        "job_id": "job-test",
        "decision": "ACCEPT_PROPOSAL",
        "reason_codes": ["ALL_GATES_PASSED"],
        "bound_inputs": {
            name: digest
            for name in (
                "audit_input_sha256",
                "evidence_manifest_sha256",
                "prompt_template_sha256",
                "candidate_patch_sha256",
                "candidate_snapshot_manifest_sha256",
                "verification_results_sha256",
                "workflow_events_sha256",
                "model_provenance_sha256",
            )
        },
        "documents_read": [
            {"artifact_id": artifact_id, "sha256": digest}
            for artifact_id in (
                "system_development_policy",
                "task_spec",
                "required_skill",
                "evaluation_contract",
                "environment_contract",
                "audit_contract",
                "audit_prompt",
                "audit_input_schema",
                "evidence_manifest_schema",
                "audit_result_schema",
            )
        ],
        "integrity": domain("I01_MANIFEST", "evidence_manifest"),
        "candidate": domain("C01_SCOPE", "candidate_patch"),
        "workflow": domain("W01_SEQUENCE", "workflow_events"),
        "findings": [],
        "missing_or_invalid_evidence": [],
        "human_report": {
            "headline": "Proposal accepted",
            "summary": "All required evidence passed.",
            "candidate_summary": "Candidate checks passed.",
            "workflow_summary": "Workflow checks passed.",
            "blockers": [],
            "next_action": "Keep the result as an unapplied proposal.",
        },
        "auditor_assertions": {
            "repair_attempted": False,
            "write_attempted": False,
            "tests_rerun": False,
            "network_used": False,
            "subagent_used": False,
            "repository_or_release_action_attempted": False,
        },
    }


def powershell_schema_accepts(document: dict[str, object], schema_path: Path) -> bool:
    executable = shutil.which("pwsh")
    if executable is None:
        raise unittest.SkipTest("PowerShell 7 is required for JSON Schema behavior checks")
    command = (
        "$document = [Console]::In.ReadToEnd(); "
        "if ($document | Test-Json -SchemaFile $env:EPHY_AUDIT_SCHEMA "
        "-ErrorAction SilentlyContinue) { exit 0 }; exit 1"
    )
    environment = os.environ.copy()
    environment["EPHY_AUDIT_SCHEMA"] = str(schema_path)
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    return completed.returncode == 0


class GovernanceContractTests(unittest.TestCase):
    def test_canonical_policy_has_fixed_identity_and_complete_marker(self) -> None:
        policy = read("docs/system-development-governance.md")

        self.assertIn(POLICY_ID, policy)
        self.assertEqual(policy.rstrip().splitlines()[-1], POLICY_MARKER)
        self.assertIn("preflight", policy)
        self.assertIn("Qwen implementation", policy)
        self.assertIn("fresh gpt-oss audit", policy)
        self.assertIn("proposal stop", policy)
        self.assertIn("元のユーザー依頼と過去会話を非破壊的に退避", policy)
        self.assertIn("一つのmodel-visible tool result", policy)
        self.assertIn("raw streaming delta", policy)

    def test_project_context_routes_agents_to_canonical_policy(self) -> None:
        agents = read("AGENTS.md")
        bootstrap = read(".pi/APPEND_SYSTEM.md")
        orchestrator = read(".pi/orchestrator.md")

        self.assertIn(POLICY_ID, agents)
        self.assertIn("docs/system-development-governance.md", agents)
        self.assertIn(POLICY_ID, bootstrap)
        self.assertIn("governance_ack", bootstrap)
        self.assertIn("Managed-Runtime-Artifacts", orchestrator)
        self.assertIn("contract_incomplete", orchestrator)
        self.assertIn("governanceContext.complete=true", orchestrator)
        self.assertIn("Governance-Context-Root", orchestrator)
        self.assertIn("Do not require duplicate copies inside the candidate", orchestrator)
        self.assertEqual(
            bootstrap.rstrip().splitlines()[-1],
            "END-OF-EPHY-PI-GOVERNANCE-BOOTSTRAP-V1",
        )

    def test_codex_review_bridge_is_current_head_bound_and_non_merging(self) -> None:
        agents = read("AGENTS.md")
        policy = read("docs/system-development-governance.md")
        prompt = read(".pi/prompts/review-self-improvement-pr.md")
        pilot = read(".pi/prompts/run-one-day-review-pilot.md")

        self.assertIn("## Code Review Rules", agents)
        self.assertIn("現在のPR head", agents)
        self.assertIn("新しいpush", agents)
        self.assertIn("external_pr_review_ready", policy)
        self.assertIn("review transport", policy)
        self.assertIn("explicit_human_merge_approval", policy)
        self.assertIn("@codex review", prompt)
        self.assertIn("現在のPR head", prompt)
        self.assertIn("P0／P1", prompt)
        self.assertIn("mergeは行わない", prompt)
        self.assertIn("bg-20260924-governed-retry-04", pilot)
        self.assertIn("ae3a18f2341fd162928f14d220b205c1d33f5bbd253c1e113f41859da15195d6", pilot)
        self.assertIn("開始時刻から24時間後", pilot)
        self.assertIn("PRは一つ", pilot)
        self.assertIn("新しいbackground Job", pilot)
        self.assertIn("external_pr_review_ready", pilot)
        self.assertIn("mergeはしない", pilot)

    def test_governance_gate_is_fail_closed_and_context_bound(self) -> None:
        gate = read(".pi/extensions/governance-gate.ts")

        for required in (
            'const FAILURE_PREFIX = "GOVERNANCE_GATE_FAILURE"',
            'pi.on("context"',
            'pi.on("before_provider_request"',
            'pi.on("message_end"',
            'pi.on("tool_call"',
            'pi.on("tool_result"',
            "MAX_CONTEXT_BUNDLE_BYTES",
            "pendingContextBundle",
            "serializedContent",
            "AUDITOR_CONTEXT_PATHS",
            "DUAL_GOVERNANCE_CONTEXT_ROOT",
            "DUAL_AUDIT_BUNDLE_ROOT",
            "realpathSync",
            "auditorPathViolation",
            "tools: [acknowledgementTool]",
            "Managed-Runtime-Artifacts",
            "MANDATORY GOVERNANCE BOOTSTRAP ONLY",
        ):
            with self.subTest(required=required):
                self.assertIn(required, gate)

        self.assertIn('const AUDITOR_TOOLS = new Set([...READ_ONLY_TOOLS]);', gate)
        self.assertNotIn("governance_context_load", gate)

    def test_self_improvement_skill_routes_to_audit_contract(self) -> None:
        skill = read(".agents/skills/ephy-worker-self-improvement/SKILL.md")

        self.assertIn("docs/system-development-governance.md", skill)
        self.assertIn("references/eval-contract.md", skill)
        self.assertIn("references/audit-contract.md", skill)
        self.assertIn("references/audit-result.schema.json", skill)
        self.assertIn("fresh gpt-oss", skill)
        self.assertIn("Qwen", skill)

    def test_audit_contract_and_prompt_are_fail_closed(self) -> None:
        contract = read(
            ".agents/skills/ephy-worker-self-improvement/references/audit-contract.md"
        )
        prompt = read(".pi/prompts/audit-ephy-worker.md")

        self.assertIn(AUDIT_CONTRACT_ID, contract)
        self.assertEqual(contract.rstrip().splitlines()[-1], AUDIT_MARKER)
        for decision in ("ACCEPT_PROPOSAL", "REJECT_PROPOSAL", "INCONCLUSIVE"):
            self.assertIn(decision, contract)
            self.assertIn(decision, prompt)
        self.assertIn("$1", prompt)
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("read", prompt)
        self.assertIn("governance_ack", prompt)

    def test_audit_result_schema_is_strict_and_machine_readable(self) -> None:
        schema_path = (
            REPOSITORY_ROOT
            / ".agents/skills/ephy-worker-self-improvement/references/audit-result.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "ephy.audit-result.v1",
        )
        self.assertEqual(
            set(schema["properties"]["decision"]["enum"]),
            {"ACCEPT_PROPOSAL", "REJECT_PROPOSAL", "INCONCLUSIVE"},
        )
        self.assertEqual(
            schema["properties"]["auditor_assertions"]["properties"]["write_attempted"]["const"],
            False,
        )
        self.assertEqual(
            schema["allOf"][0]["then"]["properties"]["reason_codes"]["const"],
            ["ALL_GATES_PASSED"],
        )
        self.assertEqual(len(schema["$defs"]["domainResult"]["allOf"]), 3)

    def test_audit_result_schema_rejects_contradictory_acceptance(self) -> None:
        schema_path = (
            REPOSITORY_ROOT
            / ".agents/skills/ephy-worker-self-improvement/references/audit-result.schema.json"
        )
        valid = valid_audit_result()
        self.assertTrue(powershell_schema_accepts(valid, schema_path))

        conflicting_reason = deepcopy(valid)
        conflicting_reason["reason_codes"] = ["ALL_GATES_PASSED", "SCOPE_VIOLATION"]

        failing_check = deepcopy(valid)
        failing_check["candidate"]["checks"][0]["status"] = "FAIL"  # type: ignore[index]

        missing_evidence = deepcopy(valid)
        missing_evidence["workflow"]["checks"][0]["evidence"] = []  # type: ignore[index]

        blocker_finding = deepcopy(valid)
        blocker_finding["findings"] = [
            {
                "finding_id": "F-001",
                "domain": "candidate",
                "severity": "blocker",
                "summary": "A blocker cannot coexist with acceptance.",
                "rule_id": "scope",
                "evidence": [
                    {
                        "artifact_id": "candidate_patch",
                        "sha256": "a" * 64,
                        "location": "line 1",
                    }
                ],
            }
        ]

        missing_required_document = deepcopy(valid)
        missing_required_document["documents_read"] = missing_required_document[
            "documents_read"
        ][:-1]  # type: ignore[index]

        for invalid in (
            conflicting_reason,
            failing_check,
            missing_evidence,
            blocker_finding,
            missing_required_document,
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(powershell_schema_accepts(invalid, schema_path))


if __name__ == "__main__":
    unittest.main()
