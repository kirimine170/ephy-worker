import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { stopManagedStage } from "./formal-stage-stop.ts";

// Additional data scope; canonical stage/governance gates remain intact and
// governance is still loaded last. Observe emitted tool calls as well as calls
// executed by Pi, so an unavailable write tool cannot disappear from evidence.
export default function (pi: ExtensionAPI) {
  const config = JSON.parse(readFileSync(process.env.EPHY_TRIAGE_CONFIG!, "utf8"));
  let requests = 0;
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
    if (name !== "read" || !allowed.has(input?.path)) stop("Tool exceeds consumer data scope");
  }
  pi.on("tool_call", (event: any) => check(event.toolName, event.input));
  // This is before the final governance hook, so it is explicitly a preview,
  // never claimed as the transmitted payload. The controller captures the
  // actual final HTTP body and uses this tool-definition superset for preflight.
  pi.on("before_provider_request", (event: any) => {
    try {
      writeFileSync(join(config.previews, String(++requests) + ".json"),
        JSON.stringify(event.payload), { flag: "wx" });
    } catch { stop("Cannot retain provider preview"); }
  });
  pi.on("message_end", (event: any) => {
    if (event.message?.role !== "assistant") return;
    for (const item of event.message.content ?? [])
      if (item.type === "toolCall") check(item.name, item.arguments);
  });
}
