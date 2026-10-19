import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync, realpathSync, writeSync } from "node:fs";
import { dirname, isAbsolute, join, parse, relative, resolve, sep } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// This is a tool boundary for a private, read-only worktree, not an OS sandbox.
// The controller must disable extension/resource discovery and await the ready
// event before sending a prompt. No other process may mutate the tree during a
// run: a preflight check cannot remove filesystem TOCTOU races in builtin tools.
const CONTEXT_PATHS = [
  "docs/self-improvement-mvp.md",
  ".agents/skills/ephy-worker-self-improvement/SKILL.md",
  ".agents/skills/ephy-worker-self-improvement/references/eval-contract.md",
  ".agents/skills/ephy-worker-self-improvement/references/audit-contract.md",
  ".pi/prompts/audit-ephy-worker.md",
];
const GATE_PATH = ".pi/extensions/governance-gate.ts";
const POLICY_PATH = "docs/system-development-governance.md";
const RUNTIME_PATHS = [GATE_PATH, "tools/pi-local/strata-provider.ts",
  "tools/pi-local/formal-stage-guard.ts", "tools/pi-local/formal-stage-stop.ts", POLICY_PATH,
  ...CONTEXT_PATHS];
const POLICY_ID = "ephy.system-development-governance.v1";
const END_MARKER = "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1";
const MAX_ENTRIES = 100_000;
// Pi normalizes these spaces even in absolute paths. Reject rather than check
// one path and let the builtin silently open a different sibling afterwards.
const PI_UNICODE_SPACES = /[\u00A0\u2000-\u200A\u202F\u205F\u3000]/;

// Only these fixed identities may enter the RPC diagnostic field. Native error
// messages can contain private paths, so unknown errors use a fixed phase code.
const REJECTION_REASON_CODES = new Map<string, string>([
  ["Guard is permanently blocked", "guard_not_ready"],
  ["Guard is not ready", "guard_not_ready"],
  ["Guard is not ready or permanently blocked", "guard_not_ready"],
  ["Tool is outside the read-only allowlist", "tool_not_allowed"],
  ["Windows system path is unavailable", "windows_system_path"],
  ["Path attribute check failed", "windows_attribute_check"],
  ["Reparse points are prohibited", "path_reparse"],
  ["Symbolic links and junctions are prohibited", "path_link"],
  ["Worktree ancestor is a link", "path_link"],
  ["Only regular files and directories are permitted", "path_not_regular"],
  ["Hard-linked files are prohibited", "path_hardlink"],
  ["Canonical path escapes worktree", "path_canonical_escape"],
  ["Worktree exceeds navigation scan limit", "path_scan_limit"],
  ["Path escapes worktree", "path_escape"],
  ["Invalid tool path", "path_invalid"],
  ["Pi-normalized Unicode spaces are prohibited in paths", "path_normalized_space"],
  ["Pi-normalized Unicode spaces are prohibited in worktree paths", "path_normalized_space"],
  ["Path shorthand is prohibited", "path_shorthand"],
  ["Network and device paths are prohibited", "path_network_device"],
  ["Drive-relative paths and alternate streams are prohibited", "path_drive_or_stream"],
  ["Ambiguous Windows path", "path_ambiguous"],
  ["Windows device name", "path_device"],
  ["Unexpected session directory", "session_directory"],
  ["Required builtin read tool is unavailable or overridden", "read_tool_provenance"],
  ["Acknowledgement tool is not the pinned governance gate", "ack_tool_provenance"],
  ["Governance resources are not verified", "governance_unverified"],
  ["User shell commands are prohibited", "shell_not_allowed"],
]);
type DiagnosticPhase = "guard_rejected" | "startup_failed" | "provider_preflight" | "tool_context"
  | "tool_provenance" | "tool_input" | "tool_path" | "tool_path_rewrite";

function rejectionCode(error: unknown, fallback: DiagnosticPhase): string {
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  return REJECTION_REASON_CODES.get(message) ?? fallback;
}

// Node exposes Windows symlinks/junctions, but not every reparse-point tag.
// Inspect attributes with a fixed .NET script. Paths are environment DATA and
// are never interpolated into PowerShell source. No profile or shell is loaded.
const WINDOWS_ATTRIBUTES = String.raw`
$ErrorActionPreference = 'Stop'
try {
  $target = $env:EPHY_GUARD_ATTRIBUTE_TARGET
  $current = $target
  while ($current) {
    if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { exit 23 }
    $parent = [IO.Directory]::GetParent($current)
    if ($null -eq $parent) { break }
    $current = $parent.FullName
  }
  if ($env:EPHY_GUARD_ATTRIBUTE_TREE -eq '1') {
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($target)
    $count = 0
    while ($pending.Count -gt 0) {
      $current = $pending.Pop()
      $count++
      if ($count -gt 100000) { exit 24 }
      $attributes = [IO.File]::GetAttributes($current)
      if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { exit 23 }
      if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($current)) { $pending.Push($entry) }
      }
    }
  }
  exit 0
} catch { exit 25 }
`;

function within(root: string, target: string): boolean {
  const part = relative(root, target);
  return part !== ".." && !part.startsWith(`..${sep}`) && !isAbsolute(part);
}

type PathSnapshot = { target: string; tree: boolean; entries: Map<string, string> };
type Inspection = { signals: (AbortSignal | undefined)[]; snapshots: PathSnapshot[]; blocked: () => boolean };

function pathSnapshot(target: string, tree: boolean): Map<string, string> {
  const entries = new Map<string, string>();
  const add = (path: string, protectedEntry: boolean) => {
    const info = lstatSync(path, { bigint: true });
    if (info.isSymbolicLink()) throw new Error("Symbolic links and junctions are prohibited");
    if (!info.isDirectory() && !info.isFile()) throw new Error("Only regular files and directories are permitted");
    if (info.isFile() && info.nlink !== 1n) throw new Error("Hard-linked files are prohibited");
    // Bigint file identities detect a regular-looking replacement, including
    // Windows reparse tags that Node does not report as symbolic links. Do not
    // bind ancestor directory timestamps to unrelated siblings' activity.
    entries.set(path, [info.dev, info.ino, info.mode, info.nlink,
      protectedEntry ? info.ctimeNs : "", protectedEntry ? info.mtimeNs : "",
      realpathSync(path)].join("\0"));
    return info.isDirectory();
  };
  let current = target;
  while (true) {
    add(current, current === target);
    if (current === parse(current).root) break;
    current = dirname(current);
  }
  if (tree) {
    const pending = [target];
    let count = 0;
    while (pending.length) {
      if (++count > MAX_ENTRIES) throw new Error("Worktree exceeds navigation scan limit");
      const path = pending.pop()!;
      if (add(path, true)) for (const name of readdirSync(path)) pending.push(join(path, name));
    }
  }
  return entries;
}

function verifyInspection(inspection: Inspection): void {
  if (inspection.blocked()) throw new Error("Guard is permanently blocked");
  if (inspection.signals.some(signal => signal?.aborted)) throw new Error("Path attribute check failed");
  for (const snapshot of inspection.snapshots) {
    const current = pathSnapshot(snapshot.target, snapshot.tree);
    if (current.size !== snapshot.entries.size
      || [...snapshot.entries].some(([path, identity]) => current.get(path) !== identity)) {
      throw new Error("Path attribute check failed");
    }
  }
}

async function windowsAttributes(target: string, tree: boolean, inspection: Inspection): Promise<void> {
  inspection.snapshots.push({ target, tree, entries: pathSnapshot(target, tree) });
  verifyInspection(inspection);
  if (process.platform !== "win32") return;
  const systemRoot = process.env.SystemRoot;
  if (!systemRoot || !isAbsolute(systemRoot)) throw new Error("Windows system path is unavailable");
  await new Promise<void>((resolveCheck, rejectCheck) => {
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(join(systemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
          Buffer.from(WINDOWS_ATTRIBUTES, "utf16le").toString("base64")], {
          windowsHide: true, stdio: ["ignore", "ignore", "pipe"],
          env: { ...process.env, EPHY_GUARD_ATTRIBUTE_TARGET: target, EPHY_GUARD_ATTRIBUTE_TREE: tree ? "1" : "0" },
        });
    } catch {
      rejectCheck(new Error("Path attribute check failed"));
      return;
    }
    let failed = false;
    let stderrBytes = 0;
    const stop = () => {
      failed = true;
      // The exact child belongs to this check. The worker's existing Windows
      // Job owns descendants and confirms cleanup if the process cannot close.
      try { child.kill("SIGKILL"); } catch { /* Remain blocked until close/host cleanup. */ }
    };
    const onError = () => { failed = true; };
    const onData = (chunk: Buffer) => {
      stderrBytes += chunk.length;
      if (stderrBytes > 4096) stop();
    };
    const signals = inspection.signals.filter((signal): signal is AbortSignal => signal !== undefined);
    // A fresh main-loop timer avoids Windows spawnSync's stale isolated clock.
    const timer = setTimeout(stop, 8_000);
    child.on("error", onError);
    child.stderr?.on("error", stop);
    child.stderr?.on("data", onData);
    for (const signal of signals) signal.addEventListener("abort", stop, { once: true });
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      for (const source of signals) source.removeEventListener("abort", stop);
      child.removeListener("error", onError);
      child.stderr?.removeListener("error", stop);
      child.stderr?.removeListener("data", onData);
      if (failed || signal !== null || code !== 0 || signals.some(source => source.aborted)) {
        rejectCheck(new Error(code === 23 ? "Reparse points are prohibited" : "Path attribute check failed"));
      } else resolveCheck();
    });
    if (signals.some(signal => signal.aborted)) stop();
  });
  verifyInspection(inspection);
}

function inspectEntry(root: string, target: string): boolean {
  const info = lstatSync(target);
  if (info.isSymbolicLink()) throw new Error("Symbolic links and junctions are prohibited");
  if (!info.isDirectory() && !info.isFile()) throw new Error("Only regular files and directories are permitted");
  if (info.isFile() && info.nlink !== 1) throw new Error("Hard-linked files are prohibited");
  if (!within(root, realpathSync(target))) throw new Error("Canonical path escapes worktree");
  return info.isDirectory();
}

function inspectTree(root: string, target: string): void {
  const pending = [target];
  let count = 0;
  while (pending.length) {
    if (++count > MAX_ENTRIES) throw new Error("Worktree exceeds navigation scan limit");
    const current = pending.pop()!;
    if (inspectEntry(root, current)) {
      for (const name of readdirSync(current)) pending.push(join(current, name));
    }
  }
}

async function inspectPath(root: string, target: string, tree: boolean, inspection: Inspection): Promise<void> {
  if (!within(root, target)) throw new Error("Path escapes worktree");
  // Check each component, not just the final realpath: even links pointing back
  // into the tree are prohibited. Windows also rejects non-symlink reparse tags.
  let current = target;
  while (true) {
    inspectEntry(root, current);
    if (relative(root, current) === "") break;
    current = dirname(current);
  }
  await windowsAttributes(target, tree, inspection);
  verifyInspection(inspection);
  if (tree) inspectTree(root, target);
}

function resolveInput(root: string, value: unknown): string {
  if (typeof value !== "string" || !value || /[\x00-\x1f]/.test(value)) throw new Error("Invalid tool path");
  if (PI_UNICODE_SPACES.test(value)) throw new Error("Pi-normalized Unicode spaces are prohibited in paths");
  // Disallow home/attachment shorthands rather than let Pi reinterpret them.
  if (/^[~@]/.test(value)) throw new Error("Path shorthand is prohibited");
  if (process.platform === "win32") {
    if (/^[\\/]{2}/.test(value)) throw new Error("Network and device paths are prohibited");
    const withoutDrive = value.replace(/^[A-Za-z]:[\\/]/, "");
    if (withoutDrive.includes(":")) throw new Error("Drive-relative paths and alternate streams are prohibited");
    for (const part of withoutDrive.split(/[\\/]/)) {
      if (part !== "." && part !== ".." && /[. ]$/.test(part)) throw new Error("Ambiguous Windows path");
      if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part)) throw new Error("Windows device name");
    }
  }
  return resolve(root, value);
}

function digest(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function samePath(left: unknown, right: string): boolean {
  return typeof left === "string" && isAbsolute(left)
    && relative(right, resolve(left)) === "" && relative(right, realpathSync(left)) === "";
}

function sameContextManifest(value: any, expected: any[]): boolean {
  return Array.isArray(value) && value.length === expected.length && value.every((entry, index) =>
    entry && Object.keys(entry).length === 4 && ["path", "sha256", "bytes", "lines"]
      .every(key => entry[key] === expected[index][key]));
}

// This manifest is bound by an environment hash supplied by the host before Pi
// starts. It describes a private copy of the unchanged canonical gate/resources,
// never a path or identity supplied by the user prompt or by model output.
async function readGovernanceManifest(inspection: Inspection) {
  const manifestPath = process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST;
  const expectedHash = process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST_SHA256;
  if (!manifestPath || !isAbsolute(manifestPath) || !/^[a-f0-9]{64}$/.test(expectedHash ?? "")) {
    throw new Error("Missing host governance manifest binding");
  }
  await inspectPath(dirname(manifestPath), manifestPath, false, inspection);
  verifyInspection(inspection);
  if (lstatSync(manifestPath).size > 524_288) throw new Error("Governance manifest is oversized");
  const bytes = readFileSync(manifestPath);
  if (digest(bytes) !== expectedHash) throw new Error("Governance manifest hash mismatch");
  const manifest = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  if (manifest?.version !== 1 || manifest.role !== "planner"
    || typeof manifest.runtime_root !== "string" || !isAbsolute(manifest.runtime_root)) {
    throw new Error("Invalid governance manifest");
  }
  const runtime = realpathSync(manifest.runtime_root);
  if (!samePath(manifest.runtime_root, runtime) || !samePath(manifest.context_root, runtime)
    || !samePath(manifest.gate_path, join(runtime, GATE_PATH))
    || !samePath(manifest.policy_path, join(runtime, POLICY_PATH))) {
    throw new Error("Governance resource location mismatch");
  }
  await inspectPath(runtime, runtime, true, inspection);
  verifyInspection(inspection);
  if (process.env.DUAL_GOVERNANCE_ROLE !== "planner"
    || !samePath(process.env.DUAL_GOVERNANCE_CONTEXT_ROOT, runtime)
    || !samePath(process.env.DUAL_GOVERNANCE_POLICY, manifest.policy_path)
    || process.env.DUAL_JOB_RUNNER || process.env.DUAL_RUNNER_SMOKE_TEST || process.env.DUAL_AUDIT_BUNDLE_ROOT) {
    throw new Error("Governance environment mismatch");
  }
  if (!Array.isArray(manifest.files) || manifest.files.length !== RUNTIME_PATHS.length
    || new Set(manifest.files.map((entry: any) => entry?.path)).size !== RUNTIME_PATHS.length) {
    throw new Error("Invalid governance resource list");
  }
  const contents = new Map<string, { bytes: Buffer; text: string; sha256: string }>();
  for (const name of RUNTIME_PATHS) {
    const entry = manifest.files.find((value: any) => value?.path === name);
    if (!entry || !Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > 2_097_152
      || !/^[a-f0-9]{64}$/.test(entry.sha256)) throw new Error("Invalid governance resource identity");
    const path = join(runtime, name);
    if (lstatSync(path).size !== entry.bytes) throw new Error("Governance resource size mismatch");
    const raw = readFileSync(path);
    const sha256 = digest(raw);
    if (sha256 !== entry.sha256) throw new Error("Governance resource hash mismatch");
    contents.set(name, { bytes: raw, text: new TextDecoder("utf-8", { fatal: true }).decode(raw), sha256 });
  }
  if (contents.get(GATE_PATH)!.sha256 !== manifest.gate_sha256
    || contents.get(POLICY_PATH)!.sha256 !== manifest.policy_sha256) {
    throw new Error("Governance source binding mismatch");
  }
  const contextManifest = CONTEXT_PATHS.map(path => {
    const content = contents.get(path)!;
    return { path, sha256: content.sha256, bytes: content.bytes.length, lines: content.text.split(/\r?\n/).length };
  });
  if (!sameContextManifest(manifest.context_manifest, contextManifest)) {
    throw new Error("Governance context manifest mismatch");
  }
  const policy = contents.get(POLICY_PATH)!.text;
  if (!policy.includes(POLICY_ID) || !policy.trimEnd().endsWith(END_MARKER)
    || policy.includes("</development_governance>")) throw new Error("Invalid governance policy");
  return { ...manifest, runtime_root: runtime, policy, context_manifest: contextManifest };
}

export default function (pi: ExtensionAPI) {
  // rg configuration can add flags including external preprocessors. GUI runs
  // use only the builtin tool's fixed arguments, even with inherited settings.
  delete process.env.RIPGREP_CONFIG_PATH;
  const cwd = resolve(process.cwd());
  const root = realpathSync(cwd);
  let ready = false;
  let failed = false;
  let governance: Awaited<ReturnType<typeof readGovernanceManifest>> | undefined;
  let governanceNonce: string | undefined;
  const shutdown = new AbortController();

  function verifyTools() {
    if (!governance) throw new Error("Governance resources are not verified");
    const allTools = pi.getAllTools();
    const reads = allTools.filter(tool => tool.name === "read");
    const acknowledgements = allTools.filter(tool => tool.name === "governance_ack");
    if (reads.length !== 1 || reads[0].sourceInfo?.source !== "builtin") {
      throw new Error("Required builtin read tool is unavailable or overridden");
    }
    const source = acknowledgements[0]?.sourceInfo;
    if (acknowledgements.length !== 1 || typeof source?.source !== "string"
      || ["builtin", "sdk"].includes(source.source) || !samePath(source.path, governance.gate_path)
      || digest(readFileSync(governance.gate_path)) !== governance.gate_sha256) {
      throw new Error("Acknowledgement tool is not the pinned governance gate");
    }
  }

  function reject(reason: string, code = rejectionCode(reason, "guard_rejected")) {
    if (ready && !failed) {
      writeSync(1, JSON.stringify({ type: "ephy_navigation_guard_violation", version: 1, reason_code: code }) + "\n");
    }
    failed = true;
    pi.setActiveTools([]);
    return { block: true, terminate: true, reason: `Desktop navigation guard: ${reason}` };
  }

  pi.on("session_start", async (_event, ctx) => {
    const inspection: Inspection = { signals: [shutdown.signal, ctx.signal], snapshots: [], blocked: () => failed };
    try {
      if (failed) throw new Error("Guard is permanently blocked");
      if (PI_UNICODE_SPACES.test(cwd) || PI_UNICODE_SPACES.test(root)) {
        throw new Error("Pi-normalized Unicode spaces are prohibited in worktree paths");
      }
      if (relative(root, realpathSync(ctx.cwd)) !== "") throw new Error("Unexpected session directory");
      // Inspect lexical ancestors as well as canonical root before attesting.
      let ancestor = cwd;
      while (true) {
        if (lstatSync(ancestor).isSymbolicLink()) throw new Error("Worktree ancestor is a link");
        if (ancestor === parse(ancestor).root) break;
        ancestor = dirname(ancestor);
      }
      await windowsAttributes(cwd, false, inspection);
      await inspectPath(root, root, true, inspection);
      governance = await readGovernanceManifest(inspection);
      verifyInspection(inspection);
      if (failed) throw new Error("Guard is permanently blocked");
      if (relative(root, realpathSync(ctx.cwd)) !== "") throw new Error("Unexpected session directory");
      verifyTools();
      // Do not activate tools here. The unchanged governance gate, loaded last,
      // owns the one-shot acknowledgement and context-delivery state machine.
      ready = true;
      // Pi redirects extension console/process.stdout output to stderr in RPC
      // mode. Write this fixed protocol marker to the RPC descriptor directly.
      writeSync(1, JSON.stringify({ type: "ephy_navigation_guard_ready", version: 1 }) + "\n");
    } catch (error) {
      const code = rejectionCode(error, "startup_failed");
      reject("Worktree initialization failed", code);
      writeSync(1, JSON.stringify({ type: "ephy_navigation_guard_failed", version: 1, reason_code: code }) + "\n");
    }
  });

  pi.on("session_shutdown", () => {
    failed = true;
    ready = false;
    pi.setActiveTools([]);
    shutdown.abort();
  });

  pi.on("tool_call", async (event, ctx) => {
    if (!ready || failed) return reject("Guard is not ready or permanently blocked");
    if (!["read", "governance_ack"].includes(event.toolName)) return reject("Tool is outside the read-only allowlist");
    let phase: DiagnosticPhase = "tool_context";
    try {
      if (relative(root, realpathSync(ctx.cwd)) !== "") throw new Error("Unexpected session directory");
      phase = "tool_provenance";
      verifyTools();
      // The canonical gate alone validates its exact five-field envelope. Never
      // add a path, rewrite an argument or substitute a synthetic ack result.
      if (event.toolName === "governance_ack") return;
      phase = "tool_input";
      const input = event.input as Record<string, unknown>;
      const target = resolveInput(root, input.path ?? ".");
      phase = "tool_path";
      const inspection: Inspection = { signals: [shutdown.signal, ctx.signal], snapshots: [], blocked: () => failed };
      await inspectPath(root, target, false, inspection);
      verifyInspection(inspection);
      if (failed) throw new Error("Guard is permanently blocked");
      phase = "tool_context";
      if (relative(root, realpathSync(ctx.cwd)) !== "") throw new Error("Unexpected session directory");
      phase = "tool_provenance";
      verifyTools();
      // Prevent differences between our path parsing and Pi's ~/attachment rules.
      phase = "tool_path_rewrite";
      input.path = target;
    } catch (error) {
      return reject(error instanceof Error ? error.message : "Path validation failed", rejectionCode(error, phase));
    }
  });

  pi.on("before_provider_request", (event: any) => {
    try {
      if (!ready || failed || !governance) throw new Error("Guard is not ready");
      verifyTools();
      if (!Array.isArray(event.payload?.messages) || !Array.isArray(event.payload?.tools)) {
        throw new Error("Missing provider payload");
      }
      // Pi 0.86.1's OpenAI renderer can use developer instead of system. Both
      // are privileged roles; user/tool text must never supply this envelope.
      const systemMessages = event.payload.messages.filter((message: any) =>
        ["system", "developer"].includes(message?.role));
      if (systemMessages.length !== 1 || typeof systemMessages[0].content !== "string") {
        throw new Error("Missing governance system message");
      }
      const system = systemMessages[0].content;
      const sections = [...system.matchAll(/<development_governance>\s*([\s\S]*?)<\/development_governance>/g)];
      let section: string;
      let rawVerifiedSection = false;
      if (sections.length === 1 && system.split("<development_governance>").length === 2
        && system.split("</development_governance>").length === 2) {
        section = sections[0][1];
      } else if (sections.length === 0 && governanceNonce && !system.includes("<development_governance")
        && !system.includes("</development_governance")) {
        // After ACK the unchanged gate's context hook replaces its section
        // with raw text. Pi 0.86.1 then omits the XML wrapper. Accept that exact
        // observed form only after the initial envelope was bound, with the
        // same nonce and verified state checked below. This is not an ACK
        // attestation: the host separately checks the final gate tool result.
        const headers = [...system.matchAll(/^Policy-ID: [^\r\n]+$/gm)];
        if (headers.length !== 1) throw new Error("Missing or repeated raw governance identity");
        section = system.slice(headers[0].index);
        rawVerifiedSection = true;
      } else {
        throw new Error("Missing or repeated governance system section");
      }
      const value = (label: string) => {
        const matches = [...section.matchAll(new RegExp(`^${label}: (.+)$`, "gm"))];
        if (matches.length !== 1) throw new Error("Invalid governance system identity");
        return matches[0][1].replace(/\r$/, "");
      };
      const nonce = value("Acknowledgement-Nonce");
      const state = value("Acknowledgement-State");
      if (value("Policy-ID") !== POLICY_ID || value("Policy-SHA256") !== governance.policy_sha256
        || value("Policy-End-Marker") !== END_MARKER || value("Role") !== "planner"
        || value("Governance-Context-Root") !== governance.runtime_root.replaceAll("\\", "/")
        || !sameContextManifest(JSON.parse(value("Required-Context-After-Ack")), governance.context_manifest)
        || value("Managed-Runtime-Artifacts") !== "[]" || value("Audit-Bundle-Root") !== "<not-applicable>"
        || !["required", "verified"].includes(state) || (rawVerifiedSection && state !== "verified")
        || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(nonce)
        || (governanceNonce && governanceNonce !== nonce) || !section.includes(governance.policy)) {
        throw new Error("Governance system identity mismatch");
      }
      const definitions = event.payload.tools.map((tool: any) => tool?.function?.name);
      if ((state === "required" && definitions.filter((name: unknown) => name === "governance_ack").length !== 1)
        || (state === "verified" && (definitions.includes("governance_ack")
          || definitions.filter((name: unknown) => name === "read").length !== 1))) {
        throw new Error("Governance phase tool definition mismatch");
      }
      if (!governanceNonce) {
        if (state !== "required") throw new Error("Missing initial governance bootstrap");
        governanceNonce = nonce;
        writeSync(1, JSON.stringify({ type: "ephy_desktop_governance_envelope", version: 1,
          policy_sha256: governance.policy_sha256, role: "planner", nonce,
          context_manifest: governance.context_manifest }) + "\n");
      }
      // Planner's canonical ceiling is wider than this GUI contract. Narrow
      // the outgoing definitions; the final gate still forces ack-only before
      // acknowledgement and controls activation after exact context delivery.
      return { ...event.payload, tools: event.payload.tools.filter((tool: any) =>
        ["read", "governance_ack"].includes(tool?.function?.name)) };
    } catch {
      reject("Governance provider preflight failed", "provider_preflight");
      // Pi catches extension exceptions. Exit only this owned Pi process so an
      // invalid/missing gate section cannot fall through to a provider call.
      process.exit(78);
    }
  });

  pi.on("user_bash", () => {
    reject("User shell commands are prohibited");
    return { result: { output: "Desktop navigation guard: shell commands are prohibited",
      exitCode: 1, cancelled: false, truncated: false } };
  });
}
