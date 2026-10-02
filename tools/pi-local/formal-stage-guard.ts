import { appendFileSync, lstatSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Load BEFORE governance-gate, which remains the final payload/ack gate.
export default function (pi: ExtensionAPI) {
  const config = JSON.parse(readFileSync(process.env.EPHY_FORMAL_STAGE_CONFIG!, "utf8"));
  const trace = process.env.EPHY_FORMAL_TRACE!;
  const root = realpathSync(config.root);
  const reads = new Set(["read", "grep", "find", "ls"]);
  const writes = new Set(["edit", "write"]);
  let failed = false;
  let outputTokens = 0;
  let requests = 0;
  const record = (kind: string, value: object) => appendFileSync(trace,
    JSON.stringify({ at: new Date().toISOString(), kind, ...value }) + "\n");
  function within(base: string, target: string) {
    const rel = relative(base, target);
    return rel !== ".." && !rel.startsWith("../") && !rel.startsWith("..\\") && !isAbsolute(rel);
  }
  function reject(reason: string) {
    failed = true;
    pi.setActiveTools([]);
    record("violation", { reason });
    return { block: true, terminate: true, reason };
  }
  function pathCheck(value: string, writing: boolean) {
    const target = resolve(root, value);
    if (!within(root, target)) return "Path escapes formal stage root";
    let current = target;
    while (current !== dirname(current)) {
      try {
        const info = lstatSync(current);
        if (info.isSymbolicLink() || (info.isFile() && info.nlink !== 1)) return "Link prohibited";
        if (!within(root, realpathSync(current))) return "Canonical path escapes stage root";
        break;
      } catch (error: any) {
        if (error.code !== "ENOENT") return "Path is unreadable";
        if (!writing) return "Read path does not exist";
        current = dirname(current);
      }
    }
    if (writing) {
      const rel = relative(root, target).replaceAll("\\", "/");
      // Exact case-sensitive scope even on case-insensitive filesystems.
      if (!config.allowed_files.includes(rel)) return "Write outside frozen file scope";
      if (!rel.endsWith(".md")) return "Executable candidates require an approved verifier sandbox";
    }
    return undefined;
  }
  pi.on("before_provider_request", (event: any) => {
    if (failed) throw new Error("FORMAL_STAGE_LATCHED");
    if (event.payload.model !== config.model_id) {
      reject("Wrong model in provider payload");
      throw new Error("FORMAL_MODEL_MISMATCH");
    }
    if (++requests > config.max_requests || outputTokens >= config.output_token_budget) {
      reject("Stage request/token budget exhausted");
      throw new Error("FORMAL_BUDGET_EXHAUSTED");
    }
    record("provider_request", { model: event.payload.model, requests, outputTokens });
    return { ...event.payload,
      max_tokens: Math.min(config.max_response_tokens, config.output_token_budget - outputTokens) };
  });
  pi.on("tool_call", (event: any) => {
    if (failed) return reject("Formal stage is permanently latched");
    if (event.toolName === "governance_ack") {
      record("tool_call", { tool: event.toolName });
      return;
    }
    const writing = writes.has(event.toolName);
    if (!reads.has(event.toolName) && !(writing && config.role === "implementer")) {
      return reject("Tool exceeds formal stage ceiling");
    }
    const value = event.input?.path ?? ".";
    if (typeof value !== "string" || !value || value.includes("\0")) return reject("Invalid tool path");
    const reason = pathCheck(value, writing);
    record("tool_call", { tool: event.toolName, path: value, blocked: Boolean(reason) });
    if (reason) return reject(reason);
  });
  pi.on("message_end", (event: any) => {
    const message = event.message;
    if (message?.role === "assistant") {
      if (message.model !== config.model_id || message.provider !== "dual-local") {
        reject("Wrong model in observed assistant response");
        throw new Error("FORMAL_MODEL_MISMATCH");
      }
      const output = message.usage?.output;
      if (!Number.isSafeInteger(output) || output < 0) {
        reject("Missing model usage evidence");
        throw new Error("FORMAL_USAGE_MISSING");
      }
      outputTokens += output;
      record("assistant", { model: message.model, provider: message.provider, outputTokens,
        stopReason: message.stopReason });
      if (outputTokens > config.output_token_budget) {
        reject("Provider exceeded output-token cap");
        throw new Error("FORMAL_BUDGET_EXHAUSTED");
      }
    }
  });
  pi.on("tool_result", (event: any) => {
    if (event.toolName === "governance_ack") {
      record("governance_result", { isError: event.isError, content: event.content, details: event.details });
    }
  });
  pi.on("agent_end", () => record("stage_end", { failed, requests, outputTokens }));
}
