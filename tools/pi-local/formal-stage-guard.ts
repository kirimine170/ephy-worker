import { appendFileSync, lstatSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { stopManagedStage } from "./formal-stage-stop.ts";

// Load BEFORE governance-gate, which remains the final payload/ack gate.
export default function (pi: ExtensionAPI) {
  const config = JSON.parse(readFileSync(process.env.EPHY_FORMAL_STAGE_CONFIG!, "utf8"));
  const trace = process.env.EPHY_FORMAL_TRACE!;
  const root = realpathSync(config.root);
  const reads = new Set(["read", "grep", "find", "ls"]);
  const writes = new Set(["edit", "write"]);
  const pendingReads = new Map<string, string>();
  let failed = false;
  let outputTokens = 0;
  let requests = 0;
  const record = (kind: string, value: object) => {
    try { appendFileSync(trace, JSON.stringify({ at: new Date().toISOString(), kind, ...value }) + "\n"); }
    catch { stopManagedStage("Could not persist formal stage evidence"); }
  };
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
  function stopRequest(reason: string): never {
    try { reject(reason); }
    finally { stopManagedStage(reason); }
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
    if (failed) stopRequest("Formal stage is permanently latched");
    if (!event?.payload || typeof event.payload !== "object") stopRequest("Invalid provider payload");
    if (event.payload.model !== config.model_id) {
      stopRequest("Wrong model in provider payload");
    }
    // Count admitted provider calls, including the currently in-flight call.
    // Do not admit or increment the N+1 call. Pi retries are disabled by the controller.
    if (requests >= config.max_requests || outputTokens >= config.output_token_budget) {
      stopRequest("Stage request/token budget exhausted");
    }
    ++requests;
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
    record("tool_call", { tool: event.toolName, path: value, blocked: Boolean(reason),
      toolCallId: event.toolCallId });
    if (reason) return reject(reason);
    if (event.toolName === "read" && typeof event.toolCallId === "string") {
      pendingReads.set(event.toolCallId, resolve(root, value));
    }
  });
  pi.on("message_end", (event: any) => {
    const message = event.message;
    if (message?.role === "assistant") {
      if (message.model !== config.model_id || message.provider !== (config.provider_id ?? "dual-local")) {
        stopRequest("Wrong model in observed assistant response");
      }
      const output = message.usage?.output;
      if (!Number.isSafeInteger(output) || output < 0) {
        stopRequest("Missing model usage evidence");
      }
      outputTokens += output;
      record("assistant", { model: message.model, provider: message.provider, outputTokens,
        stopReason: message.stopReason });
      if (outputTokens > config.output_token_budget) {
        stopRequest("Provider exceeded output-token cap");
      }
      if (["error", "aborted", "length"].includes(message.stopReason)) {
        stopRequest("Provider returned a failed assistant response");
      }
    }
  });
  pi.on("tool_result", (event: any) => {
    if (event.toolName === "governance_ack") {
      record("governance_result", { isError: event.isError, content: event.content, details: event.details });
    }
    if (event.toolName === "read") {
      const target = pendingReads.get(event.toolCallId);
      pendingReads.delete(event.toolCallId);
      if (!target || event.isError || pathCheck(target, false)) return;
      const bytes = readFileSync(target);
      const expected = new TextDecoder("utf-8", { fatal: true }).decode(bytes).replaceAll("\r\n", "\n");
      const delivered = (event.content ?? []).filter((item: any) => item.type === "text")
        .map((item: any) => item.text).join("").replaceAll("\r\n", "\n");
      // Partial/truncated/error results cannot attest a complete artifact read.
      if (delivered === expected) record("evidence_read", {
        toolCallId: event.toolCallId, path: relative(root, target).replaceAll("\\", "/"),
        sha256: createHash("sha256").update(bytes).digest("hex"), full_content_delivered: true,
      });
    }
  });
  pi.on("agent_end", () => record("stage_end", { failed, requests, outputTokens }));
}
