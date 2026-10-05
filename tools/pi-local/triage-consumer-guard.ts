import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { stopManagedStage } from "./formal-stage-stop.ts";

// Additional data scope; canonical stage/governance gates remain intact and
// governance is still loaded last. Observe emitted tool calls as well as calls
// executed by Pi, so an unavailable write tool cannot disappear from evidence.
export default function (pi: ExtensionAPI) {
  const config = JSON.parse(readFileSync(process.env.EPHY_TRIAGE_CONFIG!, "utf8"));
  let requests = 0;
  const reads = new Map<string, string>();
  const allowed = new Set(config.arm === "treatment"
    ? ["inputs/triage-batch.json", ".agents/skills/ephy-verifier-triage/SKILL.md"]
    : ["inputs/triage-batch.json"]);
  function stop(reason: string): never {
    try {
      appendFileSync(process.env.EPHY_FORMAL_TRACE!,
        JSON.stringify({ kind: "violation", reason: "triage: " + reason }) + "\n");
    } finally { stopManagedStage("triage: " + reason); }
  }
  function check(name: string, input: any) {
    if (name === "governance_ack") return;
    if (name !== "read" || Object.keys(input ?? {}).length !== 1
      || !allowed.has(input?.path)) stop("Tool exceeds consumer data scope");
  }
  pi.on("tool_call", (event: any) => {
    check(event.toolName, event.input);
    if (event.toolName === "read") reads.set(event.toolCallId, event.input.path);
  });
  pi.on("tool_result", (event: any) => {
    if (event.toolName !== "read") return;
    const path = reads.get(event.toolCallId);
    reads.delete(event.toolCallId);
    if (!path || event.isError) stop("Missing/error read result");
    const raw = readFileSync(join(config.root, path));
    const delivered = Buffer.from((event.content ?? []).filter((v: any) => v.type === "text")
      .map((v: any) => v.text).join(""), "utf8");
    const same = raw.equals(delivered);
    appendFileSync(process.env.EPHY_FORMAL_TRACE!, JSON.stringify({
      kind: "triage_exact_read", toolCallId: event.toolCallId, path,
      raw_sha256: createHash("sha256").update(raw).digest("hex"),
      delivered_sha256: createHash("sha256").update(delivered).digest("hex"),
      raw_bytes: raw.length, delivered_bytes: delivered.length, full_content_delivered: same,
    }) + "\n");
    if (!same) stop("Read result is not byte-exact (partial/normalized)");
  });
  // This is before the final governance hook, so it is explicitly a preview,
  // never claimed as the transmitted payload. The controller captures the
  // actual final HTTP body and uses this tool-definition superset for preflight.
  pi.on("before_provider_request", (event: any) => {
    if (!Array.isArray(event.payload?.tools)) stop("Missing consumer tool definitions");
    const payload = { ...event.payload, tools: event.payload.tools
      .filter((tool: any) => ["governance_ack", "read"].includes(tool?.function?.name))
      .map((tool: any) => tool.function.name !== "read" ? tool : { ...tool, function: {
        ...tool.function,
        description: "Read one permitted text file completely and byte-exactly. Supply only path; offset, limit, partial reads and other arguments are prohibited.",
        parameters: { type: "object", required: ["path"],
          properties: { path: { type: "string", enum: [...allowed] } }, additionalProperties: false },
        strict: true,
      } }) };
    try {
      writeFileSync(join(config.previews, String(++requests) + ".json"),
        JSON.stringify(payload), { flag: "wx" });
    } catch { stop("Cannot retain provider preview"); }
    return payload;
  });
  pi.on("message_end", (event: any) => {
    if (event.message?.role !== "assistant") return;
    for (const item of event.message.content ?? [])
      if (item.type === "toolCall") check(item.name, item.arguments);
  });
}
