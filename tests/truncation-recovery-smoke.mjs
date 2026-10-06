import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const repositoryRoot = resolve(import.meta.dirname, "..");
const extensionPath = resolve(repositoryRoot, ".pi", "extensions", "truncation-recovery.ts");

class FakePi {
  constructor() {
    this.commands = new Map();
    this.handlers = new Map();
  }

  appendEntry() {}
  sendUserMessage() {}
  on(name, handler) { this.handlers.set(name, handler); }
  registerCommand(name, command) { this.commands.set(name, command); }
  async emit(name, event, ctx = {}) { return this.handlers.get(name)(event, ctx); }
}

const { default: truncationRecovery } = await import(pathToFileURL(extensionPath).href);
const pi = new FakePi();
truncationRecovery(pi);

const governanceText = `prefix ${"g".repeat(12_000)} ${"BEGIN REQUIRED GOVERNANCE CONTEXT BUNDLE"} suffix`;
const governanceMessage = {
  role: "toolResult",
  toolName: "governance_ack",
  content: [{ type: "text", text: governanceText }],
};
const protectedResult = await pi.emit("context", { messages: [governanceMessage] });
assert.equal(protectedResult, undefined);
assert.equal(governanceMessage.content[0].text, governanceText);

const ordinaryResult = await pi.emit("context", {
  messages: [{ role: "toolResult", toolName: "read", content: [{ type: "text", text: "x".repeat(12_000) }] }],
});
assert.equal(ordinaryResult.messages[0].content[0].text.length < 10_000, true);
assert.match(ordinaryResult.messages[0].content[0].text, /characters omitted/);

process.env.DUAL_GOVERNANCE_ROLE = "lead";
const compaction = await pi.emit("session_before_compact", {
  reason: "threshold",
  customInstructions: "Preserve the current Job ID.",
  preparation: {
    fileOps: {
      read: new Set(["docs/read.md", "src/changed.py"]),
      written: new Set(["src/new.py"]),
      edited: new Set(["src/changed.py"]),
    },
    messagesToSummarize: [{ role: "user", content: [{ type: "text", text: "Continue the bounded task." }] }],
    turnPrefixMessages: [],
    previousSummary: undefined,
    tokensBefore: 31_000,
    isSplitTurn: false,
    firstKeptEntryId: "kept-1",
  },
}, {
  cwd: repositoryRoot,
  hasUI: false,
  ui: { notify() {} },
});
assert.equal(compaction.compaction.firstKeptEntryId, "kept-1");
assert.equal(compaction.compaction.details.role, "lead");
assert.deepEqual(compaction.compaction.details.modifiedFiles, ["src/changed.py", "src/new.py"]);
assert.deepEqual(compaction.compaction.details.readFiles, ["docs/read.md"]);
assert.match(compaction.compaction.summary, /generated locally without an LLM/);
assert.match(compaction.compaction.summary, /Re-read identity-pinned governance documents/);
assert.match(compaction.compaction.summary, /Continue the bounded task/);

delete process.env.DUAL_GOVERNANCE_ROLE;
console.log("PASS: truncation recovery preserves governance delivery and creates deterministic checkpoints");
