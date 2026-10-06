import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import setup, { selectAuditedPatch } from "../.pi/extensions/background-jobs.ts";

const root = mkdtempSync(join(tmpdir(), "ephy-formal-submit-"));
try {
  process.env.DUAL_PI_STATE_DIR = join(root, "state");
  process.env.DUAL_JOB_RUNNER = join(root, "runner.ps1");
  writeFileSync(process.env.DUAL_JOB_RUNNER, "# test only\n");
  const spec = {
    repoRoot: root, baseRevision: "a".repeat(40), runtime: {}, controls: {}, verifier_identity: {},
    model_identities: { planner: { model_id: "expected-lead" } },
    contract: { task: "Actual frozen task", semantic_scope: "Actual frozen scope",
      allowed_files: ["docs/test.md"], timeout_seconds: 1200, stage_seconds: 300,
      max_repairs: 0, output_token_budget: 8000, checks: [{id:"fixed",argv:["fixed-check"]}] },
  };
  const file = join(root, "spec.json");
  const bytes = JSON.stringify(spec);
  writeFileSync(file, bytes);
  const tools = new Map();
  let confirmation;
  setup({ registerTool: t => tools.set(t.name, t), registerCommand() {}, on() {},
    async exec(binary, args) {
      assert.equal(binary, "git", "Cancellation must not launch a runner");
      const result = args.includes("--show-toplevel") ? root : args.includes("HEAD") ? spec.baseRevision : "";
      return { code: 0, stdout: result, stderr: "" };
    },
  });
  const input = { title: "Other title", task: "Other task", doneWhen: ["other"],
    timeoutMinutes: 999, maxRepairAttempts: 3, verificationCommands: ["other-check"],
    formalSpecPath: file, formalSpecSha256: createHash("sha256").update(bytes).digest("hex") };
  const result = await tools.get("background_job_submit").execute("id", input, undefined, undefined,
    { cwd: root, hasUI: true, ui: { async confirm(_title, body) { confirmation = body; return false; } } });
  for (const text of ["Actual frozen task", "Actual frozen scope", "1200 seconds", "Repair limit: 0",
    "expected-lead", "fixed-check", "docs/test.md"]) assert.ok(confirmation.includes(text), text);
  for (const text of ["Other task", "999 minutes", "other-check"]) assert.ok(!confirmation.includes(text), text);
  assert.ok(result.content[0].text.includes("cancelled"));
  input.formalSpecSha256 = "0".repeat(64);
  await assert.rejects(() => tools.get("background_job_submit").execute("id", input, undefined, undefined,
    { cwd: root, hasUI: true, ui: { confirm() { assert.fail("Wrong hash reached approval"); } } }), /mismatch/);
  // Synthetic artifact binding control; this is not a real audit or adoption.
  const proposal = join(root, "proposal");
  const bundle = join(proposal, "audit-bundle");
  const { mkdirSync } = await import("node:fs");
  mkdirSync(bundle, { recursive: true });
  const sha = value => createHash("sha256").update(value).digest("hex");
  const patch = "Synthetic patch bytes\n";
  writeFileSync(join(proposal, "candidate.patch"), patch);
  writeFileSync(join(bundle, "candidate_patch.txt"), patch);
  const auditInput = JSON.stringify({ job_id: "test", audit_id: "test-audit", baseline_commit: spec.baseRevision,
    final_bindings: { candidate_patch_sha256: sha(patch) } });
  writeFileSync(join(bundle, "audit-input.json"), auditInput);
  const auditResult = JSON.stringify({ job_id: "test", audit_id: "test-audit", decision: "ACCEPT_PROPOSAL",
    bound_inputs: { candidate_patch_sha256: sha(patch), audit_input_sha256: sha(auditInput) } });
  writeFileSync(join(proposal, "audit-result.json"), auditResult);
  writeFileSync(join(proposal, "audit-execution-attestation.json"), JSON.stringify({ passed: true,
    schema_valid: true, candidate_unchanged: true, bundle_unchanged: true,
    audit_result_sha256: sha(auditResult), audit_input_sha256: sha(auditInput) }));
  const job = { schemaVersion: 2, status: "review_ready", id: "test", jobDir: proposal, baseRevision: spec.baseRevision };
  assert.equal(selectAuditedPatch(job), join(proposal, "candidate.patch"));
  writeFileSync(join(proposal, "candidate.patch"), "changed after audit");
  assert.throws(() => selectAuditedPatch(job), /mismatch/);
  writeFileSync(join(proposal, "candidate.patch"), patch);
  writeFileSync(join(proposal, "audit-result.json"), auditResult + " ");
  assert.throws(() => selectAuditedPatch(job), /mismatch/);
  assert.equal(selectAuditedPatch({ ...job, schemaVersion: 1 }), join(proposal, "changes.patch"));
  console.log("PASS: formal approval binds actual task, scope, checks, model and caps; hash mismatch stops");
} finally {
  rmSync(root, { recursive: true, force: true });
}
