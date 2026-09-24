import assert from "node:assert/strict";
import { copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const repositoryRoot = resolve(import.meta.dirname, "..");
const gatePath = join(repositoryRoot, ".pi", "extensions", "governance-gate.ts");
const policyPath = join(repositoryRoot, "docs", "system-development-governance.md");
const requiredContextPaths = [
  "docs/self-improvement-mvp.md",
  ".agents/skills/ephy-worker-self-improvement/SKILL.md",
  ".agents/skills/ephy-worker-self-improvement/references/eval-contract.md",
  ".agents/skills/ephy-worker-self-improvement/references/audit-contract.md",
  ".pi/prompts/audit-ephy-worker.md",
];
const baseTools = [
  "read",
  "grep",
  "find",
  "ls",
  "subagent",
  "background_job_submit",
  "background_job_status",
  "background_job_cancel",
  "bash",
  "powershell",
  "edit",
  "write",
  "background_job_apply",
];
const leadTools = [
  "read",
  "grep",
  "find",
  "ls",
  "subagent",
  "background_job_submit",
  "background_job_status",
  "background_job_cancel",
];

class FakePi {
  constructor() {
    this.activeTools = [];
    this.handlers = new Map();
    this.tools = new Map();
  }

  getActiveTools() {
    return [...this.activeTools];
  }

  getAllTools() {
    return [
      ...baseTools.map((name) => ({ name })),
      ...[...this.tools.keys()].map((name) => ({ name })),
    ];
  }

  on(name, handler) {
    assert.equal(this.handlers.has(name), false, `duplicate ${name} handler`);
    this.handlers.set(name, handler);
  }

  registerTool(tool) {
    this.tools.set(tool.name, tool);
  }

  setActiveTools(names) {
    this.activeTools = [...names];
  }

  async emit(name, event) {
    const handler = this.handlers.get(name);
    assert.ok(handler, `missing ${name} handler`);
    return handler(event);
  }
}

async function makeContextRoot() {
  const root = await mkdtemp(join(tmpdir(), "ephy-governance-gate-"));
  for (const relativePath of requiredContextPaths) {
    const destination = join(root, relativePath);
    await mkdir(dirname(destination), { recursive: true });
    await copyFile(join(repositoryRoot, relativePath), destination);
  }
  return root;
}

function envelopeFrom(section) {
  const value = (label) => {
    const match = section.match(new RegExp(`^${label}: (.+)$`, "m"));
    assert.ok(match, `missing ${label}`);
    return match[1];
  };
  return {
    policyId: value("Policy-ID"),
    policySha256: value("Policy-SHA256"),
    endMarker: value("Policy-End-Marker"),
    nonce: value("Acknowledgement-Nonce"),
    role: value("Role"),
  };
}

async function startGate(governanceGate, contextRoot, role = "lead") {
  process.env.DUAL_GOVERNANCE_ROLE = role;
  process.env.DUAL_GOVERNANCE_POLICY = policyPath;
  process.env.DUAL_GOVERNANCE_CONTEXT_ROOT = contextRoot;
  delete process.env.DUAL_JOB_RUNNER;
  delete process.env.DUAL_RUNNER_SMOKE_TEST;
  delete process.env.DUAL_AUDIT_BUNDLE_ROOT;

  const pi = new FakePi();
  governanceGate(pi);
  await pi.emit("session_start", {});
  const startEvent = { systemPromptOptions: { sections: {} } };
  await pi.emit("before_agent_start", startEvent);
  return { pi, section: startEvent.systemPromptOptions.sections.development_governance };
}

async function prepareAcknowledgement(pi, section, overrides = {}) {
  const input = { ...envelopeFrom(section), ...overrides };
  const quarantined = await pi.emit("context", {
    messages: [
      { role: "system", sections: { development_governance: section } },
      { role: "user", content: [{ type: "text", text: "original task" }] },
    ],
  });
  assert.equal(quarantined.messages.length, 2);
  assert.match(quarantined.messages[1].content[0].text, /MANDATORY GOVERNANCE BOOTSTRAP ONLY/);

  const providerPayload = await pi.emit("before_provider_request", {
    payload: {
      tools: [
        { type: "function", function: { name: "read" } },
        { type: "function", function: { name: "governance_ack" } },
      ],
    },
  });
  assert.deepEqual(providerPayload.tools.map((tool) => tool.function.name), ["governance_ack"]);
  assert.equal(providerPayload.tool_choice.function.name, "governance_ack");

  const toolCall = {
    type: "toolCall",
    id: "ack-1",
    name: "governance_ack",
    arguments: input,
  };
  const messageResult = await pi.emit("message_end", {
    message: { role: "assistant", content: [toolCall], stopReason: "toolUse" },
  });
  return { input, messageResult, toolCall };
}

async function completeAcknowledgement(pi, input) {
  const callDecision = await pi.emit("tool_call", {
    toolName: "governance_ack",
    toolCallId: "ack-1",
    input,
  });
  assert.equal(callDecision, undefined);
  const acknowledgement = await pi.tools.get("governance_ack").execute("ack-1", input);
  assert.equal(acknowledgement.details.phase, "context_delivery_pending");
  assert.deepEqual(pi.getActiveTools(), []);
  const result = await pi.emit("tool_result", {
    toolName: "governance_ack",
    toolCallId: "ack-1",
    content: acknowledgement.content,
    details: acknowledgement.details,
    isError: false,
  });
  assert.equal(result.details.phase, "ready");
  return acknowledgement;
}

async function testHappyPathAndToolCeiling(governanceGate) {
  const contextRoot = await makeContextRoot();
  try {
    const { pi, section } = await startGate(governanceGate, contextRoot);
    assert.deepEqual(pi.getActiveTools(), ["governance_ack"]);
    const { input, messageResult } = await prepareAcknowledgement(pi, section);
    assert.deepEqual(messageResult.message.content.map((item) => item.name), ["governance_ack"]);
    await completeAcknowledgement(pi, input);
    assert.deepEqual(pi.getActiveTools(), leadTools);

    const blocked = await pi.emit("tool_call", {
      toolName: "bash",
      toolCallId: "forbidden-1",
      input: {},
    });
    assert.equal(blocked.block, true);
    assert.equal(blocked.terminate, true);
    assert.deepEqual(pi.getActiveTools(), []);

    const afterLatch = await pi.emit("tool_call", {
      toolName: "read",
      toolCallId: "read-1",
      input: { path: policyPath },
    });
    assert.equal(afterLatch.block, true);
    assert.equal(afterLatch.terminate, true);
  } finally {
    await rm(contextRoot, { recursive: true, force: true });
  }
}

async function testInvalidAcknowledgementLatches(governanceGate) {
  const contextRoot = await makeContextRoot();
  try {
    const { pi, section } = await startGate(governanceGate, contextRoot);
    const { messageResult } = await prepareAcknowledgement(pi, section, {
      policySha256: "0".repeat(64),
    });
    assert.match(messageResult.message.content[0].text, /GOVERNANCE_GATE_FAILURE/);
    assert.deepEqual(pi.getActiveTools(), []);
    const blocked = await pi.emit("tool_call", {
      toolName: "read",
      toolCallId: "read-after-invalid-ack",
      input: { path: policyPath },
    });
    assert.equal(blocked.block, true);
    assert.equal(blocked.terminate, true);
  } finally {
    await rm(contextRoot, { recursive: true, force: true });
  }
}

async function testContextHashMutationLatches(governanceGate) {
  const contextRoot = await makeContextRoot();
  try {
    const { pi, section } = await startGate(governanceGate, contextRoot);
    const { input } = await prepareAcknowledgement(pi, section);
    const callDecision = await pi.emit("tool_call", {
      toolName: "governance_ack",
      toolCallId: "ack-1",
      input,
    });
    assert.equal(callDecision, undefined);
    await writeFile(join(contextRoot, requiredContextPaths[0]), "mutated\n", "utf8");
    await assert.rejects(
      pi.tools.get("governance_ack").execute("ack-1", input),
      /identity changed/,
    );
    assert.deepEqual(pi.getActiveTools(), []);
    const blocked = await pi.emit("tool_call", {
      toolName: "read",
      toolCallId: "read-after-mutation",
      input: { path: policyPath },
    });
    assert.equal(blocked.block, true);
    assert.equal(blocked.terminate, true);
  } finally {
    await rm(contextRoot, { recursive: true, force: true });
  }
}

async function testUnsupportedRoleFailsClosed(governanceGate) {
  const contextRoot = await makeContextRoot();
  try {
    const { pi, section } = await startGate(governanceGate, contextRoot, "owner");
    assert.deepEqual(pi.getActiveTools(), []);
    assert.match(section, /GOVERNANCE LOAD FAILURE/);
    assert.match(section, /Missing or unsupported DUAL_GOVERNANCE_ROLE/);
    const response = await pi.emit("message_end", {
      message: { role: "assistant", content: [{ type: "text", text: "continue" }] },
    });
    assert.match(response.message.content[0].text, /GOVERNANCE_GATE_FAILURE/);
  } finally {
    await rm(contextRoot, { recursive: true, force: true });
  }
}

process.chdir(repositoryRoot);
const { default: governanceGate } = await import(pathToFileURL(gatePath).href);
await testHappyPathAndToolCeiling(governanceGate);
await testInvalidAcknowledgementLatches(governanceGate);
await testContextHashMutationLatches(governanceGate);
await testUnsupportedRoleFailsClosed(governanceGate);
console.log("PASS: governance gate executable lifecycle smoke test (4 scenarios)");
