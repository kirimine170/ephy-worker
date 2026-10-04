import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, appendFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import setup from "../tools/pi-local/formal-stage-guard.ts";

if (process.argv[2] === "--child") {
  const [root, kind] = process.argv.slice(3);
  process.env.EPHY_FORMAL_STAGE_CONFIG = join(root, "config.json");
  process.env.EPHY_FORMAL_TRACE = join(root, kind === "trace_io" ? "missing/trace.jsonl" : "trace.jsonl");
  const handlers = new Map();
  setup({ on: (name, fn) => handlers.set(name, fn), setActiveTools() {} });
  for (let n = 0; n < 4; ++n) {
    // Simulate Pi's catch-and-continue callback handling. Native exit must make
    // the outbound marker after a denied hook unreachable.
    try { handlers.get("before_provider_request")({ payload: { model: kind === "model" ? "wrong" : "fixture" } }); }
    catch { /* deliberately swallow, matching the observed SDK behavior */ }
    appendFileSync(join(root, "outbound"), "POST\n");
    if (kind === "tokens") handlers.get("message_end")({ message: {
      role: "assistant", model: "fixture", provider: "dual-local", usage: { output: 8 }, stopReason: "stop" } });
    if (kind === "response") handlers.get("message_end")({ message: {
      role: "assistant", model: "fixture", provider: "dual-local", usage: { output: 3 }, stopReason: "stop" } });
    if (kind === "latched") handlers.get("tool_call")({ toolName: "write", input: { path: "../outside.md" } });
  }
  assert.fail("Denied provider hook returned to the simulated SDK");
} else {
  const allocation = mkdtempSync(join(tmpdir(), "ephy-stage-boundary-"));
  try {
    for (const [kind, expected] of [["requests", 2], ["tokens", 1], ["latched", 1], ["model", 0], ["trace_io", 0], ["response", 1]]) {
      const root = join(allocation, kind);
      mkdirSync(root);
      writeFileSync(join(root, "outbound"), "");
      writeFileSync(join(root, "config.json"), JSON.stringify({ root, role: "implementer", model_id: "fixture",
        allowed_files: ["docs/allowed.md"], max_requests: 2, output_token_budget: 8,
        max_response_tokens: kind === "response" ? 2 : 8 }));
      const child = spawnSync(process.execPath, ["--experimental-strip-types", fileURLToPath(import.meta.url),
        "--child", root, kind], { timeout: 10000, encoding: "utf8" });
      assert.equal(child.status, 78, child.stderr);
      assert.equal(readFileSync(join(root, "outbound"), "utf8").split("POST\n").length - 1, expected);
      if (kind === "trace_io") continue;
      const events = readFileSync(join(root, "trace.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
      assert.equal(events.filter(e => e.kind === "hard_stop").length, 1);
      assert.ok(events.some(e => e.kind === "violation"));
      assert.equal(events.filter(e => e.kind === "provider_request").length, expected);
    }
    console.log("PASS: native managed-process exit prevents outbound calls despite swallowed hook errors (6 controls)");
  } finally {
    rmSync(allocation, { recursive: true, force: true });
  }
}
