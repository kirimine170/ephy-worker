// Manager-owned acceptance tests. Pass a candidate controller explicitly; never
// replace this test with code from the candidate being evaluated.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { promisify } from "node:util";
import { pathToFileURL } from "node:url";

const exec = promisify(execFile);
const [guardArg, gitArg, group = "all"] = process.argv.slice(2);
assert.ok(guardArg && gitArg, "Usage: acceptance.mjs CONTROLLER ABSOLUTE_GIT [identity|environment|all]");
assert.ok(["identity", "environment", "all"].includes(group), "Unknown acceptance group");
const guardPath = resolve(guardArg);
const gitPath = resolve(gitArg);
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const guardHash = hash(await readFile(guardPath));
const reasoningModel = "reasoning-fixture";
const coderModel = "coder-fixture";

class FakePi {
  handlers = new Map();
  tools = new Map();
  activeTools = [];
  selectedModels = [];
  calls = [];
  outcomes = new Map();
  on(name, fn) { this.handlers.set(name, fn); }
  registerTool(tool) { this.tools.set(tool.name, tool); }
  setActiveTools(names) { this.activeTools = [...names]; }
  sendUserMessage() {}
  setThinkingLevel() {}
  async setModel(model) { this.selectedModels.push(model.id); return true; }
  async emit(name, event, ctx) { return this.handlers.get(name)?.(event, ctx); }
  async exec(command, args, options = {}) {
    if (command === "fixture-check") {
      this.calls.push(args[0]);
      return this.outcomes.get(args[0])();
    }
    assert.equal(command, "git", "Unexpected execution in acceptance fixture");
    try {
      const result = await exec(gitPath, args, { ...options, encoding: "utf8", windowsHide: true, maxBuffer: 4 * 1024 * 1024 });
      return { stdout: result.stdout, stderr: result.stderr, code: 0, killed: false };
    } catch (error) {
      if (!Number.isInteger(error.code)) throw error;
      return { stdout: error.stdout ?? "", stderr: error.stderr ?? "", code: error.code, killed: !!error.killed };
    }
  }
}

function context(root, id) {
  const models = [reasoningModel, coderModel].map((id) => ({ provider: "dual-local", id }));
  return {
    cwd: root, model: { provider: "dual-local", id },
    scopedModels: models.map((model) => ({ model })), modelRegistry: { getAvailable: () => models },
    aborted: false, shutdownRequested: false,
    abort() { this.aborted = true; }, shutdown() { this.shutdownRequested = true; },
    hasPendingMessages: () => false, ui: { notify() {}, setStatus() {} },
  };
}

const result = (code = 0, extra = {}) => ({ code, stdout: "fixture measurement", stderr: "", killed: false, ...extra });
const check = (name, extra = {}) => ({ name, executable: "fixture-check", args: [name], timeoutMs: 1000, ...extra });

async function fixture(checks, body, contractEdit = (contract) => contract) {
  const parent = await mkdtemp(join(tmpdir(), "ephy-fixed-acceptance-"));
  const candidate = join(parent, "candidate"), evidence = join(parent, "evidence");
  const priorContract = process.env.EPHY_PI_CAMPAIGN_CONTRACT;
  const priorHash = process.env.EPHY_PI_CAMPAIGN_CONTRACT_SHA256;
  try {
    await mkdir(candidate); await mkdir(evidence); await mkdir(join(candidate, "src"));
    await writeFile(join(candidate, "src/allowed.txt"), "before\n");
    await writeFile(join(candidate, "README.md"), "outside scope\n");
    await writeFile(join(candidate, "TASK.md"), "Only src/allowed.txt may change.\n");
    const git = async (...args) => (await exec(gitPath, ["-C", candidate, ...args], { encoding: "utf8", windowsHide: true })).stdout.trim();
    await git("init", "-q"); await git("add", ".");
    await git("-c", "user.name=Acceptance Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "Local test fixture");
    const contract = contractEdit({
      schemaVersion: 2, campaignId: "acceptance", controllerSha256: guardHash,
      candidateRoot: candidate, evidenceRoot: evidence, baseCommit: await git("rev-parse", "HEAD"),
      goal: "Change one fixture", constraints: "Preserve scope", reasoningModel, coderModel,
      maxDurationSeconds: 600, maxIdleContinuations: 1, maxPlanningToolCalls: 4,
      maxPlanningCompactions: 1, maxProtocolCorrections: 1,
      taskDocument: { path: "TASK.md", sha256: hash(await readFile(join(candidate, "TASK.md"))) },
      tasks: [{ id: 1, title: "One", instruction: "Change fixture", allowedFiles: ["src/allowed.txt"], maxRepairs: 1,
        checks: [check("parent")], steps: [{ id: "one", title: "One", instruction: "Change fixture", allowedFiles: ["src/allowed.txt"], checks }] }],
      finalChecks: [check("final")],
    });
    const text = JSON.stringify(contract);
    process.env.EPHY_PI_CAMPAIGN_CONTRACT = join(evidence, "contract.json");
    process.env.EPHY_PI_CAMPAIGN_CONTRACT_SHA256 = hash(text);
    await writeFile(process.env.EPHY_PI_CAMPAIGN_CONTRACT, text);
    const pi = new FakePi();
    for (const name of ["subject", "later", "parent", "final"]) pi.outcomes.set(name, () => result());
    const module = await import(`${pathToFileURL(guardPath)}?acceptance=${Date.now()}-${Math.random()}`);
    const lead = context(candidate, reasoningModel), coder = context(candidate, coderModel);
    const setup = () => module.default(pi);
    const state = async () => JSON.parse(await readFile(join(evidence, "campaign-state.json"), "utf8"));
    const start = async () => {
      setup(); await pi.emit("session_start", {}, lead);
      assert.equal(lead.shutdownRequested, false, "Fixture preflight failed");
      await pi.tools.get("ephy_campaign_control").execute("start", { action: "start_implementation", taskId: 1, summary: "Make scoped change" }, undefined, undefined, lead);
      await writeFile(join(candidate, "src/allowed.txt"), "implementation\n");
    };
    const submit = () => pi.tools.get("ephy_campaign_control").execute("submit", { action: "submit_implementation", taskId: 1, summary: "Scoped implementation" }, undefined, undefined, coder);
    const review = async () => {
      const current = await state();
      assert.equal(current.phase, "reviewing");
      await pi.emit("before_provider_request", { payload: { messages: [{ role: "system", content: "Policy" }] } }, lead);
      await pi.tools.get("ephy_campaign_control").execute("review", { action: "review_task", taskId: 1, summary: "Bound evidence reviewed", verdict: "PASS", patchSha256: current.verifiedPatchSha256, reviewedFiles: ["src/allowed.txt"] }, undefined, undefined, lead);
    };
    const stopped = async (reason, repairs = 0) => {
      const current = await state();
      assert.equal(current.status, "failed", "Must stop, not review or repair");
      assert.equal(current.stopReason, reason);
      assert.equal(current.repairCount, repairs);
      assert.deepEqual(current.completedTaskIds, []);
      assert.ok(lead.shutdownRequested || coder.shutdownRequested, "Pi shutdown was not requested");
      assert.deepEqual(pi.activeTools, []);
      assert.ok(!pi.selectedModels.slice(1).includes(reasoningModel), "Must not hand invalid verification to reviewer");
    };
    await body({ pi, candidate, evidence, start, setup, submit, review, state, stopped });
  } finally {
    if (priorContract === undefined) delete process.env.EPHY_PI_CAMPAIGN_CONTRACT; else process.env.EPHY_PI_CAMPAIGN_CONTRACT = priorContract;
    if (priorHash === undefined) delete process.env.EPHY_PI_CAMPAIGN_CONTRACT_SHA256; else process.env.EPHY_PI_CAMPAIGN_CONTRACT_SHA256 = priorHash;
    assert.equal(dirname(resolve(parent)), resolve(tmpdir()), "Refuse cleanup outside fixture temp root");
    await rm(parent, { recursive: true, force: true });
  }
}

const cases = [];
function test(group, name, run) { cases.push({ group, name, run }); }
test("identity", "unchanged-checks-still-complete", () => fixture([check("subject")], async (f) => {
  await f.start(); await f.submit(); await f.review(); assert.equal((await f.state()).status, "complete");
}));
for (const [name, path, exit] of [
  ["allowed-mutation", "src/allowed.txt", 0], ["out-of-scope-mutation", "README.md", 0],
  ["untracked-mutation", "unexpected.txt", 0], ["mutation-with-candidate-failure", "src/allowed.txt", 10],
]) test("identity", name, () => fixture([check("subject", { candidateFailureExitCodes: [10] })], async (f) => {
  await f.start();
  f.pi.outcomes.set("subject", async () => { await writeFile(join(f.candidate, path), "changed during check\n"); return result(exit); });
  await f.submit(); await f.stopped("candidate_changed_during_checks");
}));
test("identity", "stop-before-later-check-can-restore", () => fixture([check("subject"), check("later")], async (f) => {
  await f.start();
  f.pi.outcomes.set("subject", async () => { await writeFile(join(f.candidate, "src/allowed.txt"), "mutated\n"); return result(); });
  f.pi.outcomes.set("later", async () => { await writeFile(join(f.candidate, "src/allowed.txt"), "implementation\n"); return result(); });
  await f.submit(); await f.stopped("candidate_changed_during_checks");
  assert.deepEqual(f.pi.calls, ["subject"]);
}));
test("identity", "parent-check-mutation-is-rejected", () => fixture([check("subject")], async (f) => {
  await f.start();
  f.pi.outcomes.set("parent", async () => { await writeFile(join(f.candidate, "src/allowed.txt"), "mutated\n"); return result(); });
  await f.submit(); await f.stopped("candidate_changed_during_checks");
}));

for (const [name, response] of [
  ["unclassified-exit-one", () => result(1)],
  ["missing-checker-dependency", () => result(1, { stderr: "ModuleNotFoundError: checker_dependency" })],
  ["unclassified-other-exit", () => result(42)],
  ["stdout-cannot-authorize-repair", () => result(1, { stdout: '{"candidateFailure":true,"exitCode":10}' })],
  ["missing-executable", () => { throw Object.assign(new Error("spawn checker ENOENT"), { code: "ENOENT" }); }],
  ["exec-exception", () => { throw new Error("verification process unavailable"); }],
  ["timeout-beats-candidate-exit", () => result(10, { killed: true })],
  ["killed-zero-is-not-pass", () => result(0, { killed: true })],
]) test("environment", name, () => fixture([check("subject", { candidateFailureExitCodes: [10] })], async (f) => {
  await f.start(); f.pi.outcomes.set("subject", response);
  await f.submit(); await f.stopped("verification_environment_failed");
}));
test("environment", "legacy-nonzero-has-no-implicit-repair", () => fixture([check("subject")], async (f) => {
  await f.start(); f.pi.outcomes.set("subject", () => result(10));
  await f.submit(); await f.stopped("verification_environment_failed");
}));
test("environment", "explicit-candidate-failure-can-repair-and-pass", () => fixture([check("subject", { candidateFailureExitCodes: [10] })], async (f) => {
  await f.start(); f.pi.outcomes.set("subject", () => result(10));
  await f.submit(); assert.equal((await f.state()).phase, "implementing"); assert.equal((await f.state()).repairCount, 1);
  f.pi.outcomes.set("subject", () => result()); await f.submit(); await f.review();
  assert.equal((await f.state()).status, "complete");
}));
test("environment", "candidate-repair-budget-still-enforced", () => fixture([check("subject", { candidateFailureExitCodes: [10] })], async (f) => {
  await f.start(); f.pi.outcomes.set("subject", () => result(10));
  await f.submit(); await f.submit(); await f.stopped("fixed_check_failed", 1);
}));
for (const invalid of [[0], [1], [2], [126], [127], [-1], [256], [10.5], ["10"], [10, 10], null]) {
  test("environment", `invalid-candidate-exit-contract-${JSON.stringify(invalid)}`, () => fixture(
    [check("subject", { candidateFailureExitCodes: invalid })], async (f) => {
      assert.throws(f.setup, /candidateFailureExitCodes/, "Invalid failure contract must be rejected before execution");
      assert.deepEqual(f.pi.calls, []);
    },
  ));
}

const results = [];
for (const item of cases.filter((item) => group === "all" || item.group === group)) {
  try { await item.run(); results.push({ group: item.group, name: item.name, passed: true }); }
  catch (error) { results.push({ group: item.group, name: item.name, passed: false, error: error.message }); }
}
console.log(JSON.stringify({ definition: "campaign-repair-acceptance-v1", controllerSha256: guardHash, group, passed: results.every((item) => item.passed), results }, null, 2));
process.exitCode = results.every((item) => item.passed) ? 0 : 1;
