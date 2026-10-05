import { appendFileSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { createHash } from "node:crypto";
import { join, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { stopManagedStage } from "./formal-stage-stop.ts";

// Observe before governance; the controller separately compares the final wire
// payload after that pinned hook. Never label this preview as the wire request.
export default function (pi: ExtensionAPI) {
  const rawConfig = readFileSync(process.env.EPHY_TRIAGE_GENERATION_CONFIG!);
  const cfg = JSON.parse(rawConfig.toString("utf8"));
  const hash = (b: Buffer) => createHash("sha256").update(b).digest("hex");
  function budget(bytes: number) {
    const paths = new Map<string, number>();
    function visit(root: string) {
      for (const f of readdirSync(root, {withFileTypes: true})) {
        const path = join(root, f.name);
        if (f.isDirectory()) visit(path);
        else paths.set(resolve(path), statSync(path).size);
      }
    }
    visit(cfg.capture_root);
    if (cfg.log_root) visit(cfg.log_root);
    try { paths.set(resolve(process.env.EPHY_FORMAL_TRACE!), statSync(process.env.EPHY_FORMAL_TRACE!).size); }
    catch (e: any) { if (e.code !== "ENOENT") throw e; }
    const total = [...paths.values()].reduce((a, b) => a + b, 0);
    if (total + bytes > cfg.max_log_bytes) stopManagedStage("Generation capture cumulative log cap");
  }
  const record = (kind: string, value: object) => {
    const line = JSON.stringify({ kind, session_id: cfg.session_id, role: cfg.role, ...value }) + "\n";
    budget(Buffer.byteLength(line, "utf8"));
    appendFileSync(process.env.EPHY_FORMAL_TRACE!, line);
  };
  const pending = new Map<string, {name: string, input: any}>();
  const seen = new Set<string>();
  let requests = 0;
  function stop(reason: string): never {
    try { record("violation", { reason: "generation isolation: " + reason }); }
    finally { stopManagedStage(reason); }
  }
  function check(name: string, input: any) {
    if (name === "governance_ack") return;
    if (name === "read" && Object.keys(input ?? {}).length === 1
      && cfg.allowed_reads.includes(input?.path)) return;
    if (name === "write" && cfg.role === "implementer"
      && input?.path === ".agents/skills/ephy-verifier-triage/SKILL.md"
      && typeof input.content === "string" && Object.keys(input).length === 2) return;
    stop("Tool exceeds closed generation scope");
  }
  record("generation_start", { config_sha256: hash(rawConfig), pid: process.pid });
  pi.on("tool_call", (e: any) => {
    check(e.toolName, e.input);
    if (typeof e.toolCallId !== "string" || seen.has(e.toolCallId)) stop("Duplicate tool identity");
    seen.add(e.toolCallId);
    pending.set(e.toolCallId, {name: e.toolName, input: e.input});
    record("generation_tool_call", { id: e.toolCallId, name: e.toolName, input: e.input });
  });
  pi.on("tool_result", (e: any) => {
    const call = pending.get(e.toolCallId);
    pending.delete(e.toolCallId);
    if (!call || call.name !== e.toolName || e.isError
      || !(e.content ?? []).every((v: any) => v.type === "text" && typeof v.text === "string"))
      stop("Missing/error/nontext tool result");
    const text = e.content.map((v: any) => v.text).join("");
    const body = Buffer.from(text, "utf8");
    if (call.name === "read" && !readFileSync(join(cfg.root, call.input.path)).equals(body))
      stop("Read delivery is partial or normalized");
    record("generation_tool_result", { id: e.toolCallId, name: call.name, input: call.input,
      text, bytes: body.length, sha256: hash(body) });
  });
  pi.on("before_provider_request", (e: any) => {
    if (pending.size) stop("Unfinished tool result");
    const raw = Buffer.from(JSON.stringify(e.payload), "utf8");
    const number = ++requests;
    budget(raw.length);
    writeFileSync(join(cfg.previews, number + ".json"), raw, {flag: "wx"});
    record("generation_preview", { number, sha256: hash(raw) });
  });
  pi.on("message_end", (e: any) => {
    if (e.message?.role !== "assistant") return;
    for (const c of e.message.content ?? [])
      if (c.type === "toolCall") check(c.name, c.arguments);
  });
}
