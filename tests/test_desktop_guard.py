"""Exercise the real TypeScript guard without a provider or Pi model call."""

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "src/ephy_worker/desktop/navigation_guard.ts"

GOVERNANCE_FIXTURE = r"""
const repository = resolve(process.argv[3]);
const runtime = join(fixture, 'governance');
const contextPaths = ['docs/self-improvement-mvp.md',
  '.agents/skills/ephy-worker-self-improvement/SKILL.md',
  '.agents/skills/ephy-worker-self-improvement/references/eval-contract.md',
  '.agents/skills/ephy-worker-self-improvement/references/audit-contract.md',
  '.pi/prompts/audit-ephy-worker.md'];
const runtimePaths = ['.pi/extensions/governance-gate.ts', 'tools/pi-local/strata-provider.ts',
  'tools/pi-local/formal-stage-guard.ts', 'tools/pi-local/formal-stage-stop.ts',
  'docs/system-development-governance.md', ...contextPaths];
const hash = value => createHash('sha256').update(value).digest('hex');
const files = runtimePaths.map(path => {
  const target = join(runtime, path);
  fs.mkdirSync(dirname(target), { recursive: true });
  fs.copyFileSync(join(repository, path), target);
  const raw = fs.readFileSync(target);
  return { path, sha256: hash(raw), bytes: raw.length };
});
const contextManifest = contextPaths.map(path => ({ ...files.find(file => file.path === path),
  lines: fs.readFileSync(join(runtime, path), 'utf8').split(/\r?\n/).length }));
const gatePath = join(runtime, '.pi/extensions/governance-gate.ts');
const policyPath = join(runtime, 'docs/system-development-governance.md');
const manifest = { version: 1, role: 'planner', runtime_root: runtime, gate_path: gatePath,
  gate_sha256: hash(fs.readFileSync(gatePath)), policy_path: policyPath,
  policy_sha256: hash(fs.readFileSync(policyPath)), context_root: runtime,
  files, context_manifest: contextManifest };
const manifestPath = join(fixture, 'governance-manifest.json');
const saveManifest = () => {
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST_SHA256 = hash(fs.readFileSync(manifestPath));
};
saveManifest();
process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST = manifestPath;
process.env.DUAL_GOVERNANCE_ROLE = 'planner';
process.env.DUAL_GOVERNANCE_POLICY = policyPath;
process.env.DUAL_GOVERNANCE_CONTEXT_ROOT = runtime;
delete process.env.DUAL_JOB_RUNNER;
delete process.env.DUAL_RUNNER_SMOKE_TEST;
delete process.env.DUAL_AUDIT_BUNDLE_ROOT;
const builtinTools = ['read', 'grep', 'find', 'ls', 'bash', 'edit', 'write', 'subagent']
  .map(name => ({ name, sourceInfo: { source: 'builtin' } }));
const ackTool = { name: 'governance_ack', sourceInfo: { source: 'cli', path: gatePath } };
"""


def test_navigation_guard_controls(tmp_path):
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { mkdirSync, writeFileSync, symlinkSync, unlinkSync, linkSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { dirname, join, resolve } from 'node:path';
const { default: setup } = await import(process.argv[1]);
const fixture = resolve(process.argv[2]);
// GOVERNANCE_FIXTURE
const root = join(fixture, 'worktree');
const outside = join(fixture, 'outside');
mkdirSync(join(root, 'docs'), { recursive: true });
mkdirSync(outside);
writeFileSync(join(root, 'docs', 'readme.txt'), 'safe test text');
writeFileSync(join(outside, 'private.txt'), 'synthetic outside marker');
writeFileSync(join(root, "quote ' unicode 日本.txt"), 'safe');
process.chdir(root);
const nativeWrite = fs.writeSync;
let lines = [];
fs.writeSync = function (...args) {
  if (args[0] !== 1) return Reflect.apply(nativeWrite, this, args);
  const value = args[1];
  lines.push(String(value));
  return Buffer.byteLength(value);
};
syncBuiltinESMExports();
async function fresh({ start = true, override = false, cwd = root, toolList = null } = {}) {
  const handlers = new Map();
  let tools = ['governance_ack'];
  setup({
    on: (name, handler) => handlers.set(name, handler),
    setActiveTools: names => { tools = names; },
    getAllTools: () => toolList ?? [...builtinTools.map(tool => ({ ...tool,
      sourceInfo: { source: override && tool.name === 'read' ? 'extension' : 'builtin' }
    })), ackTool]
  });
  const call = (name, event, activeCwd = cwd) => handlers.get(name)(event, { cwd: activeCwd });
  const before = lines.length;
  if (start) await call('session_start', { reason: 'startup' });
  const ready = lines.slice(before).some(line => JSON.parse(line).type === 'ephy_navigation_guard_ready');
  return { call, ready, active: () => tools };
}
const event = (toolName, path) => ({ toolName, input: { path } });
try {
  process.env.RIPGREP_CONFIG_PATH = join(outside, 'unsafe-config');
  const allowed = (await fresh());
  assert.equal(process.env.RIPGREP_CONFIG_PATH, undefined);
  assert.equal(allowed.ready, true);
  assert.deepEqual(allowed.active(), ['governance_ack']);
  for (const [name, path] of [['read', 'docs/readme.txt'], ['read', join(root, 'docs', 'readme.txt')],
    ['read', "quote ' unicode 日本.txt"]]) {
    const value = event(name, path);
    assert.equal((await allowed.call('tool_call', value)), undefined);
    assert.equal(value.input.path, resolve(root, path ?? '.'));
  }
  // Model a tool dispatcher: a blocked call must not invoke the builtin read.
  const builtinReads = [];
  async function dispatchRead(gate, value) {
    const decision = await gate.call('tool_call', value);
    if (decision?.block) return decision;
    builtinReads.push(value.input.path);
    return fs.readFileSync(value.input.path, 'utf8');
  }
  assert.equal(await dispatchRead(allowed, event('read', 'docs/readme.txt')), 'safe test text');
  assert.deepEqual(builtinReads, [join(root, 'docs', 'readme.txt')]);
  for (const path of ['.', 'docs', root, join(root, 'docs')]) {
    const gate = await fresh();
    assert.equal(gate.ready, true, 'directory startup/ancestor checks remain supported');
    const before = lines.length;
    const value = event('read', path);
    const decision = await dispatchRead(gate, value);
    assert.equal(decision.block, true);
    assert.equal(decision.terminate, true);
    assert.equal(value.input.path, path, 'rejected input is not rewritten');
    assert.deepEqual(gate.active(), []);
    assert.deepEqual(lines.slice(before).map(line => JSON.parse(line)),
      [{ type: 'ephy_navigation_guard_violation', version: 1, reason_code: 'path_directory' }]);
    assert.equal((await dispatchRead(gate, event('read', 'docs/readme.txt'))).block, true);
    assert.deepEqual(builtinReads, [join(root, 'docs', 'readme.txt')], 'directory and latched calls never execute');
  }
  const ack = { toolName: 'governance_ack', input: { nonce: 'gate-validates-this', role: 'planner' } };
  const ackBefore = JSON.stringify(ack);
  assert.equal((await allowed.call('tool_call', ack)), undefined);
  assert.equal(JSON.stringify(ack), ackBefore, 'ack arguments remain unmodified');
  const limited = { toolName: 'read', input: { path: 'docs/readme.txt', offset: 1, limit: 1 } };
  assert.equal((await allowed.call('tool_call', limited)), undefined);
  assert.deepEqual(limited.input, { path: resolve(root, 'docs/readme.txt'), offset: 1, limit: 1 });
  for (const [name, path, reasonCode] of [
    ['grep', '.', 'tool_not_allowed'], ['find', '.', 'tool_not_allowed'], ['ls', '.', 'tool_not_allowed'],
    ['bash', '.', 'tool_not_allowed'], ['powershell', '.', 'tool_not_allowed'],
    ['write', 'docs/readme.txt', 'tool_not_allowed'], ['edit', 'docs/readme.txt', 'tool_not_allowed'],
    ['subagent', '.', 'tool_not_allowed'], ['karte_worklog_append', '.', 'tool_not_allowed'],
    ['read', '../outside/private.txt', 'path_escape'],
    ['read', join(outside, 'private.txt'), 'path_escape'],
    ['read', '', 'path_invalid'], ['read', '\0bad', 'path_invalid'], ['read', 42, 'path_invalid'],
    ['read', '~/private.txt', 'path_shorthand'], ['read', '@docs/readme.txt', 'path_shorthand'],
    ['read', 'missing.txt', 'tool_path']]) {
    const gate = (await fresh());
    assert.equal(gate.ready, true);
    const before = lines.length;
    const blocked = (await gate.call('tool_call', event(name, path)));
    assert.equal(blocked.block, true, `${name}:${path}`);
    assert.equal(blocked.terminate, true);
    assert.deepEqual(gate.active(), []);
    assert.equal((await gate.call('tool_call', event('read', 'docs/readme.txt'))).block, true);
    assert.deepEqual(lines.slice(before).map(line => JSON.parse(line)),
      [{ type: 'ephy_navigation_guard_violation', version: 1, reason_code: reasonCode }]);
  }
  const frozen = (await fresh());
  const frozenInput = Object.freeze({ path: 'docs/readme.txt' });
  const frozenResult = (await frozen.call('tool_call', { toolName: 'read', input: frozenInput }));
  assert.equal(frozenResult.block, true);
  assert.equal(frozenResult.terminate, true);
  assert.deepEqual(frozen.active(), []);
  assert.equal(frozenInput.path, 'docs/readme.txt');
  assert.deepEqual(JSON.parse(lines.at(-1)),
    { type: 'ephy_navigation_guard_violation', version: 1, reason_code: 'tool_path_rewrite' });
  for (const failure of [new Error('PRIVATE_NATIVE_ERROR /private/fixture.txt'),
    { message: 'PRIVATE_NATIVE_ERROR /private/fixture.txt' }]) {
    const unreadable = (await fresh());
    const nativeLstat = fs.lstatSync;
    const before = lines.length;
    try {
      fs.lstatSync = (path, ...options) => {
        if (path === join(root, 'docs', 'readme.txt')) throw failure;
        return nativeLstat(path, ...options);
      };
      syncBuiltinESMExports();
      const blocked = (await unreadable.call('tool_call', event('read', 'docs/readme.txt')));
      assert.equal(blocked.block, true);
      assert.equal(blocked.terminate, true);
      assert.deepEqual(unreadable.active(), []);
      assert.deepEqual(lines.slice(before).map(line => JSON.parse(line)),
        [{ type: 'ephy_navigation_guard_violation', version: 1, reason_code: 'tool_path' }]);
      assert.ok(!lines.slice(before).join('').includes('PRIVATE_NATIVE_ERROR'));
      assert.ok(!lines.slice(before).join('').includes('/private/fixture.txt'));
    } finally {
      fs.lstatSync = nativeLstat;
      syncBuiltinESMExports();
    }
  }
  assert.equal((await (await fresh({ start: false })).call('tool_call', event('read', 'docs/readme.txt'))).block, true);
  const overridden = (await fresh({ override: true }));
  assert.equal(overridden.ready, false);
  assert.deepEqual(overridden.active(), []);
  assert.deepEqual(JSON.parse(lines.at(-1)),
    { type: 'ephy_navigation_guard_failed', version: 1, reason_code: 'read_tool_provenance' });
  assert.equal((await (await fresh()).call('tool_call', event('read', 'docs/readme.txt'), outside)).block, true);
  for (const toolList of [builtinTools, [...builtinTools, { name: 'governance_ack' }],
    [...builtinTools, { name: 'governance_ack', sourceInfo: { source: 'sdk', path: gatePath } }],
    [...builtinTools, { name: 'governance_ack', sourceInfo: { source: 'cli', path: policyPath } }],
    [...builtinTools, ackTool, ackTool]]) {
    assert.equal((await fresh({ toolList })).ready, false, 'untrusted or duplicate ack provenance');
  }
  const manifestHash = process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST_SHA256;
  process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST_SHA256 = '0'.repeat(64);
  assert.equal((await fresh()).ready, false);
  process.env.EPHY_DESKTOP_GOVERNANCE_MANIFEST_SHA256 = manifestHash;
  process.env.DUAL_GOVERNANCE_ROLE = 'integration';
  assert.equal((await fresh()).ready, false);
  process.env.DUAL_GOVERNANCE_ROLE = 'planner';
  const gateSource = fs.readFileSync(gatePath);
  fs.appendFileSync(gatePath, '\n// synthetic tamper');
  assert.equal((await fresh()).ready, false);
  fs.writeFileSync(gatePath, gateSource);
  assert.equal(fs.readFileSync(gatePath).length, gateSource.length);
  assert.equal(hash(fs.readFileSync(gatePath)), manifest.gate_sha256);
  assert.equal((await fresh()).ready, true, 'restored canonical fixture must permit later controls');
  const shell = (await fresh());
  assert.equal((await shell.call('user_bash', { command: 'never executed' })).result.exitCode, 1);
  assert.equal((await shell.call('tool_call', event('read', 'docs/readme.txt'))).block, true);
  if (process.platform === 'win32') {
    for (const path of ['C:private.txt', 'docs/readme.txt:secret', '\\\\server\\share',
      '\\\\?\\C:\\private.txt', 'docs./readme.txt', 'NUL', 'COM1.txt']) {
      assert.equal((await (await fresh()).call('tool_call', event('read', path))).block, true, path);
    }
  }
  // Links introduced after startup must also be denied, including ancestors,
  // recursive searches, and links that point back inside the allowed root.
  for (const linkTarget of [outside, join(root, 'docs')]) {
    const existingLinkedFile = linkTarget === outside ? 'linked/private.txt' : 'linked/readme.txt';
    for (const [name, path] of [['read', existingLinkedFile]]) {
      const gate = (await fresh());
      symlinkSync(linkTarget, join(root, 'linked'), process.platform === 'win32' ? 'junction' : 'dir');
      assert.equal((await gate.call('tool_call', event(name, path))).block, true);
      assert.equal((await fresh()).ready, false);
      unlinkSync(join(root, 'linked'));
    }
  }
  const hardlink = (await fresh());
  linkSync(join(outside, 'private.txt'), join(root, 'hardlinked.txt'));
  assert.equal((await hardlink.call('tool_call', event('read', 'hardlinked.txt'))).block, true);
  assert.equal((await fresh()).ready, false);
  unlinkSync(join(root, 'hardlinked.txt'));
  // Pi unconditionally normalizes these Unicode spaces in absolute tool paths.
  // A guard must never validate one file and let the builtin open its sibling.
  const spaces = ['\u00a0', ...Array.from({ length: 11 }, (_, n) => String.fromCharCode(0x2000 + n)),
    '\u202f', '\u205f', '\u3000'];
  writeFileSync(join(root, 'a b.txt'), 'normalized sibling');
  const unicodeArgument = (await fresh());
  assert.equal(unicodeArgument.ready, true);
  assert.equal((await unicodeArgument.call('tool_call', event('read', 'new\u00a0path.txt'))).block, true);
  assert.equal(JSON.parse(lines.at(-1)).reason_code, 'path_normalized_space');
  for (const space of spaces) {
    const name = `a${space}b.txt`;
    writeFileSync(join(root, name), 'original path');
    assert.equal((await (await fresh()).call('tool_call', event('read', name))).block, true);
  }
  const unicodeRoot = join(fixture, 'work\u00a0tree');
  const normalizedRoot = join(fixture, 'work tree');
  mkdirSync(unicodeRoot);
  mkdirSync(normalizedRoot);
  writeFileSync(join(unicodeRoot, 'safe.txt'), 'inside');
  writeFileSync(join(normalizedRoot, 'safe.txt'), 'synthetic outside sibling');
  process.chdir(unicodeRoot);
  assert.equal((await fresh({ cwd: unicodeRoot })).ready, false);
  process.chdir(root);
} finally {
  fs.writeSync = nativeWrite;
  syncBuiltinESMExports();
}
console.log(JSON.stringify({ passed: true, controls: 'allowlist, readiness, canonical paths, links, shell' }));
""".replace("// GOVERNANCE_FIXTURE", GOVERNANCE_FIXTURE)
    node = shutil.which("node")
    assert node, "Node.js with --experimental-strip-types is required for the guard controls"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", script,
         GUARD.as_uri(), str(tmp_path), str(ROOT)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] is True


def test_guard_reason_code_wire_contract_matches_adapter_allowlist():
    from ephy_worker.desktop.adapter import NAVIGATION_REASON_CODES

    source = GUARD.read_text(encoding="utf-8")
    mapping = source.split("const REJECTION_REASON_CODES =", 1)[1].split("]);", 1)[0]
    phases = source.split("type DiagnosticPhase =", 1)[1].split(";", 1)[0]
    codes = set(re.findall(r'\["[^"\n]+", "([a-z_]+)"\]', mapping))
    codes.update(re.findall(r'"([a-z_]+)"', phases))
    assert codes == NAVIGATION_REASON_CODES


def test_navigation_attribute_async_lifecycle_and_identity(tmp_path):
    """Run real guard hooks with deterministic owned-child events and real paths."""
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import cp from 'node:child_process';
import { createHash } from 'node:crypto';
import { EventEmitter, getEventListeners } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import { dirname, join, resolve } from 'node:path';
const { default: setup } = await import(process.argv[1]);
const fixture = resolve(process.argv[2]);
// GOVERNANCE_FIXTURE
const root = join(fixture, 'worktree');
const outside = join(fixture, 'outside');
fs.mkdirSync(root);
fs.mkdirSync(outside);
fs.mkdirSync(join(root, 'docs'));
fs.writeFileSync(join(root, 'docs', 'readme.txt'), 'safe');
process.chdir(root);
const nativeSpawn = cp.spawn;
const nativePlatform = Object.getOwnPropertyDescriptor(process, 'platform');
const nativeSystemRoot = process.env.SystemRoot;
const nativeWrite = fs.writeSync;
const nativeSetTimeout = globalThis.setTimeout;
const nativeClearTimeout = globalThis.clearTimeout;
const lines = [];
const children = [];
const timers = [];
let mode = 'auto';
fs.writeSync = function (...args) {
  if (args[0] !== 1) return Reflect.apply(nativeWrite, this, args);
  lines.push(JSON.parse(String(args[1])));
  return Buffer.byteLength(args[1]);
};
globalThis.setTimeout = (callback, milliseconds) => {
  assert.equal(milliseconds, 8000, 'full unchanged per-invocation deadline');
  const timer = { callback, active: true };
  timers.push(timer);
  return timer;
};
globalThis.clearTimeout = timer => { timer.active = false; };
cp.spawn = (executable, args, options) => {
  assert.equal(executable, join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'));
  assert.deepEqual(args.slice(0, 4), ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand']);
  assert.equal(args.length, 5);
  const fixedScript = Buffer.from(args[4], 'base64').toString('utf16le');
  assert.ok(fixedScript.includes('} catch { exit 25 }'));
  assert.ok(!fixedScript.includes(root), 'path remains environment data');
  assert.deepEqual(options.stdio, ['ignore', 'ignore', 'pipe']);
  assert.equal(options.windowsHide, true);
  assert.equal(options.shell, undefined);
  assert.equal(options.timeout, undefined, 'native stale-loop timeout is unused');
  assert.equal(options.env.EPHY_GUARD_ATTRIBUTE_TREE === '1' || options.env.EPHY_GUARD_ATTRIBUTE_TREE === '0', true);
  if (mode === 'throw') throw new Error('PRIVATE_SPAWN_ERROR');
  const child = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kills = [];
  child.kill = signal => { child.kills.push(signal); return true; };
  child.options = options;
  children.push(child);
  if (mode === 'auto' || (mode === 'hold_tree' && options.env.EPHY_GUARD_ATTRIBUTE_TREE !== '1')
    || (mode === 'hold_runtime' && options.env.EPHY_GUARD_ATTRIBUTE_TARGET !== runtime)) {
    queueMicrotask(() => child.emit('close', 0, null));
  }
  return child;
};
syncBuiltinESMExports();
const flush = () => new Promise(resolve => setImmediate(resolve));
async function fresh({ start = true } = {}) {
  const handlers = new Map();
  let active = ['governance_ack'];
  setup({ on: (name, handler) => handlers.set(name, handler),
    setActiveTools: names => { active = names; }, getAllTools: () => [...builtinTools, ackTool] });
  const context = { cwd: root, signal: undefined };
  const call = (name, event = {}, signal) => {
    context.signal = signal;
    return handlers.get(name)(event, context);
  };
  const before = lines.length;
  if (start) await call('session_start');
  return { call, context, active: () => active,
    ready: lines.slice(before).some(line => line.type === 'ephy_navigation_guard_ready') };
}
const read = () => ({ toolName: 'read', input: { path: 'docs/readme.txt', offset: 1, limit: 1 } });
function cleaned(child) {
  assert.equal(child.listenerCount('close'), 0);
  assert.equal(child.listenerCount('error'), 0);
  assert.equal(child.stderr.listenerCount('error'), 0);
  assert.equal(child.stderr.listenerCount('data'), 0);
  assert.ok(timers.every(timer => !timer.active), 'all timers cleared after close');
}
async function pendingRead() {
  mode = 'auto';
  const gate = await fresh();
  assert.equal(gate.ready, true);
  mode = 'hold';
  const event = read();
  const controller = new AbortController();
  let settled = false;
  const promise = gate.call('tool_call', event, controller.signal).then(result => { settled = true; return result; });
  await flush();
  const child = children.at(-1);
  assert.equal(settled, false, 'pending validation cannot admit a read');
  assert.equal(event.input.path, 'docs/readme.txt');
  return { gate, event, controller, promise, child, settled: () => settled };
}
try {
  // Exercise the Windows helper deterministically on every host. spawn is
  // already mocked, so this never starts PowerShell or changes the host OS.
  Object.defineProperty(process, 'platform', { ...nativePlatform, value: 'win32' });
  process.env.SystemRoot = join(fixture, 'mock-system-root');
  assert.equal(process.platform, 'win32');
  for (const [code, signal, reason] of [[0, null, null], [23, null, 'path_reparse'],
    [25, null, 'windows_attribute_check'], [null, null, 'windows_attribute_check'],
    [0, 'SIGTERM', 'windows_attribute_check']]) {
    const run = await pendingRead();
    run.child.emit('close', code, signal);
    const result = await run.promise;
    if (reason === null) {
      assert.equal(result, undefined);
      assert.equal(run.event.input.path, join(root, 'docs', 'readme.txt'));
      assert.deepEqual([run.event.input.offset, run.event.input.limit], [1, 1]);
    } else {
      assert.equal(result.block, true);
      assert.equal(result.terminate, true);
      assert.deepEqual(run.gate.active(), []);
      assert.equal(lines.at(-1).reason_code, reason);
      assert.equal(run.event.input.path, 'docs/readme.txt');
    }
    assert.equal(getEventListeners(run.controller.signal, 'abort').length, 0);
    cleaned(run.child);
  }
  for (const action of ['error', 'stderr_error', 'timeout', 'overflow', 'abort', 'shutdown', 'permanent']) {
    const run = await pendingRead();
    if (action === 'error') run.child.emit('error', new Error('PRIVATE_NATIVE_ERROR'));
    if (action === 'stderr_error') run.child.stderr.emit('error', new Error('PRIVATE_STREAM_ERROR'));
    if (action === 'timeout') timers.at(-1).callback();
    if (action === 'overflow') run.child.stderr.emit('data', Buffer.alloc(4097, 65));
    if (action === 'abort') run.controller.abort();
    if (action === 'shutdown') await run.gate.call('session_shutdown');
    if (action === 'permanent') await run.gate.call('user_bash', { command: 'never executed' });
    await flush();
    assert.equal(run.settled(), false, 'failure must wait for owned child close');
    if (['stderr_error', 'timeout', 'overflow', 'abort', 'shutdown'].includes(action)) {
      assert.deepEqual(run.child.kills, ['SIGKILL'], 'kill only this owned child');
    }
    run.child.emit('close', 0, null); // A late zero exit never undoes failure.
    const blocked = await run.promise;
    assert.equal(blocked.block, true, action);
    assert.equal(blocked.terminate, true, action);
    assert.equal(run.event.input.path, 'docs/readme.txt');
    assert.deepEqual(run.gate.active(), []);
    assert.ok(!JSON.stringify(lines).includes('PRIVATE_'));
    assert.equal(getEventListeners(run.controller.signal, 'abort').length, 0);
    cleaned(run.child);
  }
  const boundary = await pendingRead();
  boundary.child.stderr.emit('data', Buffer.alloc(2048));
  boundary.child.stderr.emit('data', Buffer.alloc(2048));
  assert.deepEqual(boundary.child.kills, []);
  boundary.child.emit('close', 0, null);
  assert.equal(await boundary.promise, undefined, '4096-byte stderr is within original cap');
  cleaned(boundary.child);
  mode = 'auto';
  const preabort = await fresh();
  const aborted = new AbortController();
  aborted.abort();
  const beforeAbort = children.length;
  assert.equal((await preabort.call('tool_call', read(), aborted.signal)).block, true);
  assert.equal(children.length, beforeAbort, 'pre-aborted check must not spawn');
  const throwGate = await fresh();
  mode = 'throw';
  assert.equal((await throwGate.call('tool_call', read())).block, true);
  assert.equal(lines.at(-1).reason_code, 'windows_attribute_check');
  assert.ok(!JSON.stringify(lines).includes('PRIVATE_SPAWN_ERROR'));

  // Same path, regular type and realpath are insufficient: compare exact file IDs.
  const replacement = join(outside, 'replacement.txt');
  const original = join(root, 'docs', 'readme.txt');
  const saved = join(outside, 'saved.txt');
  fs.writeFileSync(replacement, 'safe');
  const swapped = await pendingRead();
  const priorId = fs.lstatSync(original, { bigint: true }).ino;
  fs.renameSync(original, saved);
  fs.renameSync(replacement, original);
  assert.notEqual(fs.lstatSync(original, { bigint: true }).ino, priorId);
  assert.equal(fs.lstatSync(original).isSymbolicLink(), false);
  swapped.child.emit('close', 0, null);
  assert.equal((await swapped.promise).block, true, 'regular file replacement during await');
  assert.equal(swapped.event.input.path, 'docs/readme.txt');
  cleaned(swapped.child);
  fs.unlinkSync(original);
  fs.renameSync(saved, original);

  // Every lexical ancestor is bound without requiring its sibling timestamps
  // to stay unchanged. A regular-looking identity replacement still fails.
  let ancestor = dirname(original);
  while (true) {
    const run = await pendingRead();
    const nativeLstat = fs.lstatSync;
    try {
      fs.lstatSync = (path, options) => {
        const info = nativeLstat(path, options);
        if (path !== ancestor || !options?.bigint) return info;
        return Object.assign(Object.create(Object.getPrototypeOf(info)), info, { ino: info.ino + 1n });
      };
      syncBuiltinESMExports();
      run.child.emit('close', 0, null);
      assert.equal((await run.promise).block, true, ancestor);
      assert.equal(run.event.input.path, 'docs/readme.txt');
      cleaned(run.child);
    } finally {
      fs.lstatSync = nativeLstat;
      syncBuiltinESMExports();
    }
    if (ancestor === dirname(ancestor)) break;
    ancestor = dirname(ancestor);
  }
  const sibling = await pendingRead();
  const unrelated = join(fixture, 'unrelated-sibling.txt');
  fs.writeFileSync(unrelated, 'other task activity');
  sibling.child.emit('close', 0, null);
  assert.equal(await sibling.promise, undefined, 'unrelated ancestor sibling activity is allowed');
  cleaned(sibling.child);
  fs.unlinkSync(unrelated);

  const movedContext = await pendingRead();
  movedContext.gate.context.cwd = outside;
  movedContext.child.emit('close', 0, null);
  assert.equal((await movedContext.promise).block, true);
  assert.equal(lines.at(-1).reason_code, 'session_directory');
  cleaned(movedContext.child);
  const changedProvenance = await pendingRead();
  const gateBytes = fs.readFileSync(gatePath);
  try {
    fs.appendFileSync(gatePath, '\n// synthetic interleaved change');
    changedProvenance.child.emit('close', 0, null);
    assert.equal((await changedProvenance.promise).block, true);
    assert.equal(lines.at(-1).reason_code, 'ack_tool_provenance');
    cleaned(changedProvenance.child);
  } finally {
    fs.writeFileSync(gatePath, gateBytes);
    assert.equal(hash(fs.readFileSync(gatePath)), manifest.gate_sha256);
  }

  // Root tree validation also binds every descendant identity, including entries
  // whose native reparse attributes would not change Node's regular-file type.
  mode = 'auto';
  const treeGate = await fresh({ start: false });
  fs.writeFileSync(replacement, 'safe');
  mode = 'hold_tree';
  const treeBefore = lines.length;
  const treeRun = treeGate.call('session_start');
  await flush();
  const treeChild = children.at(-1);
  assert.equal(treeChild.options.env.EPHY_GUARD_ATTRIBUTE_TREE, '1');
  fs.renameSync(original, saved);
  fs.renameSync(replacement, original);
  treeChild.emit('close', 0, null);
  await treeRun;
  assert.ok(!lines.slice(treeBefore).some(line => line.type === 'ephy_navigation_guard_ready'));
  assert.equal(lines.at(-1).type, 'ephy_navigation_guard_failed');
  assert.deepEqual(treeGate.active(), []);
  cleaned(treeChild);
  fs.unlinkSync(original);
  fs.renameSync(saved, original);

  // Keep earlier worktree observations bound through later governance awaits.
  mode = 'auto';
  const lateTreeGate = await fresh({ start: false });
  fs.writeFileSync(replacement, 'safe');
  mode = 'hold_runtime';
  const lateBefore = lines.length;
  const lateRun = lateTreeGate.call('session_start');
  await flush();
  const lateChild = children.at(-1);
  assert.equal(lateChild.options.env.EPHY_GUARD_ATTRIBUTE_TARGET, runtime);
  fs.renameSync(original, saved);
  fs.renameSync(replacement, original);
  lateChild.emit('close', 0, null);
  await lateRun;
  assert.ok(!lines.slice(lateBefore).some(line => line.type === 'ephy_navigation_guard_ready'));
  assert.equal(lines.at(-1).type, 'ephy_navigation_guard_failed');
  assert.deepEqual(lateTreeGate.active(), []);
  cleaned(lateChild);
  fs.unlinkSync(original);
  fs.renameSync(saved, original);

  mode = 'hold';
  const starting = await fresh({ start: false });
  const startBefore = lines.length;
  const startRun = starting.call('session_start');
  await flush();
  const startChild = children.at(-1);
  const childCount = children.length;
  assert.equal((await starting.call('tool_call', read())).block, true);
  startChild.emit('close', 0, null);
  await startRun;
  assert.equal(children.length, childCount, 'no further checks after permanent startup failure');
  assert.ok(!lines.slice(startBefore).some(line => line.type === 'ephy_navigation_guard_ready'));
  assert.deepEqual(starting.active(), []);
  cleaned(startChild);
} finally {
  cp.spawn = nativeSpawn;
  Object.defineProperty(process, 'platform', nativePlatform);
  if (nativeSystemRoot === undefined) delete process.env.SystemRoot;
  else process.env.SystemRoot = nativeSystemRoot;
  fs.writeSync = nativeWrite;
  globalThis.setTimeout = nativeSetTimeout;
  globalThis.clearTimeout = nativeClearTimeout;
  syncBuiltinESMExports();
}
assert.deepEqual(Object.getOwnPropertyDescriptor(process, 'platform'), nativePlatform);
assert.equal(process.env.SystemRoot, nativeSystemRoot);
console.log(JSON.stringify({ passed: true, controls: 'async lifecycle, limits, abort, shutdown, identity swaps' }));
""".replace("// GOVERNANCE_FIXTURE", GOVERNANCE_FIXTURE)
    node = shutil.which("node")
    assert node, "Node.js with --experimental-strip-types is required"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", script,
         GUARD.as_uri(), str(tmp_path), str(ROOT)],
        cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] is True


def test_navigation_preserves_canonical_governance_state_machine(tmp_path):
    """Real unchanged gate + guard hooks; synthetic Pi events, no provider."""
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { syncBuiltinESMExports } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const { default: setup } = await import(process.argv[1]);
const fixture = resolve(process.argv[2]);
// GOVERNANCE_FIXTURE
const { default: gateSetup } = await import(pathToFileURL(gatePath).href);
const root = join(fixture, 'worktree');
fs.mkdirSync(root);
fs.writeFileSync(join(root, 'README.md'), 'synthetic public navigation fixture');
process.chdir(root);
const nativeWrite = fs.writeSync;
const nativeExit = process.exit;
let lines = [];
fs.writeSync = function (...args) {
  if (args[0] !== 1) return Reflect.apply(nativeWrite, this, args);
  const value = args[1];
  lines.push(JSON.parse(String(value)));
  return Buffer.byteLength(value);
};
process.exit = code => { throw new Error(`OWNED_PI_EXIT_${code}`); };
syncBuiltinESMExports();
class Pi {
  active = [];
  handlers = new Map();
  tools = new Map();
  on(name, handler) { this.handlers.set(name, [...(this.handlers.get(name) ?? []), handler]); }
  registerTool(tool) { this.tools.set(tool.name, tool); }
  getAllTools() { return [...builtinTools, ...[...this.tools.keys()].map(name => ({ ...ackTool, name }))]; }
  getActiveTools() { return [...this.active]; }
  setActiveTools(names) { this.active = [...names]; }
  async emit(name, event) {
    let last;
    for (const handler of this.handlers.get(name) ?? []) {
      const result = await handler(event, { cwd: root });
      if (result === undefined) continue;
      last = result;
      if (result.block) return result;
      if (name === 'before_provider_request') event = { ...event, payload: result };
      if (name === 'context' && result.messages) event = { ...event, messages: result.messages };
      if (name === 'message_end' && result.message) event = { ...event, message: result.message };
      if (name === 'tool_result') event = { ...event, ...result };
    }
    return last;
  }
}
async function fresh({ loadFailure = false } = {}) {
  const pi = new Pi();
  setup(pi);
  if (loadFailure) process.env.DUAL_GOVERNANCE_ROLE = 'invalid';
  gateSetup(pi);
  process.env.DUAL_GOVERNANCE_ROLE = 'planner';
  await pi.emit('session_start', {});
  const start = { systemPromptOptions: { sections: {} } };
  await pi.emit('before_agent_start', start);
  const section = start.systemPromptOptions.sections.development_governance;
  return { pi, section };
}
function payload(section, names, role = 'system') {
  return { model: 'synthetic', messages: [{ role,
    content: `<development_governance>\n${section}\n</development_governance>` }],
    tools: names.map(name => ({ type: 'function', function: { name } })) };
}
function envelope(section) {
  const value = label => section.match(new RegExp(`^${label}: (.+)$`, 'm'))[1];
  return { policyId: value('Policy-ID'), policySha256: value('Policy-SHA256'),
    endMarker: value('Policy-End-Marker'), nonce: value('Acknowledgement-Nonce'), role: value('Role') };
}
async function prepare(session, overrides = {}, providerRole = 'system') {
  const { pi, section } = session;
  assert.deepEqual(pi.active, ['governance_ack']);
  const quarantined = await pi.emit('context', { messages: [
    { role: 'system', sections: { development_governance: section } },
    { role: 'user', content: [{ type: 'text', text: 'original private task' }] } ] });
  assert.ok(!JSON.stringify(quarantined).includes('original private task'));
  const sent = await pi.emit('before_provider_request', { payload: payload(section,
    ['read', 'governance_ack', 'bash', 'subagent', 'grep'], providerRole) });
  assert.deepEqual(sent.tools.map(tool => tool.function.name), ['governance_ack']);
  assert.equal(sent.tool_choice.function.name, 'governance_ack');
  const input = { ...envelope(section), ...overrides };
  const message = await pi.emit('message_end', { message: { role: 'assistant', stopReason: 'toolUse',
    content: [{ type: 'toolCall', id: 'ack-1', name: 'governance_ack', arguments: input }] } });
  return { input, message };
}
async function executeAck(session, input) {
  const before = JSON.stringify(input);
  assert.equal(await session.pi.emit('tool_call', { toolName: 'governance_ack', toolCallId: 'ack-1', input }),
    undefined);
  assert.equal(JSON.stringify(input), before);
  const result = await session.pi.tools.get('governance_ack').execute('ack-1', input);
  assert.equal(result.details.phase, 'context_delivery_pending');
  return result;
}
try {
  const happy = await fresh();
  const { input } = await prepare(happy);
  const result = await executeAck(happy, input);
  const final = await happy.pi.emit('tool_result', { toolName: 'governance_ack', toolCallId: 'ack-1',
    isError: false, ...result });
  assert.equal(final.details.phase, 'ready');
  assert.equal(final.details.governanceContext.complete, true);
  const restored = await happy.pi.emit('context', { messages: [
    { role: 'system', sections: { development_governance: happy.section } },
    { role: 'user', content: [{ type: 'text', text: 'original private task' }] } ] });
  const postSection = restored.messages.find(message => message.role === 'system').sections.development_governance;
  assert.match(postSection, /Acknowledgement-State: verified/);
  assert.ok(JSON.stringify(restored).includes('original private task'));
  const next = await happy.pi.emit('before_provider_request', { payload: payload(postSection, happy.pi.active) });
  assert.deepEqual(next.tools.map(tool => tool.function.name), ['read']);
  const rawPost = payload(postSection, ['read'], 'developer');
  rawPost.messages[0].content = `Base prompt\n<cwd>${root}</cwd>\n\n${postSection}`;
  const rawNext = await happy.pi.emit('before_provider_request', { payload: rawPost });
  assert.deepEqual(rawNext.tools.map(tool => tool.function.name), ['read']);
  assert.equal(lines.filter(value => value.type === 'ephy_desktop_governance_envelope').length, 1);
  const attestation = lines.find(value => value.type === 'ephy_desktop_governance_envelope');
  assert.equal(attestation.nonce, input.nonce);
  assert.deepEqual(attestation.context_manifest, contextManifest);
  assert.equal(await happy.pi.emit('tool_call', { toolName: 'read', input: { path: 'README.md' } }), undefined);
  assert.equal((await happy.pi.emit('tool_call', { toolName: 'subagent', input: {} })).block, true);
  const developer = await fresh();
  const developerPrepared = await prepare(developer, {}, 'developer');
  const developerPending = await executeAck(developer, developerPrepared.input);
  assert.equal((await developer.pi.emit('tool_result', { toolName: 'governance_ack', toolCallId: 'ack-1',
    isError: false, ...developerPending })).details.phase, 'ready');
  for (const mode of ['system-and-developer', 'repeated-system', 'user-role', 'initial-raw']) {
    const session = await fresh();
    const request = payload(session.section, ['governance_ack']);
    if (mode === 'system-and-developer') request.messages.push({ ...request.messages[0], role: 'developer' });
    if (mode === 'repeated-system') request.messages.push({ ...request.messages[0] });
    if (mode === 'user-role') request.messages[0].role = 'user';
    if (mode === 'initial-raw') request.messages[0].content = session.section;
    await assert.rejects(session.pi.emit('before_provider_request', { payload: request }), /OWNED_PI_EXIT_78/);
  }
  for (const mode of ['repeated-header', 'wrong-nonce', 'raw-required']) {
    const session = await fresh();
    await prepare(session);
    let section = session.section.replace('Acknowledgement-State: required', 'Acknowledgement-State: verified');
    if (mode === 'repeated-header') section += '\nPolicy-ID: ephy.system-development-governance.v1';
    if (mode === 'wrong-nonce') section = section.replace(envelope(session.section).nonce,
      '00000000-0000-0000-0000-000000000000');
    if (mode === 'raw-required') section = session.section;
    const request = payload(section, ['read'], 'developer');
    request.messages[0].content = section;
    await assert.rejects(session.pi.emit('before_provider_request', { payload: request }), /OWNED_PI_EXIT_78/);
  }
  for (const overrides of [{ nonce: 'wrong' }, { role: 'integration' }, { extra: 'not permitted' }]) {
    const session = await fresh();
    const prepared = await prepare(session, overrides);
    assert.match(prepared.message.message.content[0].text, /GOVERNANCE_GATE_FAILURE/);
    assert.equal((await session.pi.emit('tool_call', { toolName: 'governance_ack', toolCallId: 'ack-1',
      input: prepared.input })).block, true);
  }
  const tampered = await fresh();
  const prepared = await prepare(tampered);
  const pending = await executeAck(tampered, prepared.input);
  const altered = await tampered.pi.emit('tool_result', { toolName: 'governance_ack', toolCallId: 'ack-1',
    isError: false, ...pending, content: [{ type: 'text', text: 'partial context' }] });
  assert.equal(altered.isError, true);
  assert.match(altered.content[0].text, /GOVERNANCE_GATE_FAILURE/);
  assert.deepEqual(tampered.pi.active, []);
  const noAck = await fresh();
  assert.equal((await noAck.pi.emit('tool_call', { toolName: 'read', input: { path: 'README.md' } })).block, true);
  const loadFailure = await fresh({ loadFailure: true });
  await assert.rejects(loadFailure.pi.emit('before_provider_request', {
    payload: payload(loadFailure.section, ['governance_ack']) }), /OWNED_PI_EXIT_78/);
  const missingSection = await fresh();
  await assert.rejects(missingSection.pi.emit('before_provider_request', {
    payload: { messages: [{ role: 'system', content: 'absent' }], tools: [] } }), /OWNED_PI_EXIT_78/);
  const missingAck = await fresh();
  await assert.rejects(missingAck.pi.emit('before_provider_request', {
    payload: payload(missingAck.section, ['read']) }), /OWNED_PI_EXIT_78/);
  const repeatedSection = await fresh();
  const repeated = payload(repeatedSection.section, ['governance_ack']);
  repeated.messages[0].content += repeated.messages[0].content;
  await assert.rejects(repeatedSection.pi.emit('before_provider_request', { payload: repeated }), /OWNED_PI_EXIT_78/);
} finally {
  fs.writeSync = nativeWrite;
  process.exit = nativeExit;
  syncBuiltinESMExports();
}
console.log(JSON.stringify({ passed: true, controls: 'canonical gate, ack-only, final context, read-only' }));
""".replace("// GOVERNANCE_FIXTURE", GOVERNANCE_FIXTURE)
    node = shutil.which("node")
    assert node, "Node.js with --experimental-strip-types is required"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--loader", (ROOT / "tests/governance-typebox-loader.mjs").as_uri(),
         "--input-type=module", "-e", script, GUARD.as_uri(), str(tmp_path), str(ROOT)],
        cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=240, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] is True
