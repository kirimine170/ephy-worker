import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { pathToFileURL } from "node:url";

const execFileAsync = promisify(execFile);
const repositoryRoot = resolve(import.meta.dirname, "..");
const guardPath = resolve(process.argv[2] ?? join(repositoryRoot, "tools", "pi-local", "ephy-campaign-guard.ts"));
const reasoningModel = "reasoning-test-model";
const coderModel = "coder-test-model";

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

class FakePi {
  constructor() {
    this.handlers = new Map();
    this.tools = new Map();
    this.activeTools = [];
    this.followUps = [];
    this.selectedModels = [];
    this.thinkingLevels = [];
  }

  on(name, handler) {
    const handlers = this.handlers.get(name) ?? [];
    handlers.push(handler);
    this.handlers.set(name, handlers);
  }

  registerTool(tool) {
    this.tools.set(tool.name, tool);
  }

  setActiveTools(names) {
    this.activeTools = [...names];
  }

  sendUserMessage(message) {
    this.followUps.push(message);
  }

  async setModel(model) {
    this.selectedModels.push(model);
    return true;
  }

  setThinkingLevel(level) {
    this.thinkingLevels.push(level);
  }

  async exec(command, args, options = {}) {
    try {
      const result = await execFileAsync(command, args, {
        cwd: options.cwd,
        timeout: options.timeout,
        encoding: "utf8",
        windowsHide: true,
        maxBuffer: 4 * 1024 * 1024,
      });
      return { stdout: result.stdout ?? "", stderr: result.stderr ?? "", code: 0, killed: false };
    } catch (error) {
      return {
        stdout: error.stdout ?? "",
        stderr: error.stderr ?? error.message,
        code: Number.isInteger(error.code) ? error.code : 1,
        killed: error.killed === true,
      };
    }
  }

  async emit(name, event, ctx) {
    let decision;
    for (const handler of this.handlers.get(name) ?? []) {
      const value = await handler(event, ctx);
      if (value !== undefined) decision = value;
    }
    return decision;
  }
}

function context(root, modelId) {
  const models = [
    { provider: "dual-local", id: reasoningModel },
    { provider: "dual-local", id: coderModel },
  ];
  return {
    cwd: root,
    model: { provider: "dual-local", id: modelId },
    scopedModels: models.map((model) => ({ model })),
    modelRegistry: { getAvailable: () => models },
    aborted: false,
    shutdownRequested: false,
    abort() {
      this.aborted = true;
    },
    shutdown() {
      this.shutdownRequested = true;
    },
    hasPendingMessages() {
      return false;
    },
    ui: {
      notify() {},
      setStatus() {},
    },
  };
}

async function git(cwd, ...args) {
  await execFileAsync("git", args, { cwd, encoding: "utf8", windowsHide: true });
}

async function makeFixture(name, overrides = {}) {
  const parent = await mkdtemp(join(tmpdir(), `ephy-campaign-${name}-`));
  const candidate = join(parent, "candidate");
  const evidence = join(parent, "evidence");
  await mkdir(candidate);
  await mkdir(evidence);
  await mkdir(join(candidate, ".campaign"));
  await mkdir(join(candidate, "src"));
  await writeFile(join(candidate, "src", "allowed.txt"), "before\n", "utf8");
  await writeFile(join(candidate, "README.md"), "fixture\n", "utf8");
  const taskDocument = "# Frozen task\n\nOnly src/allowed.txt may change.\n";
  await writeFile(join(candidate, ".campaign", "TASKS.md"), taskDocument, "utf8");
  await git(candidate, "init", "-q");
  await git(candidate, "add", ".");
  await git(
    candidate,
    "-c",
    "user.name=Campaign Test",
    "-c",
    "user.email=campaign@example.invalid",
    "commit",
    "-q",
    "-m",
    "fixture",
  );
  const { stdout: headOutput } = await execFileAsync("git", ["rev-parse", "HEAD"], {
    cwd: candidate,
    encoding: "utf8",
    windowsHide: true,
  });
  const passingCheck = {
    name: "node-pass",
    executable: process.execPath,
    args: ["-e", "process.exit(0)"],
    timeoutMs: 10_000,
  };
  const contract = {
    schemaVersion: 1,
    campaignId: `campaign-${name}`,
    controllerSha256: sha256(await readFile(guardPath)),
    candidateRoot: candidate,
    evidenceRoot: evidence,
    baseCommit: headOutput.trim(),
    reasoningModel,
    coderModel,
    maxDurationSeconds: 600,
    maxIdleContinuations: 2,
    maxPlanningToolCalls: 16,
    maxPlanningCompactions: 2,
    maxProtocolCorrections: 2,
    taskDocument: {
      path: ".campaign/TASKS.md",
      sha256: sha256(taskDocument),
    },
    tasks: [
      {
        id: 1,
        title: "Guard smoke task",
        instruction: "Change the allowed fixture only.",
        allowedFiles: ["src/allowed.txt", "src/new.txt"],
        checks: [passingCheck],
        maxRepairs: 0,
      },
    ],
    finalChecks: [passingCheck],
    ...overrides,
  };
  const contractPath = join(evidence, "campaign-contract.json");
  const contractText = `${JSON.stringify(contract, null, 2)}\n`;
  await writeFile(contractPath, contractText, "utf8");
  return { parent, candidate, evidence, contract, contractPath, contractSha256: sha256(contractText) };
}

async function createGuard(fixture) {
  process.env.EPHY_PI_CAMPAIGN_CONTRACT = fixture.contractPath;
  process.env.EPHY_PI_CAMPAIGN_CONTRACT_SHA256 = fixture.contractSha256;
  const module = await import(`${pathToFileURL(guardPath).href}?case=${Date.now()}-${Math.random()}`);
  const pi = new FakePi();
  module.default(pi);
  return pi;
}

function resetEnvironment() {
  delete process.env.EPHY_PI_CAMPAIGN_CONTRACT;
  delete process.env.EPHY_PI_CAMPAIGN_CONTRACT_SHA256;
}

async function preflight(pi, fixture) {
  const ctx = context(fixture.candidate, reasoningModel);
  await pi.emit("session_start", {}, ctx);
  assert.equal(ctx.shutdownRequested, false);
  assert.deepEqual(pi.activeTools, ["read", "grep", "find", "ls", "ephy_campaign_control"]);
  return ctx;
}

async function testAutomaticContinuationIsBounded() {
  const fixture = await makeFixture("continuation", { maxIdleContinuations: 2 });
  try {
    const pi = await createGuard(fixture);
    const ctx = await preflight(pi, fixture);
    await pi.emit("agent_end", {}, ctx);
    await pi.emit("agent_end", {}, ctx);
    assert.equal(pi.followUps.length, 2);
    await pi.emit("agent_end", {}, ctx);
    assert.equal(ctx.aborted, true);
    assert.equal(ctx.shutdownRequested, true);
    assert.deepEqual(pi.activeTools, []);
    const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(state.status, "failed");
    assert.equal(state.stopReason, "idle_continuation_limit");
  } finally {
    resetEnvironment();
    await rm(fixture.parent, { recursive: true, force: true });
  }
}

async function testOutOfPhaseAndOutOfScopeWritesStopBeforeExecution() {
  const planningFixture = await makeFixture("planning-write");
  try {
    const pi = await createGuard(planningFixture);
    const ctx = await preflight(pi, planningFixture);
    const decision = await pi.emit(
      "tool_call",
      { toolName: "write", toolCallId: "bad-write", input: { path: "src/allowed.txt" } },
      ctx,
    );
    assert.equal(decision.block, true);
    assert.equal(decision.terminate, true);
    assert.equal(await readFile(join(planningFixture.candidate, "src", "allowed.txt"), "utf8"), "before\n");
    const state = JSON.parse(await readFile(join(planningFixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(state.stopReason, "write_outside_coder_phase");
  } finally {
    resetEnvironment();
    await rm(planningFixture.parent, { recursive: true, force: true });
  }

  const scopeFixture = await makeFixture("scope-write");
  try {
    const pi = await createGuard(scopeFixture);
    const reasoning = await preflight(pi, scopeFixture);
    const control = pi.tools.get("ephy_campaign_control");
    await control.execute(
      "start",
      { action: "start_implementation", taskId: 1, summary: "Inspect the fixture and make one bounded change." },
      undefined,
      undefined,
      reasoning,
    );
    const coder = context(scopeFixture.candidate, coderModel);
    const decision = await pi.emit(
      "tool_call",
      { toolName: "write", toolCallId: "scope-write", input: { path: "tests/test_resolve_repository.py" } },
      coder,
    );
    assert.equal(decision.block, true);
    assert.equal(decision.terminate, true);
    assert.equal(coder.shutdownRequested, true);
    const state = JSON.parse(await readFile(join(scopeFixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(state.stopReason, "write_scope_violation");
  } finally {
    resetEnvironment();
    await rm(scopeFixture.parent, { recursive: true, force: true });
  }
}

async function testPlanningBudgetsStopReadAndCompactionLoops() {
  const toolFixture = await makeFixture("planning-tools", { maxPlanningToolCalls: 2 });
  try {
    const pi = await createGuard(toolFixture);
    const ctx = await preflight(pi, toolFixture);
    for (let count = 0; count < 2; count += 1) {
      assert.equal(await pi.emit("tool_call", { toolName: "read", input: { path: "README.md" } }, ctx), undefined);
    }
    const stopped = await pi.emit("tool_call", { toolName: "read", input: { path: "README.md" } }, ctx);
    assert.equal(stopped.block, true);
    assert.equal(stopped.terminate, true);
    const state = JSON.parse(await readFile(join(toolFixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(state.stopReason, "planning_tool_call_limit");
    assert.equal(state.planningToolCalls, 3);
  } finally {
    resetEnvironment();
    await rm(toolFixture.parent, { recursive: true, force: true });
  }

  const compactFixture = await makeFixture("planning-compactions", { maxPlanningCompactions: 1 });
  try {
    const pi = await createGuard(compactFixture);
    const ctx = await preflight(pi, compactFixture);
    await pi.emit("session_compact", {}, ctx);
    assert.equal(ctx.shutdownRequested, false);
    await pi.emit("session_compact", {}, ctx);
    assert.equal(ctx.shutdownRequested, true);
    const state = JSON.parse(await readFile(join(compactFixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(state.stopReason, "planning_compaction_limit");
    assert.equal(state.planningCompactions, 2);
  } finally {
    resetEnvironment();
    await rm(compactFixture.parent, { recursive: true, force: true });
  }
}

async function testHappyPathRequiresRoleSwitchChecksAndReview() {
  const fixture = await makeFixture("happy");
  try {
    const pi = await createGuard(fixture);
    const reasoning = await preflight(pi, fixture);
    const control = pi.tools.get("ephy_campaign_control");
    const start = await control.execute(
      "start",
      { action: "start_implementation", taskId: 1, summary: "Make the exact fixture change and preserve scope." },
      undefined,
      undefined,
      reasoning,
    );
    assert.equal(start.details.phase, "implementing");
    assert.equal(pi.selectedModels.at(-1).id, coderModel);
    assert.equal(pi.thinkingLevels.at(-1), "off");
    assert.deepEqual(pi.activeTools, ["read", "grep", "find", "ls", "ephy_campaign_control", "edit", "write"]);

    const wrongSwitch = await pi.emit(
      "tool_call",
      { toolName: "ephy_select_model", toolCallId: "switch", input: { target: "reasoning" } },
      reasoning,
    );
    assert.equal(wrongSwitch.block, true);
    assert.equal(reasoning.shutdownRequested, true);
  } finally {
    resetEnvironment();
    await rm(fixture.parent, { recursive: true, force: true });
  }

  const successFixture = await makeFixture("success");
  try {
    const pi = await createGuard(successFixture);
    const reasoning = await preflight(pi, successFixture);
    const control = pi.tools.get("ephy_campaign_control");
    const promptEvent = { systemPrompt: "Existing policy", systemPromptOptions: { sections: { policy: "Existing policy" } } };
    const initial = await pi.emit("before_agent_start", promptEvent, reasoning);
    assert.equal(initial, undefined);
    assert.equal(promptEvent.systemPromptOptions.sections.policy, "Existing policy");
    assert.doesNotMatch(promptEvent.systemPromptOptions.sections.ephy_campaign, /Current phase:|action=start_implementation/);
    const userMessage = { role: "user", content: "Frozen task", timestamp: 1 };
    const plannedContext = await pi.emit("context", { messages: [userMessage] }, reasoning);
    assert.match(plannedContext.messages.find((m) => m.customType === "ephy-campaign-current-state").content, /Current phase: planning/);
    await control.execute(
      "start",
      { action: "start_implementation", taskId: 1, summary: "Make the exact fixture change and preserve scope." },
      undefined,
      undefined,
      reasoning,
    );
    const coder = context(successFixture.candidate, coderModel);
    // No new before_agent_start: model routing happens inside one agent run.
    const implementingContext = await pi.emit("context", { messages: plannedContext.messages }, coder);
    assert.equal(implementingContext.messages.length, 3);
    assert.deepEqual(implementingContext.messages[0], userMessage);
    assert.match(implementingContext.messages.find((m) => m.customType === "ephy-campaign-current-state").content, /Current phase: implementing/);
    assert.match(implementingContext.messages.at(-1).content, /action=submit_implementation/);
    assert.doesNotMatch(implementingContext.messages.at(-1).content, /action=start_implementation/);
    const originalPlan = await readFile(join(successFixture.evidence, "task-1-plan.md"), "utf8");
    const originalSnapshot = await readFile(join(successFixture.evidence, "task-1-before-snapshot.json"), "utf8");
    for (let count = 1; count <= 2; count += 1) {
      const duplicate = await control.execute(
        `duplicate-${count}`,
        { action: "start_implementation", taskId: 1, summary: "This must not replace the real plan." },
        undefined, undefined, coder,
      );
      assert.equal(duplicate.details.recoverable, true);
      assert.equal(duplicate.details.protocolCorrections, count);
      assert.equal(duplicate.details.phase, "implementing");
      assert.equal(duplicate.details.nextAction, "implement_then_submit");
      assert.notEqual(duplicate.terminate, true);
      assert.match(duplicate.content[0].text, /action=submit_implementation/);
    }
    assert.equal(coder.shutdownRequested, false);
    assert.equal(coder.aborted, false);
    assert.equal(pi.selectedModels.length, 1);
    assert.equal(await readFile(join(successFixture.evidence, "task-1-plan.md"), "utf8"), originalPlan);
    assert.equal(await readFile(join(successFixture.evidence, "task-1-before-snapshot.json"), "utf8"), originalSnapshot);
    assert.equal(await readFile(join(successFixture.candidate, "src", "allowed.txt"), "utf8"), "before\n");
    const correctionState = JSON.parse(await readFile(join(successFixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(correctionState.repairCount, 0);
    assert.equal(correctionState.idleContinuations, 0);
    // Even if compaction removes the old phase message, reconstruct from state.
    await pi.emit("session_compact", {}, coder);
    const compactedContext = await pi.emit("context", { messages: [userMessage] }, coder);
    assert.match(compactedContext.messages.find((m) => m.customType === "ephy-campaign-current-state").content, /Current phase: implementing/);
    assert.deepEqual(pi.activeTools, ["read", "grep", "find", "ls", "ephy_campaign_control", "edit", "write"]);
    const allowed = await pi.emit(
      "tool_call",
      { toolName: "write", toolCallId: "allowed", input: { path: "src/allowed.txt" } },
      coder,
    );
    assert.equal(allowed, undefined);
    await writeFile(join(successFixture.candidate, "src", "allowed.txt"), "after\n", "utf8");
    const allowedNew = await pi.emit(
      "tool_call",
      { toolName: "write", toolCallId: "allowed-new", input: { path: "src/new.txt" } },
      coder,
    );
    assert.equal(allowedNew, undefined);
    await writeFile(join(successFixture.candidate, "src", "new.txt"), "new file\n", "utf8");
    const submitted = await control.execute(
      "submit",
      { action: "submit_implementation", taskId: 1, summary: "Changed only the allowed fixture." },
      undefined,
      undefined,
      coder,
    );
    assert.equal(submitted.details.phase, "reviewing");
    assert.equal(submitted.details.results[0].exitCode, 0);
    assert.equal(pi.selectedModels.at(-1).id, reasoningModel);
    assert.equal(pi.thinkingLevels.at(-1), "medium");
    const review = context(successFixture.candidate, reasoningModel);
    const reviewedContext = await pi.emit("context", { messages: implementingContext.messages }, review);
    assert.equal(reviewedContext.messages.length, 3);
    assert.match(reviewedContext.messages.find((m) => m.customType === "ephy-campaign-current-state").content, /Current phase: reviewing/);
    assert.match(reviewedContext.messages.at(-1).content, /action=review_task/);
    const completed = await control.execute(
      "review",
      { action: "review_task", taskId: 1, summary: "Diff and fixed checks match the contract.", verdict: "PASS" },
      undefined,
      undefined,
      review,
    );
    assert.equal(completed.details.phase, "complete");
    assert.equal(completed.terminate, true);
    assert.equal(review.shutdownRequested, true);
    const state = JSON.parse(await readFile(join(successFixture.evidence, "campaign-state.json"), "utf8"));
    assert.equal(state.status, "complete");
    assert.deepEqual(state.completedTaskIds, [1]);
    assert.match(await readFile(join(successFixture.evidence, "final-report.md"), "utf8"), /Result: PASS/);
    assert.match(await readFile(join(successFixture.evidence, "combined.patch"), "utf8"), /after/);
    assert.match(await readFile(join(successFixture.evidence, "combined.patch"), "utf8"), /new file mode/);
  } finally {
    resetEnvironment();
    await rm(successFixture.parent, { recursive: true, force: true });
  }
}

async function testCorrectionsAreBoundedAndDoNotRelaxOtherGates() {
  for (const scenario of ["limit", "disabled", "wrong-model", "wrong-task", "wrong-review", "context-model"]) {
    const fixture = await makeFixture(`protocol-${scenario}`, {
      maxProtocolCorrections: scenario === "disabled" ? undefined : 2,
    });
    try {
      const pi = await createGuard(fixture);
      const reasoning = await preflight(pi, fixture);
      const control = pi.tools.get("ephy_campaign_control");
      const start = { action: "start_implementation", taskId: 1, summary: "Bounded change." };
      await control.execute("start", start, undefined, undefined, reasoning);
      const coder = context(fixture.candidate, coderModel);
      let expectedReason;
      if (scenario === "limit") {
        await control.execute("duplicate-1", start, undefined, undefined, coder);
        await control.execute("duplicate-2", start, undefined, undefined, coder);
        // Compaction and another user prompt must not reset the retry budget.
        await pi.emit("session_compact", {}, coder);
        await pi.emit("before_agent_start", { systemPromptOptions: { sections: {} } }, coder);
        await control.execute("duplicate-3", start, undefined, undefined, coder);
        expectedReason = "protocol_correction_limit";
      } else if (scenario === "disabled") {
        await control.execute("duplicate", start, undefined, undefined, coder);
        expectedReason = "protocol_correction_limit";
      } else if (scenario === "wrong-model") {
        await control.execute("duplicate", start, undefined, undefined, reasoning);
        expectedReason = "invalid_planning_transition";
      } else if (scenario === "wrong-task") {
        await control.execute("duplicate", { ...start, taskId: 2 }, undefined, undefined, coder);
        expectedReason = "task_identity_mismatch";
      } else if (scenario === "wrong-review") {
        await control.execute("skip-check", { action: "review_task", taskId: 1, summary: "Skip verification.", verdict: "PASS" }, undefined, undefined, coder);
        expectedReason = "invalid_review_transition";
      } else {
        await pi.emit("context", { messages: [] }, reasoning);
        expectedReason = "phase_model_mismatch";
      }
      const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json"), "utf8"));
      assert.equal(state.status, "failed", scenario);
      assert.equal(state.stopReason, expectedReason, scenario);
      assert.equal(coder.shutdownRequested || reasoning.shutdownRequested, true, scenario);
      assert.deepEqual(pi.activeTools, [], scenario);
      assert.equal(pi.selectedModels.length, 1, scenario);
      assert.equal(await readFile(join(fixture.candidate, "src", "allowed.txt"), "utf8"), "before\n");
    } finally {
      resetEnvironment();
      await rm(fixture.parent, { recursive: true, force: true });
    }
  }
}

async function testWorkQueueRequiresEachStepCheckAndPreservesToolHistory() {
  const fixture = await makeFixture("work-queue");
  try {
    const contentCheck = (value) => ({
      name: `expect-${value}`, executable: process.execPath,
      args: ["-e", `if(require('fs').readFileSync('src/allowed.txt','utf8')!==${JSON.stringify(value + "\n")})process.exit(1)`], timeoutMs: 10000,
    });
    const contract = {
      ...fixture.contract, schemaVersion: 2, goal: "Complete both requirements in order.",
      constraints: "Retain all checks. " + "Only the frozen fixture may change. ".repeat(100),
      maxProgressRecoveries: 1,
      tasks: [{
        id: 20, title: "Compound task", instruction: "Complete every listed step.",
        allowedFiles: ["src/allowed.txt"], maxRepairs: 0, checks: [contentCheck("two")],
        steps: ["one", "two"].map((value) => ({
          id: value, title: value, instruction: `Set the fixture to ${value}.`,
          allowedFiles: ["src/allowed.txt"], checks: [contentCheck(value)],
        })),
      }], finalChecks: [contentCheck("two")],
    };
    const contractText = JSON.stringify(contract);
    await writeFile(fixture.contractPath, contractText);
    fixture.contractSha256 = sha256(contractText);
    const pi = await createGuard(fixture);
    const reasoning = await preflight(pi, fixture);
    const coder = context(fixture.candidate, coderModel);
    const control = pi.tools.get("ephy_campaign_control");
    const originalUser = { role: "user", content: "Complete both work items and preserve all constraints.", timestamp: 1 };
    for (const [index, value] of ["one", "two"].entries()) {
      const taskId = index + 1;
      await control.execute(`start-${taskId}`, { action: "start_implementation", taskId, summary: `Set fixture to ${value}.` }, undefined, undefined, reasoning);
      const history = [
        originalUser,
        { role: "assistant", content: "Obsolete planning exploration", timestamp: 2 },
        { role: "toolResult", toolCallId: `start-${taskId}`, toolName: "ephy_campaign_control", content: [], timestamp: 3 },
        { role: "assistant", content: [{ type: "toolCall", id: "read-now", name: "read", arguments: { path: "src/allowed.txt" } }], timestamp: 4 },
        { role: "toolResult", toolCallId: "read-now", toolName: "read", content: [{ type: "text", text: "actual file" }], timestamp: 5 },
      ];
      const focused = await pi.emit("context", { messages: history }, coder);
      assert.equal(focused.messages.at(-1).role, "toolResult");
      assert.equal(focused.messages.at(-1).toolCallId, "read-now");
      assert.deepEqual(focused.messages[0], originalUser);
      assert.ok(!JSON.stringify(focused).includes("Obsolete planning exploration"));
      const packet = focused.messages.find((message) => message.customType === "ephy-campaign-current-state");
      assert.match(packet.content, new RegExp(`Current task: ${taskId}`));
      assert.match(packet.content, new RegExp(`Set the fixture to ${value}`));
      const wire = await pi.emit("before_provider_request", { payload: {
        messages: [{ role: "system", content: "Existing policy" }, { role: "user", content: packet.content }, { role: "tool", content: "actual file", tool_call_id: "read-now" }],
        tools: [{ type: "function", function: { name: "edit" } }],
      } }, coder);
      assert.match(wire.messages[0].content, /^Existing policy/);
      assert.equal(wire.messages[0].role, "system");
      assert.match(wire.messages[0].content, /YOUR CURRENT ROLE IS IMPLEMENTER/);
      assert.equal(wire.messages.at(-1).role, "tool");
      assert.equal(wire.messages.filter((message) => message.content === packet.content).length, 0);
      await pi.emit("tool_call", { toolName: "read", toolCallId: "read-once", input: { path: "src/allowed.txt" } }, coder);
      const stable = await pi.emit("context", { messages: history }, coder);
      assert.equal(stable.messages.find((message) => message.customType === "ephy-campaign-current-state").content, packet.content, "Read counters must not invalidate the system prompt prefix cache");
      const empty = await control.execute("empty", { action: "submit_implementation", taskId, summary: "Claiming completion without editing." }, undefined, undefined, coder);
      assert.equal(empty.details.recoverable, true);
      assert.equal(coder.shutdownRequested, false);
      await pi.emit("tool_call", { toolName: "write", toolCallId: `write-${taskId}`, input: { path: "src/allowed.txt" } }, coder);
      await writeFile(join(fixture.candidate, "src/allowed.txt"), `${value}\n`);
      await pi.emit("tool_result", { toolName: "write", toolCallId: `write-${taskId}`, isError: false }, coder);
      await pi.emit("session_compact", {}, coder);
      const recovered = await pi.emit("context", { messages: [originalUser] }, coder);
      assert.match(recovered.messages.find((m) => m.customType === "ephy-campaign-current-state").content, new RegExp(`Current task: ${taskId}`));
      assert.equal(JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json"))).observedEdits, 1);
      const submitted = await control.execute("submit", { action: "submit_implementation", taskId, summary: `Changed to ${value}.` }, undefined, undefined, coder);
      assert.equal(submitted.details.phase, "reviewing");
      assert.equal(submitted.details.results.length, taskId === 1 ? 1 : 2);
      const reviewContext = await pi.emit("context", { messages: [originalUser] }, reasoning);
      const reviewPacket = reviewContext.messages.find((m) => m.customType === "ephy-campaign-current-state").content;
      assert.match(reviewPacket, /YOUR CURRENT ROLE IS REVIEWER/);
      assert.match(reviewPacket, /diff --git a\/src\/allowed.txt/);
      assert.match(reviewPacket, /"exitCode": 0/);
      assert.match(reviewPacket, new RegExp(submitted.details.patchSha256));
      assert.match(reviewContext.messages.at(-1).content, /Current request: REVIEW/);
      const retry = { role: "user", content: "Call the review tool now, not JSON prose.", timestamp: 8 };
      const idleContext = await pi.emit("context", { messages: [originalUser,
        { role: "toolResult", toolCallId: "submit", toolName: "ephy_campaign_control", content: [], timestamp: 6 },
        { role: "assistant", content: "Old prose response", timestamp: 7 }, retry,
      ] }, reasoning);
      assert.deepEqual(idleContext.messages.at(-1), retry, "A new user correction must remain after the assistant answer it corrects");
      const reviewWire = await pi.emit("before_provider_request", { payload: { messages: [{ role: "system", content: "Existing policy" }] } }, reasoning);
      assert.match(reviewWire.messages[0].content, /BEGIN CANDIDATE EVIDENCE/);
      const incompleteReview = await control.execute("missing-verdict", { action: "review_task", taskId, summary: "Read the evidence.", patchSha256: submitted.details.patchSha256, reviewedFiles: ["src/allowed.txt"] }, undefined, undefined, reasoning);
      assert.equal(incompleteReview.details.recoverable, true);
      assert.equal(JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json"))).phase, "reviewing");
      await control.execute("review", { action: "review_task", taskId, summary: "Patch and check agree.", verdict: "PASS", patchSha256: submitted.details.patchSha256, reviewedFiles: ["src/allowed.txt"] }, undefined, undefined, reasoning);
      const progress = JSON.parse(await readFile(join(fixture.evidence, "work-progress.json")));
      assert.equal(progress.items[index].status, "verified_and_reviewed");
      assert.equal(progress.items[index].sourceTaskId, 20);
      if (taskId === 1) assert.equal(progress.items[1].status, "planning");
    }
    const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json")));
    assert.equal(state.phase, "complete");
    assert.deepEqual(state.completedTaskIds, [1, 2]);
    assert.deepEqual(state.pendingTaskIds, []);
  } finally { resetEnvironment(); await rm(fixture.parent, { recursive: true, force: true }); }
}

async function testReviewMustIdentifyDeliveredEvidence() {
  for (const scenario of ["missing", "hash", "files", "not-delivered", "too-large", "mutating-final", "missing-verdict-limit"]) {
    const fixture = await makeFixture(`review-identity-${scenario}`);
    try {
      const parent = fixture.contract.tasks[0];
      if (scenario === "mutating-final") fixture.contract.finalChecks = [{ name: "mutating-check", executable: process.execPath, args: ["-e", "require('fs').writeFileSync('src/allowed.txt','changed after review\\n')"], timeoutMs: 10000 }];
      const text = JSON.stringify({ ...fixture.contract, schemaVersion: 2, goal: "Change one fixture.", constraints: "Only the fixture.", tasks: [{ ...parent, steps: [{ id: "one", title: "One", instruction: parent.instruction, allowedFiles: parent.allowedFiles, checks: parent.checks }] }] });
      await writeFile(fixture.contractPath, text);
      fixture.contractSha256 = sha256(text);
      const pi = await createGuard(fixture);
      const reasoning = await preflight(pi, fixture);
      const coder = context(fixture.candidate, coderModel);
      const control = pi.tools.get("ephy_campaign_control");
      await control.execute("start", { action: "start_implementation", taskId: 1, summary: "Change fixture." }, undefined, undefined, reasoning);
      await writeFile(join(fixture.candidate, "src/allowed.txt"), scenario === "too-large" ? "large evidence\n".repeat(2500) : "after\n");
      const submitted = await control.execute("submit", { action: "submit_implementation", taskId: 1, summary: "Changed fixture." }, undefined, undefined, coder);
      if (scenario === "too-large") {
        assert.equal(submitted.terminate, true);
        const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json")));
        assert.equal(state.stopReason, "review_evidence_too_large");
        assert.deepEqual(state.completedTaskIds, []);
        continue;
      }
      const params = { action: "review_task", taskId: 1, summary: "Claim PASS.", verdict: "PASS", patchSha256: submitted.details.patchSha256, reviewedFiles: ["src/allowed.txt"] };
      if (scenario === "missing") delete params.patchSha256;
      if (scenario === "hash") params.patchSha256 = "0".repeat(64);
      if (scenario === "files") params.reviewedFiles = ["README.md"];
      if (scenario === "mutating-final") await pi.emit("before_provider_request", { payload: { messages: [{ role: "system", content: "Policy" }] } }, reasoning);
      if (scenario === "missing-verdict-limit") {
        await pi.emit("before_provider_request", { payload: { messages: [{ role: "system", content: "Policy" }] } }, reasoning);
        delete params.verdict;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          const correction = await control.execute(`missing-${attempt}`, params, undefined, undefined, reasoning);
          assert.equal(correction.details.recoverable, true);
          assert.deepEqual(JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json"))).completedTaskIds, []);
        }
      }
      const result = await control.execute("review", params, undefined, undefined, reasoning);
      assert.equal(result.terminate, true);
      const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json")));
      assert.equal(state.stopReason, scenario === "missing-verdict-limit" ? "review_missing" : scenario === "mutating-final" ? "candidate_changed_during_final_checks" : scenario === "not-delivered" ? "review_evidence_not_delivered" : "review_evidence_identity_mismatch");
      if (scenario !== "mutating-final") assert.deepEqual(state.completedTaskIds, []);
    } finally { resetEnvironment(); await rm(fixture.parent, { recursive: true, force: true }); }
  }
}

async function testEarlyQueueAdvanceIsRejectedAndCorrectedOnlyFinitely() {
  const fixture = await makeFixture("early-queue");
  try {
    const parent = fixture.contract.tasks[0];
    const text = JSON.stringify({ ...fixture.contract, schemaVersion: 2, goal: "Two files in order.", constraints: "Never skip a gate.", tasks: [{ ...parent, steps: parent.allowedFiles.map((file, index) => ({ id: String(index + 1), title: file, instruction: "Change this file only.", allowedFiles: [file], checks: parent.checks })) }] });
    await writeFile(fixture.contractPath, text);
    fixture.contractSha256 = sha256(text);
    const pi = await createGuard(fixture);
    const reasoning = await preflight(pi, fixture);
    const coder = context(fixture.candidate, coderModel);
    await pi.tools.get("ephy_campaign_control").execute("start", { action: "start_implementation", taskId: 1, summary: "Change first file." }, undefined, undefined, reasoning);
    await pi.emit("tool_call", { toolName: "write", toolCallId: "edit-first", input: { path: "src/allowed.txt" } }, coder);
    await writeFile(join(fixture.candidate, "src/allowed.txt"), "after\n");
    await pi.emit("tool_result", { toolName: "write", toolCallId: "edit-first", isError: false }, coder);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const result = await pi.emit("tool_call", { toolName: "write", toolCallId: `future-${attempt}`, input: { path: "src/new.txt" } }, coder);
      assert.equal(result.block, true);
      const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json")));
      assert.deepEqual(state.completedTaskIds, []);
      assert.equal(state.taskId, 1);
      if (attempt < 2) {
        assert.notEqual(result.terminate, true);
        assert.match(result.reason, /submit_implementation/);
        assert.equal(state.phase, "implementing");
      } else {
        assert.equal(result.terminate, true);
        assert.equal(state.stopReason, "write_scope_violation");
      }
    }
    await assert.rejects(readFile(join(fixture.candidate, "src/new.txt")), { code: "ENOENT" });
  } finally { resetEnvironment(); await rm(fixture.parent, { recursive: true, force: true }); }
}

async function testReadLoopRecoveryIsBoundedAndNoOpWritesDoNotResetIt() {
  const fixture = await makeFixture("read-loop", { maxRepeatedReads: 1, maxReadOnlyToolCalls: 5, maxProgressRecoveries: 1 });
  try {
    const pi = await createGuard(fixture);
    const reasoning = await preflight(pi, fixture);
    await pi.tools.get("ephy_campaign_control").execute("start", { action: "start_implementation", taskId: 1, summary: "Make a change." }, undefined, undefined, reasoning);
    const coder = context(fixture.candidate, coderModel);
    const read = { toolName: "read", input: { path: "src/allowed.txt", offset: 1, limit: 10 } };
    assert.equal(await pi.emit("tool_call", read, coder), undefined);
    const correction = await pi.emit("tool_call", read, coder);
    assert.equal(correction.block, true);
    assert.notEqual(correction.terminate, true);
    await pi.emit("tool_call", { toolName: "write", toolCallId: "no-op", input: { path: "src/allowed.txt" } }, coder);
    await pi.emit("tool_result", { toolName: "write", toolCallId: "no-op", isError: false }, coder);
    const stopped = await pi.emit("tool_call", read, coder);
    assert.equal(stopped.terminate, true);
    const state = JSON.parse(await readFile(join(fixture.evidence, "campaign-state.json")));
    assert.equal(state.stopReason, "repeated_read_without_change");
    assert.equal(state.observedEdits, 0);
    assert.equal(state.progressRecoveries, 1);
  } finally { resetEnvironment(); await rm(fixture.parent, { recursive: true, force: true }); }
}

async function testInvalidWorkQueueIsRejectedBeforeExecution() {
  for (const scenario of ["empty", "scope", "duplicate", "checks"]) {
    const fixture = await makeFixture(`invalid-queue-${scenario}`);
    try {
      const step = { id: "one", title: "One", instruction: "Change only the fixture.", allowedFiles: ["src/allowed.txt"], checks: fixture.contract.finalChecks };
      const parent = { ...fixture.contract.tasks[0], steps: [step] };
      if (scenario === "empty") parent.steps = [];
      if (scenario === "scope") step.allowedFiles = ["README.md"];
      if (scenario === "duplicate") parent.steps = [step, { ...step }];
      if (scenario === "checks") step.checks = [];
      const text = JSON.stringify({ ...fixture.contract, schemaVersion: 2, goal: "Goal", constraints: "Scope", tasks: [parent] });
      await writeFile(fixture.contractPath, text);
      fixture.contractSha256 = sha256(text);
      await assert.rejects(createGuard(fixture), /frozen steps|subset of parent scope|step IDs|fixed checks/);
      assert.equal(await readFile(join(fixture.candidate, "src/allowed.txt"), "utf8"), "before\n");
    } finally { resetEnvironment(); await rm(fixture.parent, { recursive: true, force: true }); }
  }
}

export { makeFixture, createGuard, preflight, context, resetEnvironment, sha256, coderModel, reasoningModel };

if (import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
await testInvalidWorkQueueIsRejectedBeforeExecution();
await testReviewMustIdentifyDeliveredEvidence();
await testEarlyQueueAdvanceIsRejectedAndCorrectedOnlyFinitely();
await testWorkQueueRequiresEachStepCheckAndPreservesToolHistory();
await testReadLoopRecoveryIsBoundedAndNoOpWritesDoNotResetIt();
await testAutomaticContinuationIsBounded();
await testOutOfPhaseAndOutOfScopeWritesStopBeforeExecution();
await testPlanningBudgetsStopReadAndCompactionLoops();
await testHappyPathRequiresRoleSwitchChecksAndReview();
await testCorrectionsAreBoundedAndDoNotRelaxOtherGates();
console.log("PASS: campaign lifecycle, atomic routing, fresh phase context, bounded protocol correction, planning, scope, check, and review gates");
}
