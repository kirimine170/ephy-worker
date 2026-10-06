import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";

// Creates a new disposable worktree; never resets or reuses an existing run.
const [repositoryArg, base, candidateArg, evidenceArg, pythonArg, ruffArg] = process.argv.slice(2);
if (![repositoryArg, base, candidateArg, evidenceArg, pythonArg, ruffArg].every(Boolean)) {
  throw new Error("Usage: node prepare-campaign-test.mjs REPOSITORY BASE CANDIDATE EVIDENCE PYTHON RUFF");
}
const repository = resolve(repositoryArg);
const candidate = resolve(candidateArg);
const evidence = resolve(evidenceArg);
const python = resolve(pythonArg);
const ruff = resolve(ruffArg);
if (existsSync(candidate) || existsSync(evidence)) throw new Error("Candidate and evidence must be new paths");
const controller = join(repository, "tools/pi-local/ephy-campaign-guard.ts");
const checker = join(repository, "scripts/check_report_json_decoding_v2.py");
const probe = join(repository, "scripts/report_json_decoding_probe.py");
const hash = (file) => createHash("sha256").update(readFileSync(file)).digest("hex");
for (const file of [controller, checker, probe, python, ruff]) {
  if (!existsSync(file)) throw new Error(`Required input missing: ${file}`);
}
const resolvedBase = execFileSync("git", ["-C", repository, "rev-parse", "--verify", "--end-of-options", base], { encoding: "utf8", windowsHide: true }).trim();
if (execFileSync("git", ["-C", repository, "cat-file", "-t", resolvedBase], { encoding: "utf8", windowsHide: true }).trim() !== "commit") throw new Error("Base must identify a commit");
execFileSync("git", ["-C", repository, "worktree", "add", "--detach", candidate, resolvedBase], { stdio: "inherit", windowsHide: true });
mkdirSync(evidence, { recursive: true });
const inputDirectory = join(evidence, "input-snapshots");
mkdirSync(inputDirectory);
const inputSnapshots = {};
for (const [name, source] of Object.entries({ controller, checker, probe })) {
  const snapshot = join(inputDirectory, name + (name === "controller" ? ".ts" : ".py"));
  writeFileSync(snapshot, readFileSync(source));
  inputSnapshots[name] = { path: source, snapshot, sha256: hash(source) };
}
const taskDir = join(candidate, ".ephy-worker/campaign-test");
mkdirSync(taskDir, { recursive: true });
const taskPath = join(taskDir, "TASKS.md");
const taskText = `# Long-instruction work queue: strict report JSON reading

## Goal and execution

Complete BOTH report-reading corrections in this disposable checkout. The controller expands this request into two small work items, each with its own planning, implementation, verification, and reasoning review. Follow the current work item. The complete request remains available in this document, while work-progress.json in the evidence directory records completion and pending items. A completion claim or a compaction summary does not complete a work item.

This is an explicitly authorized exploratory proposal experiment. It exercises gpt-oss planning/review and Qwen implementation inside one controlled Pi session. It does not constitute a formal independent audit. Keep the patch uncommitted for external review. The controller owns model changes and fixed verification commands. Its control tool returns the next required action. It will stop Pi after the final checks. Do not call ephy_select_model.

## Requirement A: research report

The async test test_complete_report_provenance_and_fresh_job in tests/test_research.py reads report.json after running the research executor. Make that report read explicitly select strict UTF-8 so it does not depend on the Windows process locale. The target is near line 313. Preserve all existing assertions: completed state, model-call stages, search/source counts, evidence page, saved report state, generated Markdown, model closure, unique job identity, and rejection of an existing job directory. Preserve the fake model, fake fetcher, fake collector, and all other tests.

## Requirement B: Tavily report

The async test test_full_workflow_uses_tavily_candidates_not_snippet_evidence in tests/test_tavily.py checks that the API key does not appear in report.json. Make that report read explicitly select strict UTF-8. The target is near line 396. Preserve the parametrization: one completed case and one partial-credit case. Preserve source counts, evidence page, engine and provenance assertions, snippet exclusion, request/credit metrics, and the API-key exclusion assertion. Both parameter cases must still execute.

## Shared correctness constraints

There are TWO source read sites and THREE pytest cases. Do not search for a third source location. The controller allows only the file associated with the current work item. tests/test_workflow_edges.py already uses explicit UTF-8 and is outside this task. No production source changes are needed for these two report reads.

Read valid Japanese UTF-8 without deleting or replacing characters. Invalid UTF-8 must raise a decoding error in the candidate's actual read path. Do not use errors=ignore, errors=replace, exception suppression, process-wide UTF-8 environment settings, skip/xfail, removed assertions, altered fixtures, or special-casing of probe strings. A simple encoding="utf-8" argument is sufficient. Do not perform unrelated reformatting or cleanup.

## Workflow and evidence

For each current work item, gpt-oss reads the relevant requirement and records a concrete short plan with start_implementation. Qwen reads the relevant source range, makes the edit, and submits with submit_implementation. The controller independently runs the frozen checks and captures the patch hash. gpt-oss reads that patch and check evidence before review_task. A PASS advances exactly one work item. The controller, not the model, records the resulting queue state.

After reading a range once, use edit/write if the needed change is understood. Repeating the same read without a change receives finite correction guidance. An empty submission also receives finite guidance and cannot skip the work item. If a correction is returned, act on the current assignment; it is not a successful submission. After compaction, trust recorded completion and actual files, not a remembered claim of an edit.

The first work item is checked for strict read options and preservation of target/module semantics plus its focused pytest case. The second is checked independently and then the parent checks both files. Final verification runs valid Japanese input, invalid UTF-8, and semantic-negative probes through the actual test path. The independent final patch must contain only the two intended read corrections. Passing normal pytest alone is insufficient.

## Local boundaries

Work only in this checkout. Shell and network are not model tools in this experiment. Do not install packages, alter dependencies, change the task contract, modify the controller, edit previous evidence, commit, push, open a PR, merge, or apply this proposal to another checkout. Fixed executables and evidence output are controlled by the runner. When complete, leave the uncommitted proposal and stop through the controller.
`;
writeFileSync(taskPath, taskText, "utf8");
const check = (name, executable, args, timeoutMs = 120000) => ({ name, executable, args, timeoutMs });
const targets = [
  ["research", "tests/test_research.py", "test_complete_report_provenance_and_fresh_job", 305],
  ["tavily", "tests/test_tavily.py", "test_full_workflow_uses_tavily_candidates_not_snippet_evidence", 380],
];
const baseline = join(evidence, "baseline");
mkdirSync(join(baseline, "tests"), { recursive: true });
for (const [, file] of targets) {
  writeFileSync(join(baseline, file), execFileSync("git", ["-C", repository, "show", `${resolvedBase}:${file}`], { windowsHide: true }));
}
const staticCode = `import runpy,sys,subprocess; from pathlib import Path
m=runpy.run_path(sys.argv[1]); candidate=Path.cwd(); rel=Path(sys.argv[2]); name=sys.argv[3]
original=subprocess.check_output(['git','show','HEAD:'+rel.as_posix()],cwd=candidate)
import tempfile
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/rel.name; p.write_bytes(original)
 assert m['canonical_module_without_target'](p,name)==m['canonical_module_without_target'](candidate/rel,name),'unrelated code changed'
 assert m['canonical_target'](p,name,candidate=False)==m['canonical_target'](candidate/rel,name,candidate=True),'read or test semantics changed'
print('PASS: strict UTF-8 and target/module semantics')`;
// A shared editable venv otherwise imports the manager checkout. Bind both the
// current interpreter and any Python subprocess to the candidate's source.
const pytestBootstrap = `import os,sys; from pathlib import Path
source=(Path.cwd()/'src').resolve(); sys.path.insert(0,str(source)); os.environ['PYTHONPATH']=str(source)
import ephy_worker,pytest
assert Path(ephy_worker.__file__).resolve().is_relative_to(source),'wrong source checkout'
print('Candidate source:',ephy_worker.__file__)
raise SystemExit(pytest.main(sys.argv[1:]))`;
const pytestCheck = (name, cases) => check(name, python, ["-c", pytestBootstrap, "-q", ...cases, "-p", "no:cacheprovider"]);
const both = pytestCheck("both-targets", targets.map(([, file, name]) => `${file}::${name}`));
const strict = check("strict-dynamic-checker", python, [checker, baseline, candidate, "--python", python, "--plugin", probe, "--basetemp", join(evidence, "strict-probes"), "--evidence", join(evidence, "strict-probes.json")]);
// Static preservation is independently checked against HEAD per step. The
// existing final checker also exercises valid/invalid bytes and negative data.
const contract = {
  schemaVersion: 2, campaignId: "long-instruction-pi-test", controllerSha256: hash(controller),
  candidateRoot: candidate, evidenceRoot: evidence, baseCommit: resolvedBase,
  goal: "Complete both strict report-reading requirements and return a verified uncommitted proposal.",
  constraints: "Only the current work-item file may change. Preserve all assertions and fixtures. Strict explicit UTF-8 without suppression, character loss, or process-wide locale settings. Offline, proposal-only. Two read sites cover three test cases.",
  reasoningModel: "gpt-oss-20b-MXFP4", coderModel: "Qwen3-Coder-Next-Q4_K_M",
  maxDurationSeconds: 1200, maxIdleContinuations: 2, maxPlanningToolCalls: 8, maxPlanningCompactions: 1,
  maxProtocolCorrections: 2, maxReadOnlyToolCalls: 12, maxRepeatedReads: 3, maxProgressRecoveries: 2,
  taskDocument: { path: ".ephy-worker/campaign-test/TASKS.md", sha256: hash(taskPath) },
  tasks: [{
    id: 1, title: "Strict report JSON decoding", instruction: "Implement all requirements through the controller's frozen queue, one current work item at a time.",
    allowedFiles: targets.map(([, file]) => file), maxRepairs: 1, checks: [both],
    steps: targets.map(([id, file, name, line]) => ({
      id, title: `${id} report read`, allowedFiles: [file],
      instruction: `In ${file}, function ${name}, near line ${line}, add explicit encoding="utf-8" to its report.json read_text call. Preserve everything else. Read the relevant range once and edit it; there is one source site in this work item.`,
      checks: [check(`${id}-semantics`, python, ["-c", staticCode, checker, file, name]), pytestCheck(`${id}-pytest`, [`${file}::${name}`])],
    })),
  }],
  finalChecks: [strict, check("ruff", ruff, ["check", ...targets.map(([, file]) => file)]), check("diff-check", "git", ["diff", "--check"])],
};
const contractPath = join(evidence, "campaign-contract.json");
writeFileSync(contractPath, JSON.stringify(contract, null, 2) + "\n");
writeFileSync(join(evidence, "test-inputs.json"), JSON.stringify({
  ...inputSnapshots,
  contractSha256: hash(contractPath), taskSha256: hash(taskPath),
}, null, 2) + "\n");
console.log(JSON.stringify({ candidate, evidence, contractPath, contractSha256: hash(contractPath), taskBytes: Buffer.byteLength(taskText) }));
