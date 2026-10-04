import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, readFileSync, existsSync, utimesSync, statSync } from "node:fs";
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
  const execCalls = [];
  let confirmation;
  setup({ registerTool: t => tools.set(t.name, t), registerCommand() {}, on() {},
    async exec(binary, args) {
      execCalls.push([binary, args]);
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
  input.formalSpecSha256 = createHash("sha256").update(bytes).digest("hex");
  const jobDir = join(process.env.DUAL_PI_STATE_DIR, "jobs", "bg-unreadable-fixture");
  const recordFile = join(jobDir, "job.json");
  const failures = [];
  for (const corruption of ["malformed", "missing", "directory", "wrong-shape", "unknown-status"]) {
    mkdirSync(jobDir, { recursive: true });
    if (corruption === "malformed") writeFileSync(recordFile, "{interrupted state");
    if (corruption === "directory") mkdirSync(recordFile);
    if (corruption === "wrong-shape") writeFileSync(recordFile, "{}");
    if (corruption === "unknown-status") writeFileSync(recordFile, JSON.stringify({
      schemaVersion: 1, id: "bg-unreadable-fixture", status: "unknown", jobDir,
      title: "Synthetic job", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    }));
    const before = existsSync(recordFile) && corruption !== "directory" ? readFileSync(recordFile) : undefined;
    // Old timestamps must not let stale recovery replace an unreadable record.
    const old = new Date("2000-01-01T00:00:00Z");
    utimesSync(jobDir, old, old);
    execCalls.length = 0;
    try {
      const blocked = await tools.get("background_job_submit").execute("id", input, undefined, undefined,
        { cwd: root, hasUI: true, ui: { confirm() { assert.fail("Unreadable job reached confirmation"); } } });
      assert.equal(blocked.details.active.length, 1);
      assert.equal(blocked.details.active[0].status, "unreadable");
      assert.equal(blocked.details.active[0].id, "bg-unreadable-fixture");
      assert.equal(execCalls.length, 0, "Unreadable job must block before Git/runner commands");
      const status = await tools.get("background_job_status").execute("id", { jobId: "bg-unreadable-fixture" });
      assert.equal(status.details.jobs[0].status, "unreadable");
      const cancelled = await tools.get("background_job_cancel").execute("id", { jobId: "bg-unreadable-fixture" }, undefined, undefined,
        { hasUI: true, ui: { confirm() { assert.fail("Unreadable job must not be overwritten by cancellation"); } } });
      assert.equal(cancelled.details.status, "unreadable");
      assert.equal(existsSync(join(jobDir, "cancel.request")), false);
      if (before) assert.deepEqual(readFileSync(recordFile), before);
      if (corruption === "missing") assert.equal(existsSync(recordFile), false);
      if (corruption === "directory") assert.equal(statSync(recordFile).isDirectory(), true);
      console.log(`PASS: unreadable-job ${corruption} blocks submit without commands or record mutation`);
    } catch (error) { failures.push(`${corruption}: ${error.message}`); }
    rmSync(jobDir, { recursive: true, force: true });
  }
  assert.deepEqual(failures, [], "Every unreadable state must remain visible and block submission");
  mkdirSync(jobDir, { recursive: true });
  const restored = { schemaVersion: 1, id: "bg-unreadable-fixture", jobDir, status: "running", runnerPid: process.pid,
    title: "Synthetic restored job", createdAt: "2000-01-01T00:00:00Z", updatedAt: "2000-01-01T00:00:00Z" };
  writeFileSync(recordFile, JSON.stringify(restored));
  const restoredBytes = readFileSync(recordFile);
  execCalls.length = 0;
  const running = await tools.get("background_job_submit").execute("id", input, undefined, undefined,
    { cwd: root, hasUI: true, ui: { confirm() { assert.fail("Live restored job reached confirmation"); } } });
  assert.equal(running.details.active[0].status, "running");
  assert.equal(execCalls.length, 0);
  assert.deepEqual(readFileSync(recordFile), restoredBytes);
  console.log("PASS: restored live job remains active without stale recovery or commands");
  restored.status = "failed";
  writeFileSync(recordFile, JSON.stringify(restored));
  let reachedConfirmation = false;
  const terminal = await tools.get("background_job_submit").execute("id", input, undefined, undefined,
    { cwd: root, hasUI: true, ui: { confirm() { reachedConfirmation = true; return false; } } });
  assert.equal(reachedConfirmation, true);
  assert.ok(terminal.content[0].text.includes("cancelled"));
  assert.equal(execCalls.every(([binary]) => binary === "git"), true);
  console.log("PASS: readable terminal job permits normal confirmation; no runner is started");
  console.log("PASS: formal approval binds actual task, scope, checks, model and caps; hash mismatch stops");
} finally {
  rmSync(root, { recursive: true, force: true });
}
