// Manager runtime tests, NOT new requirements imposed on the frozen exercise.
import assert from "node:assert/strict";
import { readFile, writeFile, rm } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { makeFixture, createGuard, preflight, context, resetEnvironment, sha256, coderModel } from "./pi-campaign-guard-smoke.mjs";
import { campaignOutcome, saveOutcome } from "../tools/pi-local/campaign-outcome.mjs";

const cases = [];
const test = (name, run) => cases.push({ name, run });
const check = (source) => ({ name: "recovery-check", executable: process.execPath, args: ["-e", source], timeoutMs: 10000, candidateFailureExitCodes: [10] });

async function fixture(checkSource, run) {
  const f = await makeFixture("recovery");
  try {
    f.contract.tasks[0].maxRepairs = 2;
    f.contract.tasks[0].checks = [check(checkSource)];
    const text = JSON.stringify(f.contract);
    await writeFile(f.contractPath, text); f.contractSha256 = sha256(text);
    f.pi = await createGuard(f);
    f.lead = await preflight(f.pi, f);
    f.coder = context(f.candidate, coderModel);
    f.control = (ctx, params) => f.pi.tools.get("ephy_campaign_control").execute("fixture", { taskId: 1, summary: "Fixture", ...params }, undefined, undefined, ctx);
    f.state = async () => JSON.parse(await readFile(join(f.evidence, "campaign-state.json")));
    f.submit = () => f.control(f.coder, { action: "submit_implementation" });
    f.outcome = (exit = 0, message = "") => campaignOutcome(f.evidence, f.contractSha256, exit, message);
    await f.control(f.lead, { action: "start_implementation" });
    await writeFile(join(f.candidate, "src/allowed.txt"), "implementation\n");
    await run(f);
  } finally {
    resetEnvironment();
    assert.equal(dirname(resolve(f.parent)), resolve(tmpdir()));
    await rm(f.parent, { recursive: true, force: true });
  }
}

test("candidate-error-feedback-repair-and-success", () => fixture(`const fs = require('fs'); if (fs.readFileSync('src/allowed.txt','utf8') !== 'repaired\\n') { console.log('ctx is not defined'); process.exit(10); }`, async (f) => {
  const failed = await f.submit();
  assert.match(failed.content[0].text, /ctx is not defined/);
  assert.match(failed.content[0].text, /Repair 1\/2/);
  assert.equal((await f.state()).taskOutcome, "running");
  assert.equal(f.coder.shutdownRequested, false);
  assert.equal(f.pi.selectedModels.length, 1);
  const payload = await f.pi.emit("before_provider_request", { payload: { messages: [{ role: "system", content: "Policy" }] } }, f.coder);
  assert.match(payload.messages[0].content, /ctx is not defined/);
  assert.match(payload.messages[0].content, /Repair 1\/2/);
  await writeFile(join(f.candidate, "src/allowed.txt"), "repaired\n");
  await f.submit();
  assert.equal((await f.state()).phase, "reviewing");
  await f.control(f.lead, { action: "review_task", verdict: "PASS" });
  assert.equal((await f.state()).taskOutcome, "succeeded");
  assert.equal(f.outcome().outcome, "succeeded");
  assert.equal(f.outcome().exitCode, 0);
  // Inconsistent launcher/evidence cannot be silently promoted to success.
  assert.equal(f.outcome(1, "process failure").outcome, "needs_input");
  await writeFile(join(f.evidence, "combined.patch"), "tamper");
  assert.equal(f.outcome().outcome, "needs_input");
}));
test("two-repairs-only-then-actionable-human-decision", () => fixture("console.log('ctx is not defined'); process.exit(10)", async (f) => {
  for (let attempt = 1; attempt <= 3; attempt++) {
    await f.submit();
    assert.equal((await f.state()).taskOutcome, attempt < 3 ? "running" : "needs_input");
  }
  assert.equal((await f.state()).status, "failed");
  assert.equal((await f.state()).repairCount, 2);
  const report = f.outcome();
  assert.equal(report.reason, "fixed_check_failed");
  assert.equal(report.exitCode, 2);
  assert.match(report.question, /budget \(2\) is exhausted/);
  assert.ok(report.blocker.patchSha256);
  saveOutcome(f.evidence, report);
  assert.match(await readFile(join(f.evidence, "NEXT-ACTION.md"), "utf8"), /needs_input/);
  await f.submit(); assert.equal((await f.state()).repairCount, 2);
}));
test("infrastructure-is-not-repaired", () => fixture("process.exit(2)", async (f) => {
  await f.submit();
  assert.equal((await f.state()).repairCount, 0);
  assert.equal(f.outcome().reason, "verification_environment_failed");
  assert.equal(f.outcome().outcome, "needs_input");
  assert.match(f.outcome().question, /manager diagnose/);
}));
test("controller-exception-becomes-evidenced-blocker", () => fixture("process.exit(0)", async (f) => {
  f.pi.exec = async () => { throw new Error("Git process unavailable"); };
  await f.submit();
  assert.equal(f.outcome().outcome, "needs_input");
  assert.equal(f.outcome().reason, "controller_execution_failed");
  assert.match(f.outcome().blocker.error, /Git process unavailable/);
}));
test("agent-stop-is-not-human-interrupt", () => fixture("process.exit(0)", async (f) => {
  await f.control(f.coder, { action: "stop" });
  assert.equal(f.outcome().outcome, "needs_input");
  assert.equal(f.outcome().reason, "agent_requested_stop");
}));
test("only-interactive-explicit-stop-is-interrupted", () => fixture("process.exit(0)", async (f) => {
  await f.pi.emit("input", { source: "extension", text: "EPHY_STOP" }, f.coder);
  assert.equal((await f.state()).taskOutcome, "running");
  await f.pi.emit("input", { source: "interactive", text: "EPHY_STOP" }, f.coder);
  assert.equal(f.outcome().outcome, "interrupted");
  assert.equal(f.outcome().exitCode, 130);
  await f.pi.emit("agent_end", {}, f.coder);
  assert.equal(f.pi.followUps.length, 0);
}));
test("unexplained-shutdown-is-not-success-or-user-interrupt", () => fixture("process.exit(0)", async (f) => {
  await f.pi.emit("session_shutdown", {}, f.coder);
  assert.equal(f.outcome().outcome, "needs_input");
  assert.equal(f.outcome().reason, "session_ended_before_completion");
}));
test("deadline-check-in-control-cannot-grant-repair", () => fixture("process.exit(10)", async (f) => {
  const now = Date.now;
  try {
    Date.now = () => new Date(f.contract.maxDurationSeconds * 1000 + now() + 1000).getTime();
    await f.submit();
  } finally { Date.now = now; }
  assert.equal((await f.state()).repairCount, 0);
  assert.equal(f.outcome().reason, "deadline_reached");
}));
test("missing-state-is-actionable-not-success", () => fixture("process.exit(0)", async (f) => {
  await rm(join(f.evidence, "campaign-state.json"));
  const report = f.outcome(2, "preflight unavailable");
  assert.equal(report.outcome, "needs_input");
  assert.equal(report.exitCode, 2);
  assert.ok(report.question);
  assert.equal(report.launcherError, "preflight unavailable");
}));
for (const item of cases) { await item.run(); console.log(`PASS: ${item.name}`); }
console.log(`PASS: ${cases.length} recovery / task outcome scenarios`);
