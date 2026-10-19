// Manager-side preparation/verification only. This file is never a Pi extension.
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const [mode, ...args] = process.argv.slice(2);
const target = "tools/pi-local/ephy-campaign-guard.ts";
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const hash = (path) => sha(readFileSync(path));
const write = (path, bytes) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, bytes); };
const git = (executable, cwd, ...argv) => execFileSync(executable, ["-C", cwd, ...argv], { windowsHide: true, timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
const ps = (value) => `'${value.replaceAll("'", "''")}'`;

function prepare(sourceArg, packArg, gitArg, baselineArg, baselineSha) {
  assert.ok(sourceArg && packArg && gitArg, "prepare SOURCE NEW_PACK ABSOLUTE_GIT");
  assert.ok(isAbsolute(gitArg) && existsSync(gitArg), "An existing absolute Git executable is required");
  const source = resolve(sourceArg), pack = resolve(packArg), gitExe = resolve(gitArg);
  assert.ok(!existsSync(pack), "Never reuse or overwrite an existing handoff");
  // Runtime safety fixes are manager work, not the candidate's implementation.
  // A new trial can retain the original frozen exercise with explicit identity.
  const baselineInput = baselineArg ? resolve(baselineArg) : join(source, target);
  if (baselineArg) assert.equal(hash(baselineInput), baselineSha, "Baseline input hash mismatch");
  const files = {
    "checks/acceptance.mjs": join(source, "tests/pi-campaign-repair-acceptance.mjs"),
    "checks/smoke.mjs": join(source, "tests/pi-campaign-guard-smoke.mjs"),
    "checks/loader.mjs": join(source, "tests/governance-typebox-loader.mjs"),
    "checks/verify.mjs": fileURLToPath(import.meta.url),
    "checks/outcome.mjs": join(source, "tools/pi-local/campaign-outcome.mjs"),
    "inputs/original-controller.ts": baselineInput,
    "inputs/AGENTS.md": join(source, "AGENTS.md"),
    "inputs/governance.md": join(source, "docs/system-development-governance.md"),
    "TASK.md": join(source, "docs/pi-controller-repair-task.md"),
  };
  // Read before creating any output. Frozen copies, not live manager tests, run later.
  const buffers = Object.fromEntries(Object.entries(files).map(([name, path]) => [name, readFileSync(path)]));
  mkdirSync(pack, { recursive: true });
  for (const [name, bytes] of Object.entries(buffers)) write(join(pack, name), bytes);
  const baseline = join(pack, "baseline-repository"), candidate = join(pack, "candidate"), evidence = join(pack, "evidence");
  // Git 2.37 for Windows rejects command-scope safe.directory. A per-process
  // global config is supported, without modifying the user's global config.
  const trustName = "inputs/git-trust.config";
  const gitConfigGlobal = join(pack, trustName);
  write(gitConfigGlobal, `[safe]\n\tdirectory =\n\tdirectory = ${JSON.stringify(candidate.replaceAll("\\", "/"))}\n`);
  mkdirSync(baseline); mkdirSync(evidence); mkdirSync(join(pack, "empty-hooks"));
  for (const [name, input] of Object.entries({
    [target]: "inputs/original-controller.ts", "AGENTS.md": "inputs/AGENTS.md",
    "docs/system-development-governance.md": "inputs/governance.md", "TASK.md": "TASK.md",
  })) write(join(baseline, name), buffers[input]);
  // This private fixture has no remote and shares no Git metadata with ephy-worker.
  // Its initial commit is an input snapshot, not an implementation/release commit.
  git(gitExe, baseline, "init", "-q");
  git(gitExe, baseline, "config", "core.autocrlf", "false");
  git(gitExe, baseline, "config", "core.hooksPath", join(pack, "empty-hooks"));
  git(gitExe, baseline, "add", "--", target, "AGENTS.md", "docs/system-development-governance.md", "TASK.md");
  git(gitExe, baseline, "-c", "user.name=Ephy Input Snapshot", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "Frozen controller input fixture; not a release");
  const baseCommit = git(gitExe, baseline, "rev-parse", "HEAD").toString().trim();
  git(gitExe, baseline, "worktree", "add", "--detach", candidate, baseCommit);
  assert.equal(git(gitExe, candidate, "status", "--porcelain=v1", "--untracked-files=all").toString().trim(), "");
  const manifest = {
    definition: "controller-repair-handoff-v1", createdAt: new Date().toISOString(),
    source, sourceHead: git(gitExe, source, "rev-parse", "HEAD").toString().trim(),
    runtimeController: join(source, target), runtimeControllerSha256: hash(join(source, target)),
    baselineInput, baselineControllerSha256: sha(buffers["inputs/original-controller.ts"]),
    gitExe, gitVersion: git(gitExe, source, "--version").toString().trim(),
    nodeExe: process.execPath, nodeVersion: process.version, candidate, evidence, baseCommit, allowedFiles: [target],
    gitConfigGlobal,
    fixedFiles: Object.fromEntries([...Object.keys(files), trustName].map((name) => [name, hash(join(pack, name))])),
  };
  const manifestPath = join(pack, "manifest.json");
  write(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
  const manifestSha = hash(manifestPath);
  const command = (group) => ({ name: `frozen-${group}`, executable: process.execPath,
    args: [join(pack, "checks/verify.mjs"), "verify", manifestPath, manifestSha, group], timeoutMs: 150000,
    candidateFailureExitCodes: [10] });
  const taskText = buffers["TASK.md"].toString("utf8");
  const sections = [
    taskText.split("## 作業1：")[1].split("## 作業2：")[0],
    taskText.split("## 作業2：")[1].split("## 固定評価と提出")[0],
  ];
  const contract = {
    schemaVersion: 2, campaignId: "controller-repair-first-two", controllerSha256: manifest.runtimeControllerSha256,
    candidateRoot: candidate, evidenceRoot: evidence, baseCommit,
    goal: "Repair only verification identity and failure classification in the candidate controller. Return an unapplied proposal.",
    constraints: "Only tools/pi-local/ephy-campaign-guard.ts may change. Tests/contracts/runtime are manager-owned and immutable. Candidate code is not the running controller. No shell, network, commit, push, apply, new tasks, model changes or watchdog work. Exploratory only, not a formal independent audit. Read TASK.md for the fixed two requirements.",
    reasoningModel: "gpt-oss-20b-MXFP4", coderModel: "Qwen3-Coder-Next-Q4_K_M",
    maxDurationSeconds: 3600, maxIdleContinuations: 2, maxPlanningToolCalls: 8, maxPlanningCompactions: 1,
    maxProtocolCorrections: 2, maxReadOnlyToolCalls: 20, maxRepeatedReads: 3, maxProgressRecoveries: 2,
    taskDocument: { path: "TASK.md", sha256: sha(buffers["TASK.md"]) },
    tasks: [{ id: 1, title: "Repair the first two audited controller defects", instruction: "Implement the two frozen work items in order; preserve all unrelated behavior.",
      allowedFiles: [target], maxRepairs: 2, checks: [command("all")],
      steps: ["identity", "environment"].map((id, index) => ({ id, title: id, instruction: sections[index].trim(), allowedFiles: [target], checks: [command(id)] })) }],
    finalChecks: [command("all")],
  };
  const contractPath = join(evidence, "campaign-contract.json");
  write(contractPath, JSON.stringify(contract, null, 2) + "\n");
  const prompt = "Read TASK.md. This is the authorized exploratory controller repair proposal. Follow the controller-assigned current work item: gpt-oss plans/reviews and Qwen implements. Only edit the candidate tools/pi-local/ephy-campaign-guard.ts. Frozen checks are run externally. Do not change runtime or tests, apply anything, or add work. Submit through ephy_campaign_control and stop with the unapplied proposal.";
  write(join(pack, "PROMPT.txt"), prompt + "\n");
  const launcher = join(dirname(source), "start-dual-pi.ps1");
  write(join(pack, "start-pi.ps1"), [
    "param([switch]$PreflightOnly)",
    "$ErrorActionPreference = 'Stop'",
    "$previousHandoffPath = $env:PATH",
    "$previousHandoffGitGlobal = $env:GIT_CONFIG_GLOBAL",
    "$handoffExitCode = 2",
    "$handoffError = ''",
    "try {",
    `$env:PATH = ${ps(dirname(gitExe) + ";" + dirname(process.execPath) + ";")} + $env:PATH`,
    `if ((Get-FileHash -Algorithm SHA256 -LiteralPath ${ps(manifestPath)}).Hash.ToLowerInvariant() -ne ${ps(manifestSha)}) { throw 'Frozen manifest changed' }`,
    `$env:GIT_CONFIG_GLOBAL = ${ps(gitConfigGlobal)}`,
    `& ${ps(process.execPath)} ${ps(join(pack, "checks/verify.mjs"))} preflight ${ps(manifestPath)} ${ps(manifestSha)}`,
    "if ($LASTEXITCODE -ne 0) { throw 'Handoff preflight failed; preserve evidence and stop' }",
    "if ($PreflightOnly) { $global:LASTEXITCODE = 0; return }",
    "try { Invoke-RestMethod -Uri 'http://127.0.0.1:18080/models' -TimeoutSec 1 | Out-Null; $activeModel = $true } catch { $activeModel = $false }",
    "if ($activeModel) { throw 'A Pi/model session is already active. Close it before this attended test.' }",
    `Push-Location -LiteralPath ${ps(candidate)}`,
    "try {",
    `  & ${ps(launcher)} -AutonomousRouter -AutonomousWriteTools -GovernanceRoot ${ps(source)} -CampaignContract ${ps(contractPath)} -CampaignContractSha256 ${ps(hash(contractPath))} -InitialPrompt ${ps(prompt)}`,
    "  $handoffExitCode = if ($null -eq $LASTEXITCODE) { 2 } else { $LASTEXITCODE }",
    "} finally { Pop-Location }",
    "} catch {",
    "  $handoffError = $_.Exception.Message",
    "  $handoffExitCode = 2",
    "} finally {",
    "  $env:PATH = $previousHandoffPath",
    "  $env:GIT_CONFIG_GLOBAL = $previousHandoffGitGlobal",
    "}",
    // Outcome reporter runs outside Pi and can also describe failed preflight,
    // missing state or a process error. A non-success never returns exit zero.
    `if ((Get-FileHash -Algorithm SHA256 -LiteralPath ${ps(join(pack, "checks/outcome.mjs"))}).Hash.ToLowerInvariant() -ne ${ps(hash(join(pack, "checks/outcome.mjs")))}) { Write-Host 'Human input required: outcome reporter changed; preserve this handoff.'; exit 2 }`,
    `& ${ps(process.execPath)} ${ps(join(pack, "checks/outcome.mjs"))} ${ps(evidence)} ${ps(hash(contractPath))} $handoffExitCode $handoffError`,
    "exit $LASTEXITCODE", "",
  ].join("\r\n"));
  console.log(JSON.stringify({ pack, candidate, manifestPath, manifestSha256: manifestSha, contractPath, sourceUnchanged: true, piStarted: false }, null, 2));
}

function loadFrozen(manifestArg, expectedHash) {
  assert.ok(manifestArg && /^[a-f0-9]{64}$/.test(expectedHash ?? ""), "Frozen manifest and expected hash required");
  const manifestPath = resolve(manifestArg), pack = dirname(manifestPath);
  assert.equal(hash(manifestPath), expectedHash, "Frozen manifest changed");
  const m = JSON.parse(readFileSync(manifestPath, "utf8"));
  assert.equal(m.definition, "controller-repair-handoff-v1");
  for (const [name, expected] of Object.entries(m.fixedFiles)) assert.equal(hash(join(pack, name)), expected, `Fixed input changed: ${name}`);
  assert.equal(hash(m.runtimeController), m.runtimeControllerSha256, "Running controller source changed; start a new experiment");
  assert.equal(git(m.gitExe, m.candidate, "rev-parse", "HEAD").toString().trim(), m.baseCommit, "Candidate base changed");
  return { m, pack };
}

function snapshot(m) {
  const names = [...new Set([
    ...git(m.gitExe, m.candidate, "diff", "--name-only", "-z", "HEAD", "--").toString().split("\0"),
    ...git(m.gitExe, m.candidate, "ls-files", "--others", "--exclude-standard", "-z").toString().split("\0"),
  ].filter(Boolean))].sort();
  assert.deepEqual(names, [target], "Proposal must change only the controller");
  return { files: names, controllerSha256: hash(join(m.candidate, target)),
    patchSha256: sha(git(m.gitExe, m.candidate, "diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--")) };
}

function verify(manifestArg, expectedHash, group = "all") {
  assert.ok(["identity", "environment", "all"].includes(group));
  const { m, pack } = loadFrozen(manifestArg, expectedHash);
  const before = snapshot(m);
  const evidence = join(m.evidence, `verification-${group}-${randomUUID()}`); mkdirSync(evidence);
  write(join(evidence, "before.json"), JSON.stringify(before, null, 2));
  const run = (name, argv) => {
    const executed = spawnSync(m.nodeExe, argv, { cwd: m.candidate, encoding: "utf8", windowsHide: true,
      env: { ...process.env, PATH: `${dirname(m.gitExe)}${delimiter}${dirname(m.nodeExe)}${delimiter}${process.env.PATH ?? ""}` },
      timeout: 120000, maxBuffer: 4 * 1024 * 1024 });
    const code = executed.status, stdout = executed.stdout ?? "", stderr = executed.stderr ?? executed.error?.message ?? "";
    write(join(evidence, `${name}.stdout.log`), stdout); write(join(evidence, `${name}.stderr.log`), stderr);
    if (executed.error || executed.signal || ![0, 1].includes(code)) throw executed.error ?? new Error(`${name} execution failed: ${code}, ${executed.signal}`);
    loadFrozen(manifestArg, expectedHash);
    assert.deepEqual(snapshot(m), before, "Verifier mutated the proposal");
    return { code, stdout };
  };
  const prefix = ["--experimental-strip-types", "--experimental-loader", pathToFileURL(join(pack, "checks/loader.mjs")).href];
  const acceptance = run("acceptance", [...prefix, join(pack, "checks/acceptance.mjs"), join(m.candidate, target), m.gitExe, group]);
  const report = JSON.parse(acceptance.stdout);
  assert.equal(report.definition, "campaign-repair-acceptance-v1");
  assert.equal(report.controllerSha256, before.controllerSha256);
  assert.equal(report.passed, acceptance.code === 0);
  const smoke = run("legacy-smoke", [...prefix, join(pack, "checks/smoke.mjs"), join(m.candidate, target)]);
  git(m.gitExe, m.candidate, "diff", "--check");
  loadFrozen(manifestArg, expectedHash); assert.deepEqual(snapshot(m), before);
  const passed = report.passed && smoke.code === 0;
  write(join(evidence, "result.json"), JSON.stringify({ ...before, group, passed, acceptance: report, legacySmokeExitCode: smoke.code }, null, 2));
  console.log(JSON.stringify({ group, passed, candidate: before, checks: `${report.results.filter((r) => r.passed).length}/${report.results.length}`, legacySmokeExitCode: smoke.code, failedCases: report.results.filter((r) => !r.passed), evidence }, null, 2));
  process.exitCode = passed ? 0 : 10;
}

try {
  if (mode === "prepare") prepare(...args);
  else if (mode === "verify") verify(...args);
  else if (mode === "preflight") {
    const { m } = loadFrozen(...args);
    assert.equal(git(m.gitExe, m.candidate, "status", "--porcelain=v1", "--untracked-files=all").toString().trim(), "", "Candidate must be clean before Pi starts");
    assert.ok(!existsSync(join(m.evidence, "campaign-state.json")), "Never resume this handoff");
    console.log("PASS: fixed inputs, runtime controller, clean base, fresh evidence");
  } else throw new Error("Expected prepare, preflight, or verify");
} catch (error) {
  console.error(`VERIFICATION_INFRASTRUCTURE_FAILURE: ${error.message}`);
  process.exitCode = 2;
}
