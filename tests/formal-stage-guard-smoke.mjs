import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, linkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import setup from "../tools/pi-local/formal-stage-guard.ts";

const temporary = mkdtempSync(join(tmpdir(), "ephy-formal-guard-"));
try {
  const root = join(temporary, "candidate");
  mkdirSync(join(root, "docs"), { recursive: true });
  writeFileSync(join(root, "docs", "existing.md"), "existing");
  linkSync(join(root, "docs", "existing.md"), join(root, "docs", "linked.md"));
  writeFileSync(join(root, "docs", "readable.md"), "existing");
  let serial = 0;
  function fresh(role = "implementer") {
    const config = join(temporary, `config-${++serial}.json`);
    const trace = join(temporary, `trace-${serial}.jsonl`);
    writeFileSync(config, JSON.stringify({ root, role, model_id: "test-model",
      allowed_files: ["docs/new.md", "docs/linked.md"], max_requests: 3,
      output_token_budget: 10, max_response_tokens: 6 }));
    process.env.EPHY_FORMAL_STAGE_CONFIG = config;
    process.env.EPHY_FORMAL_TRACE = trace;
    const handlers = new Map();
    const api = { on: (name, fn) => handlers.set(name, fn), setActiveTools: () => {} };
    setup(api);
    return { call: (name, input) => handlers.get(name)(input), trace };
  }
  const tool = (name, path) => ({ toolName: name, input: { path } });
  assert.equal(fresh().call("tool_call", tool("write", "docs/new.md")), undefined);
  for (const [name, path] of [["write", "../outside.md"], ["write", "docs/unapproved.md"],
    ["write", "docs/linked.md"], ["bash", "."], ["subagent", "."], ["read", "../outside.md"]]) {
    const gate = fresh();
    assert.equal(gate.call("tool_call", tool(name, path)).block, true);
    assert.equal(gate.call("tool_call", tool("read", "docs/existing.md")).block, true);
  }
  assert.equal(fresh("auditor").call("tool_call", tool("write", "docs/new.md")).block, true);
  assert.equal(fresh("planner").call("tool_call", tool("write", "docs/new.md")).block, true);
  const budget = fresh();
  assert.equal(budget.call("before_provider_request", { payload: { model: "test-model" } }).max_tokens, 6);
  budget.call("message_end", { message: { role: "assistant", model: "test-model", provider: "dual-local",
    usage: { output: 8 }, stopReason: "stop" } });
  assert.equal(budget.call("before_provider_request", { payload: { model: "test-model" } }).max_tokens, 2);
  assert.throws(() => fresh().call("before_provider_request", { payload: { model: "wrong" } }), /MODEL_MISMATCH/);
  assert.throws(() => fresh().call("message_end", { message: { role: "assistant", model: "test-model",
    provider: "dual-local", stopReason: "stop" } }), /USAGE_MISSING/);
  const capped = fresh();
  for (let n = 0; n < 3; n++) capped.call("before_provider_request", { payload: { model: "test-model" } });
  assert.throws(() => capped.call("before_provider_request", { payload: { model: "test-model" } }), /BUDGET_EXHAUSTED/);
  assert.ok(readFileSync(capped.trace, "utf8").includes("violation"));
  for (const [text, isError, expected] of [["existing", false, true], ["partial", false, false],
    ["existing", true, false], ["existing\n[truncated]", false, false]]) {
    const gate = fresh("auditor");
    gate.call("tool_call", { ...tool("read", "docs/readable.md"), toolCallId: "read-1" });
    gate.call("tool_result", { toolName: "read", toolCallId: "read-1", isError,
      content: [{ type: "text", text }] });
    assert.equal(readFileSync(gate.trace, "utf8").includes('"kind":"evidence_read"'), expected);
  }
  console.log("PASS: formal stage path, hard-link, role, model, output-token, request and latch controls");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
