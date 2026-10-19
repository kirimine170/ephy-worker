import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const source = resolve(import.meta.dirname, "..");
const gitExe = process.argv[2];
assert.ok(gitExe, "Pass an absolute Git executable");
const outputDirectory = process.argv[3] && resolve(process.argv[3]);
if (outputDirectory) mkdirSync(outputDirectory, { recursive: true });
const script = join(source, "tools/pi-local/controller-repair-handoff.mjs");
const sourceGuard = join(source, "tools/pi-local/ephy-campaign-guard.ts");
const hash = (file) => createHash("sha256").update(readFileSync(file)).digest("hex");
const before = hash(sourceGuard);
const temporary = mkdtempSync(join(tmpdir(), "ephy-handoff-smoke-"));
const pack = join(temporary, "pack");
const trace = [];
// A PowerShell 7 parent can export its module path to powershell.exe (5.1).
// Test the Windows PowerShell launcher with its own built-in module directory.
const windowsPowerShellEnv = { ...process.env, PSModulePath: join(process.env.SystemRoot ?? "C:\\Windows", "System32/WindowsPowerShell/v1.0/Modules") };
function run(label, argv) {
  const result = spawnSync(process.execPath, [script, ...argv], { encoding: "utf8", windowsHide: true, timeout: 240000, maxBuffer: 8 * 1024 * 1024 });
  assert.ifError(result.error); assert.equal(result.signal, null);
  trace.push({ label, code: result.status, stdout: result.stdout, stderr: result.stderr });
  return result;
}
try {
  const baselineInput = join(temporary, "original-exercise.ts");
  writeFileSync(baselineInput, Buffer.concat([readFileSync(sourceGuard), Buffer.from("\n// Distinct frozen exercise input; never the runtime.\n")]));
  assert.equal(run("reject-wrong-baseline-identity", ["prepare", source, pack, resolve(gitExe), baselineInput, "0".repeat(64)]).status, 2);
  assert.equal(existsSync(pack), false);
  const prepared = run("prepare", ["prepare", source, pack, resolve(gitExe), baselineInput, hash(baselineInput)]);
  assert.equal(prepared.status, 0, prepared.stderr);
  const frozen = JSON.parse(prepared.stdout), manifest = JSON.parse(readFileSync(frozen.manifestPath));
  const contract = JSON.parse(readFileSync(frozen.contractPath));
  assert.equal(manifest.baselineControllerSha256, hash(baselineInput));
  assert.equal(manifest.runtimeControllerSha256, before);
  assert.notEqual(manifest.baselineControllerSha256, manifest.runtimeControllerSha256);
  assert.equal(hash(join(manifest.candidate, "tools/pi-local/ephy-campaign-guard.ts")), manifest.baselineControllerSha256);
  assert.equal(contract.tasks[0].maxRepairs, 2);
  assert.equal(contract.maxDurationSeconds, 3600);
  assert.deepEqual(contract.tasks[0].steps[0].checks[0].candidateFailureExitCodes, [10]);
  const checkArgs = [frozen.manifestPath, frozen.manifestSha256];
  const trustText = readFileSync(manifest.gitConfigGlobal, "utf8");
  assert.equal(trustText, `[safe]\n\tdirectory =\n\tdirectory = ${JSON.stringify(manifest.candidate.replaceAll("\\", "/"))}\n`);
  assert.equal(manifest.fixedFiles["inputs/git-trust.config"], hash(manifest.gitConfigGlobal));
  if (process.platform === "win32") {
    const launch = join(pack, "start-pi.ps1").replaceAll("'", "''");
    const probe = spawnSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
      `$env:GIT_CONFIG_GLOBAL = 'handoff-sentinel'; $savedPath = $env:PATH; & '${launch}' -PreflightOnly; if ($LASTEXITCODE -ne 0) { exit 1 }; if ($env:GIT_CONFIG_GLOBAL -ne 'handoff-sentinel' -or $env:PATH -ne $savedPath) { throw 'Launcher leaked process settings' }`],
    { encoding: "utf8", windowsHide: true, timeout: 30000, env: windowsPowerShellEnv });
    assert.ifError(probe.error);
    trace.push({ label: "launcher-preflight-and-env-restore", code: probe.status, stdout: probe.stdout, stderr: probe.stderr });
    assert.equal(probe.status, 0, probe.stderr);
  }
  assert.equal(run("clean-preflight", ["preflight", ...checkArgs]).status, 0);
  assert.equal(run("refuse-reuse", ["prepare", source, pack, resolve(gitExe)]).status, 2);
  assert.equal(hash(frozen.manifestPath), frozen.manifestSha256);
  const candidateGuard = join(manifest.candidate, "tools/pi-local/ephy-campaign-guard.ts");
  writeFileSync(candidateGuard, Buffer.concat([readFileSync(candidateGuard), Buffer.from("\n// Handoff harness: no behavioral change.\n")]));
  assert.equal(run("dirty-preflight", ["preflight", ...checkArgs]).status, 2);
  const baseline = run("baseline-verification", ["verify", ...checkArgs, "all"]);
  assert.ok([0, 10].includes(baseline.status), baseline.stderr);
  const report = JSON.parse(baseline.stdout);
  assert.equal(report.passed, baseline.status === 0);
  assert.equal(report.legacySmokeExitCode, 0, "Existing smoke must still pass");
  assert.equal(report.candidate.controllerSha256, hash(candidateGuard));
  const detailed = JSON.parse(readFileSync(join(report.evidence, "result.json")));
  assert.equal(detailed.acceptance.results.length, 29);
  for (const name of ["unchanged-checks-still-complete", "explicit-candidate-failure-can-repair-and-pass", "candidate-repair-budget-still-enforced"]) {
    assert.equal(detailed.acceptance.results.find((item) => item.name === name).passed, true, name);
  }
  if (outputDirectory) writeFileSync(join(outputDirectory, "baseline-acceptance.json"), JSON.stringify(detailed, null, 2) + "\n");
  const fixedCheck = join(pack, "checks/acceptance.mjs");
  writeFileSync(fixedCheck, Buffer.concat([readFileSync(fixedCheck), Buffer.from("\n// Deliberate frozen-input tamper control.\n")]));
  const tampered = run("fixed-input-tamper", ["verify", ...checkArgs, "all"]);
  assert.equal(tampered.status, 2);
  assert.match(tampered.stderr, /Fixed input changed/);
  if (process.platform === "win32") {
    const launch = join(pack, "start-pi.ps1").replaceAll("'", "''");
    const failedProbe = spawnSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
      `$env:GIT_CONFIG_GLOBAL = 'failure-sentinel'; $savedPath = $env:PATH; & '${launch}' -PreflightOnly; if ($LASTEXITCODE -ne 2 -or $env:GIT_CONFIG_GLOBAL -ne 'failure-sentinel' -or $env:PATH -ne $savedPath) { throw 'Failed launcher leaked settings or accepted tampering' }; exit 0`],
    { encoding: "utf8", windowsHide: true, timeout: 30000, env: windowsPowerShellEnv });
    assert.ifError(failedProbe.error);
    trace.push({ label: "failed-launcher-restores-env", code: failedProbe.status, stdout: failedProbe.stdout, stderr: failedProbe.stderr });
    assert.equal(failedProbe.status, 0, failedProbe.stderr);
    const outcome = JSON.parse(readFileSync(join(manifest.evidence, "task-outcome.json")));
    assert.equal(outcome.outcome, "needs_input");
    assert.equal(outcome.exitCode, 2);
    assert.ok(outcome.question);
  }
  assert.equal(hash(sourceGuard), before, "Runtime controller was changed by preparation/verification");
  console.log(JSON.stringify({ passed: true, scenarios: trace.map(({ label, code }) => ({ label, code })), baselinePassed: report.passed, baselineChecks: report.checks, runtimeControllerUnchanged: true }, null, 2));
} finally {
  if (outputDirectory) {
    writeFileSync(join(outputDirectory, "handoff-smoke-trace.json"), JSON.stringify(trace, null, 2) + "\n");
    if (existsSync(join(pack, "evidence"))) cpSync(join(pack, "evidence"), join(outputDirectory, "fixture-evidence"), { recursive: true, errorOnExist: true, force: false });
  }
  // Only a mkdtemp-owned private fixture is removed; no user worktree metadata is shared.
  assert.equal(dirname(resolve(temporary)), resolve(tmpdir()));
  rmSync(temporary, { recursive: true, force: true });
}
