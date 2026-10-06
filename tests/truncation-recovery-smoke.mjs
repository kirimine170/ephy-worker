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

function fileOps({ read = [], written = [], edited = [] } = {}) {
  return { read: new Set(read), written: new Set(written), edited: new Set(edited) };
}

function compactionEvent({
  message,
  files = fileOps(),
  branchEntries = [],
  previousSummary,
  firstKeptEntryId,
}) {
  return {
    reason: "threshold",
    customInstructions: "Preserve the current Job ID.",
    branchEntries,
    preparation: {
      fileOps: files,
      messagesToSummarize: [{ role: "user", content: [{ type: "text", text: message }] }],
      turnPrefixMessages: [],
      previousSummary,
      tokensBefore: 31_000,
      isSplitTurn: false,
      firstKeptEntryId,
    },
  };
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
const ctx = {
  cwd: repositoryRoot,
  hasUI: false,
  ui: { notify() {} },
};
const first = await pi.emit("session_before_compact", compactionEvent({
  message: "Continue the bounded task.",
  files: fileOps({ read: ["docs/read.md", "src/changed.py"], written: ["src/new.py"], edited: ["src/changed.py"] }),
  firstKeptEntryId: "kept-1",
}), ctx);
assert.equal(first.compaction.firstKeptEntryId, "kept-1");
assert.equal(first.compaction.details.checkpointVersion, 2);
assert.equal(first.compaction.details.role, "lead");
assert.deepEqual(first.compaction.details.modifiedFiles, ["src/changed.py", "src/new.py"]);
assert.deepEqual(first.compaction.details.readFiles, ["docs/read.md"]);
assert.match(first.compaction.summary, /generated locally without an LLM/);
assert.match(first.compaction.summary, /Re-read identity-pinned governance documents/);
assert.match(first.compaction.summary, /Continue the bounded task/);
assert.doesNotMatch(first.compaction.summary, /## Previous checkpoint/);

const second = await pi.emit("session_before_compact", compactionEvent({
  message: "Now inspect only the remaining file.",
  files: fileOps({ read: ["src/new.py", "tests/new-test.py"], edited: ["src/final.py"] }),
  branchEntries: [{
    type: "compaction",
    summary: first.compaction.summary,
    details: first.compaction.details,
  }],
  previousSummary: first.compaction.summary,
  firstKeptEntryId: "kept-2",
}), ctx);
assert.deepEqual(second.compaction.details.modifiedFiles, ["src/changed.py", "src/final.py", "src/new.py"]);
assert.deepEqual(second.compaction.details.readFiles, ["docs/read.md", "tests/new-test.py"]);
assert.match(second.compaction.details.retainedContext, /Continue the bounded task/);
assert.match(second.compaction.details.retainedContext, /Now inspect only the remaining file/);
assert.equal((second.compaction.summary.match(/# Ephy Pi deterministic recovery checkpoint/g) ?? []).length, 1);
assert.doesNotMatch(second.compaction.summary, /## Previous checkpoint/);
assert.equal(second.compaction.summary.length < 30_000, true);

delete process.env.DUAL_GOVERNANCE_ROLE;
console.log("PASS: truncation recovery preserves governance bytes and emits bounded non-recursive checkpoints");
