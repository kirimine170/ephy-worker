// Human-facing task outcome; internal failed trials remain failed and immutable.
import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const json = (path) => JSON.parse(readFileSync(path, "utf8"));

export function campaignOutcome(evidenceRoot, contractSha256, launcherExitCode, launcherError = "") {
  const report = {
    outcome: "needs_input", exitCode: 2, reason: "session_result_missing", contractSha256,
    campaignStatus: null, evidenceRoot, launcherExitCode, launcherError,
    question: "The task has not been verified complete. Inspect the saved state and launcher diagnostics, then choose manager repair, a new bounded handoff, or deferral. Do not rerun this handoff.",
  };
  try {
    const state = json(join(evidenceRoot, "campaign-state.json"));
    if (state.contractSha256 !== contractSha256) throw new Error("State belongs to a different contract");
    report.campaignStatus = state.status;
    report.reason = state.stopReason ?? "session_ended_before_completion";
    report.progress = { completedTaskIds: state.completedTaskIds, pendingTaskIds: state.pendingTaskIds, repairCount: state.repairCount };
    if (state.taskOutcome === "interrupted" && state.stopReason === "user_requested_interrupt") {
      const events = readFileSync(join(evidenceRoot, "campaign-events.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
      if (!events.some((event) => event.kind === "campaign_stopped" && event.reason === "user_requested_interrupt" && event.source === "interactive")) {
        throw new Error("User interruption evidence missing");
      }
      Object.assign(report, { outcome: "interrupted", exitCode: 130, question: null });
    } else if (!launcherError && launcherExitCode === 0 && state.status === "complete" && state.phase === "complete" && state.taskOutcome === "succeeded") {
      if (!Array.isArray(state.pendingTaskIds) || state.pendingTaskIds.length || !state.completedTaskIds?.length) throw new Error("Incomplete task queue");
      const snapshot = json(join(evidenceRoot, "combined-snapshot.json"));
      const patchSha256 = hash(readFileSync(join(evidenceRoot, "combined.patch")));
      if (patchSha256 !== snapshot.patchSha256 || patchSha256 !== state.verifiedPatchSha256) throw new Error("Final patch identity mismatch");
      if (!existsSync(join(evidenceRoot, "final-report.md"))) throw new Error("Final report missing");
      Object.assign(report, { outcome: "succeeded", exitCode: 0, reason: "proposal_complete", patchSha256, question: null });
    } else if (existsSync(join(evidenceRoot, "attention-required.json"))) {
      const attention = json(join(evidenceRoot, "attention-required.json"));
      if (attention.contractSha256 !== contractSha256) throw new Error("Attention record belongs to a different contract");
      report.question = attention.question || report.question;
      report.blocker = attention.details;
      report.checkSummary = attention.checkSummary;
    }
    if (launcherError && report.outcome === "needs_input") report.reason = "launcher_or_preflight_failed";
  } catch (error) {
    report.diagnostic = error.message;
  }
  return report;
}

export function saveOutcome(evidenceRoot, report) {
  const text = ["# Task outcome", "", `- Outcome: ${report.outcome}`, `- Reason: ${report.reason}`,
    `- Internal campaign result: ${report.campaignStatus ?? "unavailable"}`, `- Evidence: ${evidenceRoot}`,
    "- Proposal only; no apply / commit / push / merge.", "", report.question ?? "",
    "", "```json", JSON.stringify(report, null, 2), "```", ""].join("\n");
  writeFileSync(join(evidenceRoot, "task-outcome.json"), JSON.stringify(report, null, 2) + "\n");
  writeFileSync(join(evidenceRoot, "NEXT-ACTION.md"), text);
  return text;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const [evidenceRoot, contractHash, exitCode, message = ""] = process.argv.slice(2);
  try {
    if (!evidenceRoot || !/^[a-f0-9]{64}$/.test(contractHash ?? "") || !/^-?\d+$/.test(exitCode ?? "")) throw new Error("Invalid outcome arguments");
    const report = campaignOutcome(resolve(evidenceRoot), contractHash, Number(exitCode), message);
    saveOutcome(resolve(evidenceRoot), report);
    console.log(`Task outcome: ${report.outcome}; reason: ${report.reason}`);
    if (report.question) console.log(report.question);
    console.log(`Read: ${join(evidenceRoot, "NEXT-ACTION.md")}`);
    process.exitCode = report.exitCode;
  } catch (error) {
    console.error(`Human input required: unable to save task outcome: ${error.message}. Preserve ${evidenceRoot}.`);
    process.exitCode = 2;
  }
}
