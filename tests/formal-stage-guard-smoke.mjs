import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, linkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import setup from "../tools/pi-local/formal-stage-guard.ts";

const temporary = mkdtempSync(join(tmpdir(), "ephy-formal-guard-"));
const nativeExit = process.exit;
// Callback controls run in this test process; a separate child smoke below
// verifies the actual native exit against an SDK-like swallowing handler.
process.exit = code => { assert.equal(code, 78); throw new Error("MANAGED_STAGE_EXIT"); };
try {
  const root = join(temporary, "candidate");
  mkdirSync(join(root, "docs"), { recursive: true });
  writeFileSync(join(root, "docs", "existing.md"), "existing");
  linkSync(join(root, "docs", "existing.md"), join(root, "docs", "linked.md"));
  writeFileSync(join(root, "docs", "readable.md"), "existing");
  let serial = 0;
  function fresh(role = "implementer", provider_id = "dual-local", extra = {}) {
    const config = join(temporary, `config-${++serial}.json`);
    const trace = join(temporary, `trace-${serial}.jsonl`);
    writeFileSync(config, JSON.stringify({ root, role, model_id: "test-model", provider_id,
      allowed_files: ["docs/new.md", "docs/linked.md"], max_requests: 3,
      output_token_budget: 10, max_response_tokens: 6, ...extra }));
    process.env.EPHY_FORMAL_STAGE_CONFIG = config;
    process.env.EPHY_FORMAL_TRACE = trace;
    const handlers = new Map();
    const api = { on: (name, fn) => handlers.set(name, fn), setActiveTools: () => {} };
    setup(api);
    return { call: (name, input) => handlers.get(name)(input), trace };
  }
  const tool = (name, path) => ({ toolName: name, input: { path } });
  assert.equal(fresh("planner").call("tool_call", tool("ls", ".")), undefined);
  const emptyPath = fresh("planner");
  assert.equal(emptyPath.call("tool_call", tool("ls", "")).block, true);
  assert.equal(emptyPath.call("tool_call", tool("ls", ".")).block, true);
  assert.equal(fresh().call("tool_call", tool("write", "docs/new.md")), undefined);
  assert.equal(fresh("implementer", "dual-local", { allowed_files: ["src/example.py"] })
    .call("tool_call", tool("write", "src/example.py")).block, true);
  assert.throws(() => fresh("implementer", "dual-local", {
    execution_profile: "native_windows_code_v1", verifier_sandbox: { passed: true },
  }), /pinned native isolation/);
  // Synthetic binding control only; this does not attest actual OS isolation.
  const pin = path => ({ path, sha256: createHash("sha256").update(readFileSync(path)).digest("hex") });
  const codex = join(temporary, "synthetic-codex.exe");
  const controlOut = join(temporary, "synthetic-controls.stdout");
  const controlErr = join(temporary, "synthetic-controls.stderr");
  const capabilityPath = join(temporary, "synthetic-capability.json");
  writeFileSync(codex, "synthetic executable");
  const controlNames = ["inside_write", "protected_read_denied", "protected_write_denied",
    "candidate_write_denied", "checker_write_denied", "child_read_denied", "child_write_denied",
    "child_candidate_write_denied", "child_checker_write_denied",
    "candidate_read", "checker_read", "nested_temp_read_write", "child_candidate_read", "child_checker_read",
    "child_nested_temp_read_write",
    "parent_metadata", "parent_enumeration_denied", "sibling_read_denied", "sibling_write_denied",
    "child_parent_metadata", "child_parent_enumeration_denied", "child_sibling_read_denied",
    "child_sibling_write_denied",
    "network_denied", "loopback_denied", "child_network_denied", "child_loopback_denied", "asyncio_completed"];
  const output = { nonce: "synthetic", controls: Object.fromEntries(controlNames.map(name => [name, true])) };
  writeFileSync(controlOut, JSON.stringify(output));
  writeFileSync(controlErr, "");
  function isolatedConfig() {
    writeFileSync(capabilityPath, JSON.stringify({ schema: "ephy.native-verifier-capability.v1",
      network_policy: "windows_all_network_denied", exit_code: 0, codex: pin(codex),
      nonce: "synthetic", stdout: pin(controlOut), stderr: pin(controlErr) }));
    return { execution_profile: "native_windows_code_v1", allowed_files: ["src/example.py"],
      verifier_sandbox: { profile: "native_windows_code_v1", descriptor_sha256: "a".repeat(64),
        network_policy: "windows_all_network_denied",
        module_sha256: "b".repeat(64), codex: pin(codex), capability: pin(capabilityPath) } };
  }
  assert.equal(fresh("implementer", "dual-local", isolatedConfig())
    .call("tool_call", tool("write", "src/example.py")), undefined);
  assert.equal(fresh("auditor", "dual-local", isolatedConfig())
    .call("tool_call", tool("write", "src/example.py")).block, true);
  output.controls.loopback_denied = false;
  writeFileSync(controlOut, JSON.stringify(output));
  assert.throws(() => fresh("implementer", "dual-local", isolatedConfig()), /controls did not all succeed/);
  for (const [name, path] of [["write", "../outside.md"], ["write", "docs/unapproved.md"],
    ["write", "docs/linked.md"], ["bash", "."], ["subagent", "."], ["read", "../outside.md"]]) {
    const gate = fresh();
    assert.equal(gate.call("tool_call", tool(name, path)).block, true);
    assert.equal(gate.call("tool_call", tool("read", "docs/existing.md")).block, true);
  }
  assert.equal(fresh("auditor").call("tool_call", tool("write", "docs/new.md")).block, true);
  assert.equal(fresh("planner").call("tool_call", tool("write", "docs/new.md")).block, true);
  const budget = fresh();
  const constrained = budget.call("before_provider_request", {
    payload: { model: "test-model", max_completion_tokens: 16384 } });
  assert.equal(constrained.max_tokens, 6);
  assert.equal("max_completion_tokens" in constrained, false);
  budget.call("message_end", { message: { role: "assistant", model: "test-model", provider: "dual-local",
    usage: { output: 4 }, stopReason: "stop" } });
  assert.equal(budget.call("before_provider_request", { payload: { model: "test-model" } }).max_tokens, 6);
  budget.call("message_end", { message: { role: "assistant", model: "test-model", provider: "dual-local",
    usage: { output: 4 }, stopReason: "stop" } });
  assert.equal(budget.call("before_provider_request", { payload: { model: "test-model" } }).max_tokens, 2);
  const oversized = fresh();
  oversized.call("before_provider_request", { payload: { model: "test-model" } });
  assert.throws(() => oversized.call("message_end", { message: {
    role: "assistant", model: "test-model", provider: "dual-local",
    usage: { output: 7 }, stopReason: "stop" } }), /MANAGED_STAGE_EXIT/);
  assert.throws(() => oversized.call("before_provider_request", {
    payload: { model: "test-model" } }), /MANAGED_STAGE_EXIT/);
  assert.throws(() => fresh().call("before_provider_request", { payload: { model: "wrong" } }), /MANAGED_STAGE_EXIT/);
  assert.throws(() => fresh().call("message_end", { message: { role: "assistant", model: "test-model",
    provider: "dual-local", stopReason: "stop" } }), /MANAGED_STAGE_EXIT/);
  const capped = fresh();
  for (let n = 0; n < 3; n++) capped.call("before_provider_request", { payload: { model: "test-model" } });
  assert.throws(() => capped.call("before_provider_request", { payload: { model: "test-model" } }), /MANAGED_STAGE_EXIT/);
  assert.ok(readFileSync(capped.trace, "utf8").includes("violation"));
  const strata = fresh("implementer", "strata-local");
  strata.call("before_provider_request", { payload: { model: "test-model" } });
  strata.call("message_end", { message: {
    role: "assistant", model: "test-model", provider: "strata-local",
    usage: { output: 1 }, stopReason: "stop" } });
  assert.throws(() => fresh("implementer", "strata-local").call("message_end", { message: {
    role: "assistant", model: "test-model", provider: "dual-local",
    usage: { output: 1 }, stopReason: "stop" } }), /MANAGED_STAGE_EXIT/);
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
  process.exit = nativeExit;
  rmSync(temporary, { recursive: true, force: true });
}
