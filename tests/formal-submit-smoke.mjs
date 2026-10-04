import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, readFileSync, existsSync, utimesSync, statSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import setup, { selectAuditedPatch } from "../.pi/extensions/background-jobs.ts";

async function waitForFile(file) {
  const deadline = Date.now() + 15000;
  while (!existsSync(file)) {
    if (Date.now() > deadline) throw new Error("Admission fixture barrier timeout");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

async function runAdmissionChild() {
  const [directory, actor, phase] = process.argv.slice(3);
  process.env.DUAL_PI_STATE_DIR = join(directory, "state");
  process.env.DUAL_JOB_RUNNER = join(directory, "runner.ps1");
  process.env.DUAL_POWERSHELL_EXE = "fixture-shell";
  const tools = new Map(), calls = [];
  let confirmations = 0, launches = 0;
  const pause = async () => {
    writeFileSync(join(directory, "first-at-barrier"), actor);
    await waitForFile(join(directory, "release-first"));
  };
  setup({ registerTool: t => tools.set(t.name, t), registerCommand() {}, on() {},
    async exec(binary, args) {
      calls.push([binary, args]);
      if (binary === "git") {
        if (actor === "first" && phase === "git" && args.includes("--show-toplevel")) await pause();
        const stdout = args.includes("--show-toplevel") ? directory : args.includes("HEAD") ? "a".repeat(40) : "";
        return { code: 0, stdout, stderr: "" };
      }
      assert.equal(binary, "fixture-shell");
      launches++;
      // Actual current fixture PID; the shell/runner itself is never executed.
      return { code: 0, stdout: String(process.pid), stderr: "" };
    },
  });
  const result = await tools.get("background_job_submit").execute("id",
    { title: "Synthetic admission", task: "Offline fixture", doneWhen: ["No real runner"] }, undefined, undefined,
    { cwd: directory, hasUI: true, ui: { async confirm() {
      confirmations++;
      if (actor === "first" && phase === "confirmation") await pause();
      return true;
    }, notify() {} } });
  writeFileSync(join(directory, actor + "-result.json"), JSON.stringify({
    pid: process.pid, calls: calls.length, confirmations, launches,
    blocked: result.details.admissionBlocked === true, status: result.details.status,
  }));
}

function spawnAdmissionChild(directory, actor, phase) {
  const child = spawn(process.execPath, [...process.execArgv, fileURLToPath(import.meta.url),
    "--admission-child", directory, actor, phase], { timeout: 20000 });
  let output = "";
  child.stdout.on("data", chunk => { output += chunk; });
  child.stderr.on("data", chunk => { output += chunk; });
  const done = new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("exit", (code, signal) => code === 0 ? resolve() : reject(new Error(
      "Admission child failed: " + code + "/" + signal + " " + output)));
  });
  return { done };
}

async function runAdmissionControls(root) {
  const failures = [];
  for (const phase of ["git", "confirmation"]) {
    const directory = join(root, "admission-" + phase);
    mkdirSync(directory);
    writeFileSync(join(directory, "runner.ps1"), "# offline fixture only\n");
    const first = spawnAdmissionChild(directory, "first", phase);
    try {
      await waitForFile(join(directory, "first-at-barrier"));
      const second = spawnAdmissionChild(directory, "second", phase);
      await second.done;
    } finally {
      writeFileSync(join(directory, "release-first"), "release");
      await first.done;
    }
    const a = JSON.parse(readFileSync(join(directory, "first-result.json"), "utf8"));
    const b = JSON.parse(readFileSync(join(directory, "second-result.json"), "utf8"));
    const jobs = readdirSync(join(directory, "state", "jobs"));
    console.log("OBSERVE: cross-process admission " + phase + " " + JSON.stringify({ first: a, second: b, jobs: jobs.length }));
    try {
      assert.notEqual(a.pid, b.pid, "Separate OS processes are required");
      assert.equal(a.launches, 1);
      assert.equal(b.launches, 0);
      assert.equal(b.calls, 0);
      assert.equal(b.confirmations, 0);
      assert.equal(b.blocked, true);
      assert.equal(jobs.length, 1);
      assert.equal(existsSync(join(directory, "state", "background-admission.lock")), false);
      console.log("PASS: cross-process admission " + phase + " permits one queued job and one mocked launch");
    } catch (error) { failures.push(phase + ": " + error.message); }
  }

  const previousState = process.env.DUAL_PI_STATE_DIR;
  const directory = join(root, "admission-recovery");
  process.env.DUAL_PI_STATE_DIR = directory;
  mkdirSync(directory);
  const claim = join(directory, "background-admission.lock");
  const params = { title: "Offline recovery", task: "Synthetic", doneWhen: ["No runner"] };
  const toolsFor = exec => {
    const tools = new Map();
    setup({ registerTool: t => tools.set(t.name, t), registerCommand() {}, on() {}, exec });
    return tools;
  };
  const git = async (binary, args) => {
    assert.equal(existsSync(claim), true, "Admission must remain held through Git and confirmation");
    assert.equal(binary, "git");
    return { code: 0, stdout: args.includes("--show-toplevel") ? root : args.includes("HEAD") ? "a".repeat(40) : "", stderr: "" };
  };
  try {
    let confirmations = 0;
    const tools = toolsFor(git);
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const cancelled = await tools.get("background_job_submit").execute("id", params, undefined, undefined,
          { cwd: root, hasUI: true, ui: { confirm() { confirmations++; return false; } } });
        assert.ok(cancelled.content[0].text.includes("cancelled"));
        assert.equal(existsSync(claim), false);
      } catch (error) { failures.push("cancel-release: " + error.message); }
    }
    if (confirmations === 2) console.log("PASS: cancelled admission releases its claim and allows a new attempt");
    try {
      const throwing = toolsFor(async () => {
        assert.equal(existsSync(claim), true);
        throw new Error("controlled command failure");
      });
      await assert.rejects(throwing.get("background_job_submit").execute("id", params, undefined, undefined,
        { cwd: root, hasUI: true, ui: { confirm() { assert.fail("Failure reached confirmation"); } } }), /controlled command failure/);
      assert.equal(existsSync(claim), false);
      const retry = await tools.get("background_job_submit").execute("id", params, undefined, undefined,
        { cwd: root, hasUI: true, ui: { confirm() { return false; } } });
      assert.ok(retry.content[0].text.includes("cancelled"));
      console.log("PASS: throwing admission releases its claim and permits a later attempt");
    } catch (error) { failures.push("exception-release: " + error.message); }
    const abandoned = JSON.stringify({ pid: 999999999, createdAt: "2000-01-01T00:00:00Z" });
    writeFileSync(claim, abandoned);
    try {
      const blocked = await tools.get("background_job_submit").execute("id", params, undefined, undefined,
        { cwd: root, hasUI: true, ui: { confirm() { assert.fail("Abandoned claim reached confirmation"); } } });
      assert.equal(blocked.details.admissionBlocked, true);
      assert.equal(readFileSync(claim, "utf8"), abandoned);
      console.log("PASS: abandoned admission claim is preserved and blocks automatic recovery");
    } catch (error) { failures.push("abandoned-claim: " + error.message); }
  } finally { process.env.DUAL_PI_STATE_DIR = previousState; }
  assert.deepEqual(failures, [], "Cross-process admission and claim recovery controls must all pass");
}


async function runPostVerifierApplyControls(root) {
  const previousState = process.env.DUAL_PI_STATE_DIR;
  const failures = [];
  const sha = value => createHash("sha256").update(value).digest("hex");
  const cases = [["none", "final-verifier"], ...["dirty", "head", "patch"].flatMap(
    mutation => [["" + mutation, "confirmation"], ["" + mutation, "final-verifier"]])];
  try {
    for (const [mutation, phase] of cases) {
      const directory = join(root, "apply-" + mutation + "-" + phase);
      const checkout = join(directory, "checkout");
      mkdirSync(checkout, { recursive: true });
      const emptyConfig = join(directory, "empty-gitconfig");
      writeFileSync(emptyConfig, "");
      const git = args => {
        const result = spawnSync("git", args, { cwd: checkout, encoding: "utf8",
          env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: emptyConfig } });
        if (result.error) throw result.error;
        return { code: result.status, stdout: result.stdout, stderr: result.stderr };
      };
      const checkedGit = args => {
        const result = git(args);
        assert.equal(result.code, 0, result.stderr);
        return result.stdout;
      };
      checkedGit(["init"]);
      writeFileSync(join(checkout, "doc.md"), "before\n");
      checkedGit(["add", "doc.md"]);
      checkedGit(["-c", "user.name=OfflineControl", "-c", "user.email=offline@local.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "Fixture baseline"]);
      const base = checkedGit(["rev-parse", "HEAD"]).trim();
      writeFileSync(join(checkout, "doc.md"), "approved\n");
      const patch = checkedGit(["diff", "--binary", "--full-index"]);
      checkedGit(["checkout", "--", "doc.md"]);
      const state = join(directory, "state");
      process.env.DUAL_PI_STATE_DIR = state;
      const jobDir = join(state, "jobs", "bg-apply-control");
      const bundle = join(jobDir, "audit-bundle");
      mkdirSync(bundle, { recursive: true });
      const controller = join(directory, "controller");
      mkdirSync(join(controller, "ephy_worker"), { recursive: true });
      const pins = { [process.execPath]: sha(readFileSync(process.execPath)) };
      for (const name of ["__init__.py", "formal_runtime.py", "formal_artifacts.py", "formal_campaign.py", "strata_runtime.py"]) {
        const file = join(controller, "ephy_worker", name);
        writeFileSync(file, "# inert controller fixture; integration verifier is mocked\n");
        pins[file] = sha(readFileSync(file));
      }
      writeFileSync(join(jobDir, "candidate.patch"), patch);
      writeFileSync(join(bundle, "candidate_patch.txt"), patch);
      const input = JSON.stringify({ job_id: "bg-apply-control", audit_id: "offline-audit",
        baseline_commit: base, final_bindings: { candidate_patch_sha256: sha(patch) } });
      const result = JSON.stringify({ job_id: "bg-apply-control", audit_id: "offline-audit",
        decision: "ACCEPT_PROPOSAL", bound_inputs: {
          candidate_patch_sha256: sha(patch), audit_input_sha256: sha(input) } });
      writeFileSync(join(bundle, "audit-input.json"), input);
      writeFileSync(join(jobDir, "audit-result.json"), result);
      writeFileSync(join(jobDir, "audit-execution-attestation.json"), JSON.stringify({
        passed: true, schema_valid: true, candidate_unchanged: true, bundle_unchanged: true,
        audit_result_sha256: sha(result), audit_input_sha256: sha(input) }));
      const at = new Date().toISOString();
      const job = { id: "bg-apply-control", schemaVersion: 2, status: "review_ready", title: "Offline apply",
        createdAt: at, updatedAt: at, jobDir, repoRoot: checkout, baseRevision: base,
        runtime: { python: process.execPath, controller_source: controller }, contract: { runtime_hashes: pins } };
      const jobFile = join(jobDir, "job.json");
      const originalJob = JSON.stringify(job);
      writeFileSync(jobFile, originalJob);
      const mutate = () => {
        if (mutation === "dirty") writeFileSync(join(checkout, "caller.txt"), "caller change must be preserved\n");
        if (mutation === "head") checkedGit(["-c", "user.name=OfflineControl",
          "-c", "user.email=offline@local.invalid", "-c", "commit.gpgsign=false",
          "commit", "--allow-empty", "-m", "Concurrent HEAD move"]);
        if (mutation === "patch") writeFileSync(join(jobDir, "candidate.patch"),
          patch.replace("+approved\n", "+unapproved\n"));
      };
      let verifierCalls = 0, applyCalls = 0, error, outcome;
      const tools = new Map();
      setup({ registerTool: tool => tools.set(tool.name, tool), registerCommand() {}, on() {},
        async exec(binary, args) {
          if (binary === "git") {
            if (args[0] === "apply" && !args.includes("--check")) applyCalls++;
            return git(args);
          }
          assert.equal(binary, process.execPath, "Only the integration verifier is mocked");
          assert.ok(args.includes("--verify-proposal-only"));
          verifierCalls++;
          if (phase === "final-verifier" && verifierCalls === 2) {
            await new Promise(resolve => setTimeout(resolve, 0));
            mutate();
          }
          return { code: 0, stdout: "", stderr: "" };
        },
      });
      try {
        outcome = await tools.get("background_job_apply").execute("id", { jobId: job.id },
          undefined, undefined, { cwd: checkout, hasUI: true, ui: {
            async confirm() { if (phase === "confirmation") mutate(); return true; }, notify() {} } });
      } catch (caught) { error = caught; }
      const observed = { mutation, phase, verifierCalls, applyCalls,
        checkout_head: checkedGit(["rev-parse", "HEAD"]).trim(),
        doc: readFileSync(join(checkout, "doc.md"), "utf8"),
        retained_job_status: JSON.parse(readFileSync(jobFile, "utf8")).status,
        error: error?.message, message: outcome?.content?.[0]?.text };
      console.log("OBSERVE: native Git apply window " + JSON.stringify(observed));
      try {
        if (mutation === "none") {
          assert.equal(error, undefined);
          assert.equal(applyCalls, 1);
          assert.equal(verifierCalls, 2);
          assert.equal(observed.doc, "approved\n");
          assert.equal(observed.retained_job_status, "applied");
          assert.equal(checkedGit(["diff", "--cached"]).trim(), "", "Application remains unstaged");
        } else {
          assert.equal(applyCalls, 0, "Drift must stop before any native application");
          assert.equal(observed.doc, "before\n");
          assert.equal(readFileSync(jobFile, "utf8"), originalJob, "Rejected integration must preserve the Job");
          if (mutation === "dirty") assert.equal(readFileSync(join(checkout, "caller.txt"), "utf8"), "caller change must be preserved\n");
          if (mutation === "head") assert.notEqual(observed.checkout_head, base);
          if (mutation === "patch") assert.equal(readFileSync(join(jobDir, "candidate.patch"), "utf8"), patch.replace("+approved\n", "+unapproved\n"));
        }
        console.log("PASS: native Git integration " + mutation + " during " + phase);
      } catch (caught) { failures.push(mutation + "/" + phase + ": " + caught.message); }
    }
  } finally { process.env.DUAL_PI_STATE_DIR = previousState; }
  assert.deepEqual(failures, [], "Native Git apply must reject drift after confirmation or final verifier");
}

if (process.argv[2] === "--admission-child") {
  await runAdmissionChild();
} else {

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
  await runAdmissionControls(root);
  await runPostVerifierApplyControls(root);
  console.log("PASS: formal approval binds actual task, scope, checks, model and caps; hash mismatch stops");
} finally {
  rmSync(root, { recursive: true, force: true });
}
}
