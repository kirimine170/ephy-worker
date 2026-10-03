import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { buildDetachedRunnerCommand, normalizeGitRoot } from "./git-paths.mjs";

type JobStatus =
	| "queued"
	| "preparing"
	| "running"
	| "repairing"
	| "verifying"
	| "cancelling"
	| "audit_pending"
	| "review_ready"
	| "external_review_pending"
	| "verification_failed"
	| "failed"
	| "cancelled"
	| "timed_out"
	| "applied";

interface BackgroundJob {
	schemaVersion: 1 | 2;
	id: string;
	title: string;
	status: JobStatus;
	createdAt: string;
	updatedAt: string;
	sourceCwd: string;
	repoRoot: string;
	baseRevision: string;
	dirtyAtSubmit: boolean;
	task: string;
	doneWhen: string[];
	verificationCommands: string[];
	timeoutMinutes: number;
	maxRepairAttempts?: number;
	executionProfile: "dual-local-coding";
	worktreePath: string;
	jobDir: string;
	runnerPid?: number;
	agentPid?: number;
	agentExitCode?: number;
	appliedAt?: string;
	message?: string;
	verificationResults: Array<{ command: string; exitCode: number; log: string }>;
	verificationHistory?: Array<{ attempt: number; verificationResults: Array<{ command: string; exitCode: number; log: string }>; patchSha256: string }>;
}

const ACTIVE = new Set<JobStatus>(["queued", "preparing", "running", "repairing", "verifying", "cancelling"]);
function isActive(job: BackgroundJob): boolean { return ACTIVE.has(job.status) || (job.schemaVersion === 2 && job.status === "audit_pending"); }
const TERMINAL = new Set<JobStatus>([
	"external_review_pending",
	"audit_pending",
	"review_ready",
	"verification_failed",
	"failed",
	"cancelled",
	"timed_out",
	"applied",
]);

const SubmitParams = Type.Object({
	title: Type.String({ description: "Short descriptive title for the implementation job" }),
	task: Type.String({ description: "Self-contained implementation task distilled from the discussion" }),
	doneWhen: Type.Array(Type.String(), {
		description: "Observable acceptance criteria. State what must be true before the job is complete.",
		minItems: 1,
		maxItems: 12,
	}),
	verificationCommands: Type.Optional(
		Type.Array(Type.String(), {
			description: "Optional deterministic commands the runner must execute after the agent finishes",
			maxItems: 10,
		}),
	),
	formalSpecPath: Type.Optional(Type.String({ description: "Absolute controller-frozen v2 Job spec; includes scope, immutable checks, environment/model/runtime identities and limits" })),
	formalSpecSha256: Type.Optional(Type.String({ description: "SHA-256 of the controller-frozen v2 spec" })),
	timeoutMinutes: Type.Optional(
		Type.Integer({ description: "Whole-job timeout in minutes", minimum: 5, maximum: 480, default: 60 }),
	),
	maxRepairAttempts: Type.Optional(
		Type.Integer({ description: "Automatic repair attempts after failed verification (0-3, default 2)", minimum: 0, maximum: 3, default: 2 }),
	),
});

const JobIdParams = Type.Object({
	jobId: Type.String({ description: "Background job id" }),
});

function now(): string {
	return new Date().toISOString();
}

export function selectAuditedPatch(job: BackgroundJob): string {
	const patch = path.join(job.jobDir, job.schemaVersion === 2 ? "candidate.patch" : "changes.patch");
	if (job.schemaVersion !== 2) return patch;
	const hash = (file: string) => {
		const info = fs.lstatSync(file);
		if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error("Unsafe integration artifact");
		return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
	};
	const inputFile = path.join(job.jobDir, "audit-bundle", "audit-input.json");
	const resultFile = path.join(job.jobDir, "audit-result.json");
	const input = JSON.parse(fs.readFileSync(inputFile, "utf8"));
	const result = JSON.parse(fs.readFileSync(resultFile, "utf8"));
	const attestation = JSON.parse(fs.readFileSync(path.join(job.jobDir, "audit-execution-attestation.json"), "utf8"));
	const expectedPatch = input.final_bindings?.candidate_patch_sha256;
	if (job.status !== "review_ready" || input.job_id !== job.id || result.job_id !== job.id ||
		input.baseline_commit !== job.baseRevision || result.audit_id !== input.audit_id ||
		result.decision !== "ACCEPT_PROPOSAL" || !/^[a-f0-9]{64}$/.test(expectedPatch ?? "") ||
		result.bound_inputs?.candidate_patch_sha256 !== expectedPatch || hash(patch) !== expectedPatch ||
		hash(path.join(job.jobDir, "audit-bundle", "candidate_patch.txt")) !== expectedPatch ||
		attestation.passed !== true || attestation.schema_valid !== true ||
		attestation.candidate_unchanged !== true || attestation.bundle_unchanged !== true ||
		attestation.audit_result_sha256 !== hash(resultFile) || attestation.audit_input_sha256 !== hash(inputFile) ||
		result.bound_inputs?.audit_input_sha256 !== hash(inputFile)) {
		throw new Error("Formal audited patch identity/attestation mismatch");
	}
	return patch;
}

async function verifyFormalIntegration(pi: ExtensionAPI, job: BackgroundJob): Promise<void> {
	if (job.schemaVersion !== 2) return;
	const formal = job as BackgroundJob & { runtime: { python: string; controller_source: string }; contract: { runtime_hashes: Record<string, string> } };
	const files = [formal.runtime.python, ...["formal_runtime.py", "formal_artifacts.py", "formal_campaign.py"].map(name => path.join(formal.runtime.controller_source, "ephy_worker", name))];
	for (const file of files) {
		const expected = formal.contract.runtime_hashes[file] ?? Object.entries(formal.contract.runtime_hashes).find(([key]) => path.resolve(key) === path.resolve(file))?.[1];
		if (!expected || createHash("sha256").update(fs.readFileSync(file)).digest("hex") !== expected) throw new Error("Integration verifier runtime pin mismatch");
	}
	const check = await pi.exec(formal.runtime.python, ["-c",
		"import sys; sys.path.insert(0,sys.argv.pop(1)); from ephy_worker.formal_runtime import main; main()",
		formal.runtime.controller_source, "--job", path.join(job.jobDir, "job.json"), "--verify-proposal-only"], { cwd: job.repoRoot, timeout: 30_000 });
	if (check.code !== 0) throw new Error(`Formal integration verification failed: ${check.stderr || check.stdout}`);
}

function getStateDir(): string {
	return process.env.DUAL_PI_STATE_DIR ?? path.join(process.cwd(), ".pi-dual-runtime");
}

function getJobsDir(): string {
	return path.join(getStateDir(), "jobs");
}

function atomicWriteJson(filePath: string, value: unknown): void {
	fs.mkdirSync(path.dirname(filePath), { recursive: true });
	const temporary = `${filePath}.${process.pid}.tmp`;
	fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
	try {
		fs.renameSync(temporary, filePath);
	} catch {
		// Windows cannot atomically rename over an existing destination.
		fs.copyFileSync(temporary, filePath);
		fs.unlinkSync(temporary);
	}
}

function loadJobFile(filePath: string): BackgroundJob | undefined {
	try {
		return JSON.parse(fs.readFileSync(filePath, "utf8")) as BackgroundJob;
	} catch {
		return undefined;
	}
}

function loadJobs(): BackgroundJob[] {
	const jobsDir = getJobsDir();
	if (!fs.existsSync(jobsDir)) return [];
	return fs
		.readdirSync(jobsDir, { withFileTypes: true })
		.filter((entry) => entry.isDirectory())
		.map((entry) => loadJobFile(path.join(jobsDir, entry.name, "job.json")))
		.filter((job): job is BackgroundJob => Boolean(job))
		.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

function isProcessAlive(processId: number | undefined): boolean {
	if (!processId) return false;
	try {
		process.kill(processId, 0);
		return true;
	} catch {
		return false;
	}
}

function recoverStaleJobs(): BackgroundJob[] {
	const jobs = loadJobs();
	const currentTime = Date.now();
	for (const job of jobs) {
		if (!isActive(job)) continue;
		const ageMs = currentTime - Date.parse(job.updatedAt);
		const staleWithoutPid = !job.runnerPid && ageMs > 60_000;
		const staleDeadRunner = Boolean(job.runnerPid) && !isProcessAlive(job.runnerPid) && ageMs > 15_000;
		if (staleWithoutPid || staleDeadRunner) {
			job.status = "failed";
			job.updatedAt = now();
			job.message = "Recovered stale job: background runner is no longer active";
			atomicWriteJson(path.join(job.jobDir, "job.json"), job);
		}
	}
	return loadJobs();
}

function summarize(job: BackgroundJob): string {
	const suffix = job.message ? ` — ${job.message}` : "";
	return `${job.id} [${job.status}] ${job.title}${suffix}`;
}

function ageLabel(milliseconds: number): string {
	const seconds = Math.max(0, Math.floor(milliseconds / 1000));
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m`;
	return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function progressLines(jobs: BackgroundJob[]): string[] {
	const job = jobs.find((candidate) => isActive(candidate)) ?? jobs[0];
	if (!job) return ["BG: no jobs. Use /bg to start one."];
	const currentTime = Date.now();
	let lastActivity = Date.parse(job.updatedAt);
	try {
		lastActivity = Math.max(lastActivity, fs.statSync(path.join(job.jobDir, "agent.jsonl")).mtimeMs);
		for (const name of fs.readdirSync(job.jobDir).filter((file) => file.startsWith("agent-repair-") && file.endsWith(".jsonl"))) {
			lastActivity = Math.max(lastActivity, fs.statSync(path.join(job.jobDir, name)).mtimeMs);
		}
	} catch {
		// The agent log does not exist until the runner starts.
	}
	const elapsedUntil = TERMINAL.has(job.status) ? Date.parse(job.updatedAt) : currentTime;
	const elapsed = ageLabel(elapsedUntil - Date.parse(job.createdAt));
	const activity = ageLabel(currentTime - lastActivity);
	return [
		`BG ${job.id} [${job.status}] ${job.title}`,
		TERMINAL.has(job.status)
			? `Stopped after ${elapsed} · ${job.message ?? "Review the job logs"} · /bg-status ${job.id}`
			: `${job.message ?? "Working"} · ${elapsed} elapsed · activity ${activity} ago · /bg-status ${job.id}`,
	];
}

function makeJobId(): string {
	const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
	return `bg-${stamp}-${randomUUID().slice(0, 6)}`;
}

async function startDetachedRunner(
	pi: ExtensionAPI,
	shell: string,
	runner: string,
	jobFile: string,
): Promise<{ code: number; pid?: number; stdout: string; stderr: string }> {
	const command = buildDetachedRunnerCommand(shell, runner, jobFile);
	const encoded = Buffer.from(command, "utf16le").toString("base64");
	const result = await pi.exec(shell, ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], { timeout: 10_000 });
	const pid = Number.parseInt(result.stdout.trim(), 10);
	return {
		code: result.code,
		pid: Number.isSafeInteger(pid) && pid > 0 ? pid : undefined,
		stdout: result.stdout,
		stderr: result.stderr,
	};
}

function renderTask(job: BackgroundJob): string {
	const acceptance = job.doneWhen.map((item) => `- ${item}`).join("\n");
	const commands =
		job.verificationCommands.length > 0
			? job.verificationCommands.map((command) => `- \`${command}\``).join("\n")
			: "- No external verification command was supplied. The runner will perform only its fixed diff check; worker self-checks are not independent verification.";

	return `# Background implementation job ${job.id}

## Goal

${job.task}

## Acceptance criteria

${acceptance}

## Required verification

${commands}

## Execution rules

- You are the lead for this isolated background job.
- You MUST delegate the implementation to the \`qwen-worker\` subagent.
- After it returns, review the worker report for scope or contract conflicts，but do not edit files or run acceptance commands yourself.
- The runner performs the independent checks after this lead process exits．If those checks show a candidate-origin failure，a fresh bounded repair pass will provide the evidence and require another Qwen delegation.
- The runner may request at most ${job.maxRepairAttempts ?? 2} additional correction attempts after its own checks. Keep the original goal, scope, and verification commands fixed; report any conflict instead of widening them.
- Work only in the current worktree. Do not touch another checkout.
- Do not submit another background job, commit, push, merge, deploy, or delete user work.
- Finish with a concise report of the delegated outcome，worker-reported checks，and remaining risks．Do not present worker self-checks as independent verification.
`;
}

export default function (pi: ExtensionAPI): void {
	if (process.env.DUAL_BACKGROUND_CHILD === "1") return;
	let uiMode: "on" | "compact" | "off" = "compact";
	let refreshDisplay: (() => void) | undefined;

	pi.registerTool({
		name: "background_job_submit",
		label: "Submit background implementation",
		description:
			"Submit an isolated dual-model implementation job only after the user explicitly asks to implement in the background. The user must confirm before it starts.",
		parameters: SubmitParams,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			let formalSpec: Record<string, unknown> | undefined;
			if (params.formalSpecPath || params.formalSpecSha256) {
				if (!params.formalSpecPath || !path.isAbsolute(params.formalSpecPath) || !params.formalSpecSha256) {
					throw new Error("Formal submission requires absolute spec path and SHA-256");
				}
				const { createHash } = await import("node:crypto");
				const bytes = fs.readFileSync(params.formalSpecPath);
				if (createHash("sha256").update(bytes).digest("hex") !== params.formalSpecSha256) {
					throw new Error("Frozen formal spec SHA-256 mismatch");
				}
				formalSpec = JSON.parse(bytes.toString("utf8"));
				const required = ["repoRoot", "baseRevision", "contract", "runtime", "controls", "model_identities", "verifier_identity"];
				if (!formalSpec || Object.keys(formalSpec).sort().join() !== required.sort().join()) {
					throw new Error("Incomplete or unexpected formal spec fields");
				}
			}
			const runner = process.env.DUAL_JOB_RUNNER;
			const shell = process.env.DUAL_POWERSHELL_EXE ?? "powershell.exe";
			if (!runner || !fs.existsSync(runner)) {
				return { content: [{ type: "text", text: "Background runner is not configured." }], details: {} };
			}

			const active = recoverStaleJobs().filter((job) => isActive(job));
			if (active.length > 0) {
				return {
					content: [{ type: "text", text: `A background job is already active:\n${active.map(summarize).join("\n")}` }],
					details: { active },
				};
			}

			const repoResult = await pi.exec("git", ["rev-parse", "--show-toplevel"], { cwd: ctx.cwd, timeout: 10_000 });
			if (repoResult.code !== 0) {
				return {
					content: [{ type: "text", text: "Background jobs require a Git repository so work can be isolated in a worktree." }],
					details: { stderr: repoResult.stderr },
				};
			}
			const rawRepoRoot = repoResult.stdout.trim();
			const repoRoot = normalizeGitRoot(rawRepoRoot);
			if (!repoRoot || !path.isAbsolute(repoRoot)) {
				return {
					content: [{ type: "text", text: "Git returned a repository root that is not an absolute native path." }],
					details: { rawRepoRoot, repoRoot },
				};
			}
			const headResult = await pi.exec("git", ["rev-parse", "HEAD"], { cwd: repoRoot, timeout: 10_000 });
			if (headResult.code !== 0) {
				return {
					content: [{ type: "text", text: "The repository has no resolvable HEAD commit." }],
					details: { rawRepoRoot, repoRoot, stderr: headResult.stderr, stdout: headResult.stdout },
				};
			}
			const statusResult = await pi.exec("git", ["status", "--porcelain"], { cwd: repoRoot, timeout: 10_000 });
			const dirty = statusResult.stdout.trim().length > 0;
			const timeoutMinutes = params.timeoutMinutes ?? 60;
			const maxRepairAttempts = params.maxRepairAttempts ?? 2;
			const commands = (params.verificationCommands ?? []).map((command) => command.trim()).filter(Boolean);
			if (formalSpec && (formalSpec.repoRoot !== repoRoot || formalSpec.baseRevision !== headResult.stdout.trim())) {
				throw new Error("Formal spec repository/base differs from submission context");
			}
			const confirmation = formalSpec ? [
				`Formal spec SHA-256: ${params.formalSpecSha256}`,
				`Repository: ${repoRoot}`,
				`Base: ${formalSpec.baseRevision}`,
				`Task: ${formalSpec.contract.task}`,
				`Semantic scope: ${formalSpec.contract.semantic_scope}`,
				`Allowed files: ${JSON.stringify(formalSpec.contract.allowed_files)}`,
				`Whole-job timeout: ${formalSpec.contract.timeout_seconds} seconds`,
				`Stage timeout: ${formalSpec.contract.stage_seconds} seconds`,
				`Repair limit: ${formalSpec.contract.max_repairs}`,
				`Output tokens per stage: ${formalSpec.contract.output_token_budget}`,
				`Models: ${JSON.stringify(formalSpec.model_identities)}`,
				`Frozen checks: ${JSON.stringify(formalSpec.contract.checks)}`,
				"Fresh isolated proposal only. No automatic apply, commit, push or merge.",
			].join("\n") : [
				`Title: ${params.title}`,
				`Repository: ${repoRoot}`,
				`Base: ${headResult.stdout.trim().slice(0, 12)}`,
				`Timeout: ${timeoutMinutes} minutes`,
				`Automatic repair attempts: ${maxRepairAttempts} (bounded, same isolated worktree)`,
				commands.length > 0 ? `Verification: ${commands.join(" ; ")}` : "Verification: agent checks + git diff --check",
				dirty ? "WARNING: uncommitted changes in the current checkout are NOT included." : "Current checkout is clean.",
				"The job will use an isolated worktree and will not merge or push automatically.",
			].join("\n");

			if (!ctx.hasUI) {
				return {
					content: [{ type: "text", text: "Background submission requires an interactive confirmation." }],
					details: {},
				};
			}
			const confirmed = await ctx.ui.confirm("Start background implementation?", confirmation);
			if (!confirmed) {
				return { content: [{ type: "text", text: "Background job submission cancelled by the user." }], details: {} };
			}

			const id = makeJobId();
			if (formalSpec && (formalSpec.repoRoot !== repoRoot || formalSpec.baseRevision !== headResult.stdout.trim())) {
				throw new Error("Formal spec repository/base differs from submission context");
			}
			const jobDir = path.join(getJobsDir(), id);
			const worktreePath = path.join(getStateDir(), "worktrees", id);
			const job: BackgroundJob = {
				schemaVersion: 1,
				id,
				title: params.title.trim(),
				status: "queued",
				createdAt: now(),
				updatedAt: now(),
				sourceCwd: ctx.cwd,
				repoRoot,
				baseRevision: headResult.stdout.trim(),
				dirtyAtSubmit: dirty,
				task: params.task.trim(),
				doneWhen: params.doneWhen.map((item) => item.trim()).filter(Boolean),
				verificationCommands: commands,
				timeoutMinutes,
				maxRepairAttempts,
				executionProfile: "dual-local-coding",
				worktreePath,
				jobDir,
				verificationResults: [],
			};
			if (formalSpec) {
				Object.assign(job, formalSpec, { schemaVersion: 2, humanAuthorization: "explicit-execute-proposal-only" });
				job.task = formalSpec.contract.task;
				job.timeoutMinutes = formalSpec.contract.timeout_seconds / 60;
				job.maxRepairAttempts = formalSpec.contract.max_repairs;
			}
			fs.mkdirSync(jobDir, { recursive: true });
			fs.writeFileSync(path.join(jobDir, "TASK.md"), renderTask(job), "utf8");
			const jobFile = path.join(jobDir, "job.json");
			atomicWriteJson(jobFile, job);

			const launch = await startDetachedRunner(pi, shell, runner, jobFile);
			if (launch.code !== 0 || !launch.pid) {
				job.status = "failed";
				job.updatedAt = now();
				job.message = `Background runner failed to launch: ${launch.stderr || launch.stdout || `exit ${launch.code}`}`;
				atomicWriteJson(jobFile, job);
				return {
					content: [{ type: "text", text: job.message }],
					details: { job, launch },
				};
			}
			const currentJob = loadJobFile(jobFile) ?? job;
			if (currentJob.status === "queued") {
				currentJob.runnerPid = launch.pid;
				currentJob.updatedAt = now();
				currentJob.message = "Background runner process launched";
				atomicWriteJson(jobFile, currentJob);
			}
			if (uiMode !== "off") ctx.ui.notify(`Background job ${id} started`, "info");

			return {
				content: [
					{
						type: "text",
						text: `Background job submitted: ${id}\nWorktree: ${worktreePath}\nUse background_job_status or /bg-status to inspect it.`,
					},
				],
				details: loadJobFile(jobFile) ?? currentJob,
			};
		},
	});

	pi.registerTool({
		name: "background_job_status",
		label: "Background job status",
		description: "Read background implementation job status and verification results.",
		parameters: Type.Object({ jobId: Type.Optional(Type.String({ description: "Optional job id; omit for recent jobs" })) }),
		async execute(_toolCallId, params) {
			const jobs = loadJobs();
			const selected = params.jobId ? jobs.filter((job) => job.id === params.jobId) : jobs.slice(0, 10);
			return {
				content: [{ type: "text", text: selected.length > 0 ? selected.map(summarize).join("\n") : "No matching background jobs." }],
				details: { jobs: selected },
			};
		},
	});

	pi.registerTool({
		name: "background_job_cancel",
		label: "Cancel background job",
		description: "Request cancellation of an active background implementation job. Requires user confirmation.",
		parameters: JobIdParams,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const job = loadJobs().find((candidate) => candidate.id === params.jobId);
			if (!job) return { content: [{ type: "text", text: `Unknown job: ${params.jobId}` }], details: {} };
			if (!isActive(job)) {
				return { content: [{ type: "text", text: `Job ${job.id} is already ${job.status}.` }], details: job };
			}
			if (!ctx.hasUI || !(await ctx.ui.confirm("Cancel background job?", summarize(job)))) {
				return { content: [{ type: "text", text: "Cancellation was not confirmed." }], details: job };
			}
			fs.writeFileSync(path.join(job.jobDir, "cancel.request"), `${now()}\n`, "utf8");
			job.status = "cancelling";
			job.updatedAt = now();
			job.message = "Cancellation requested";
			atomicWriteJson(path.join(job.jobDir, "job.json"), job);
			return { content: [{ type: "text", text: `Cancellation requested for ${job.id}.` }], details: job };
		},
	});

	pi.registerTool({
		name: "background_job_apply",
		label: "Apply background job",
		description:
			"Apply a review-ready background job patch to its original checkout without committing, merging, pushing, or deploying. Requires a clean unchanged base and user confirmation.",
		parameters: JobIdParams,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const job = recoverStaleJobs().find((candidate) => candidate.id === params.jobId);
			if (!job) return { content: [{ type: "text", text: `Unknown job: ${params.jobId}` }], details: {} };
			if (job.status !== "review_ready") {
				return { content: [{ type: "text", text: `Job ${job.id} is ${job.status}, not review_ready.` }], details: job };
			}
			const patchFile = selectAuditedPatch(job);
			await verifyFormalIntegration(pi, job);
			if (!fs.existsSync(patchFile) || fs.statSync(patchFile).size === 0) {
				return { content: [{ type: "text", text: `Job ${job.id} has no patch to apply.` }], details: job };
			}
			const head = await pi.exec("git", ["rev-parse", "HEAD"], { cwd: job.repoRoot, timeout: 10_000 });
			const status = await pi.exec(
				"git",
				["--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"],
				{ cwd: job.repoRoot, timeout: 10_000 },
			);
			if (head.code !== 0 || head.stdout.trim() !== job.baseRevision) {
				return {
					content: [{ type: "text", text: "Original checkout HEAD changed after submission. Review and integrate the worktree manually." }],
					details: job,
				};
			}
			if (status.code !== 0 || status.stdout.trim().length > 0) {
				return {
					content: [{ type: "text", text: "Original checkout is not clean. Commit or stash its changes before applying the background patch." }],
					details: job,
				};
			}
			const check = await pi.exec("git", ["apply", "--check", patchFile], { cwd: job.repoRoot, timeout: 30_000 });
			if (check.code !== 0) {
				return {
					content: [{ type: "text", text: `Patch no longer applies cleanly:\n${check.stderr || check.stdout}` }],
					details: job,
				};
			}
			if (!ctx.hasUI || !(await ctx.ui.confirm("Apply background changes?", `${summarize(job)}\n\nTarget: ${job.repoRoot}\nNo commit, merge, push, or deploy will be performed.`))) {
				return { content: [{ type: "text", text: "Patch application was not confirmed." }], details: job };
			}
			// The checkout may have changed while the confirmation dialog was open.
			// Revalidate the base, cleanliness, and patch immediately before applying.
			const finalHead = await pi.exec("git", ["rev-parse", "HEAD"], { cwd: job.repoRoot, timeout: 10_000 });
			const finalStatus = await pi.exec(
				"git",
				["--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"],
				{ cwd: job.repoRoot, timeout: 10_000 },
			);
			if (
				finalHead.code !== 0 ||
				finalHead.stdout.trim() !== job.baseRevision ||
				finalStatus.code !== 0 ||
				finalStatus.stdout.trim().length > 0
			) {
				return {
					content: [{ type: "text", text: "Original checkout changed during confirmation. Nothing was applied." }],
					details: job,
				};
			}
			const finalCheck = await pi.exec("git", ["apply", "--check", patchFile], { cwd: job.repoRoot, timeout: 30_000 });
			if (finalCheck.code !== 0) {
				return {
					content: [{ type: "text", text: `Patch stopped applying cleanly during confirmation:\n${finalCheck.stderr || finalCheck.stdout}` }],
					details: job,
				};
			}
			selectAuditedPatch(job); // Recheck bound bytes after the confirmation and final git check.
			await verifyFormalIntegration(pi, job);
			const applied = await pi.exec("git", ["apply", patchFile], { cwd: job.repoRoot, timeout: 30_000 });
			if (applied.code !== 0) {
				return { content: [{ type: "text", text: `Patch application failed:\n${applied.stderr || applied.stdout}` }], details: job };
			}
			job.status = "applied";
			job.appliedAt = now();
			job.updatedAt = now();
			job.message = "Patch applied to original checkout; changes remain uncommitted";
			atomicWriteJson(path.join(job.jobDir, "job.json"), job);
			const appliedStatus = await pi.exec(
				"git",
				["--no-optional-locks", "status", "--short", "--untracked-files=all"],
				{ cwd: job.repoRoot, timeout: 10_000 },
			);
			const statusSummary =
				appliedStatus.code === 0
					? appliedStatus.stdout.trim() || "(clean; patch contained no effective changes)"
					: `(status unavailable: ${appliedStatus.stderr || appliedStatus.stdout})`;
			return {
				content: [
					{
						type: "text",
						text: `Applied ${job.id} to ${job.repoRoot}. Changes are uncommitted; inspect and test them before committing.\n\nCurrent status:\n${statusSummary}`,
					},
				],
				details: job,
			};
		},
	});

	pi.registerCommand("bg", {
		description: "Create a confirmed background implementation job from the current discussion",
		handler: async (args) => {
			const extra = args.trim() ? ` Additional instruction: ${args.trim()}` : "";
			pi.sendUserMessage(
				`I explicitly want the implementation to run as a background job. Distill our agreed goal, constraints, acceptance criteria, and deterministic verification commands, then call background_job_submit.${extra}`,
			);
		},
	});

	pi.registerCommand("bg-status", {
		description: "Show recent background jobs or one job id",
		handler: async (args, ctx) => {
			const id = args.trim();
			const jobs = loadJobs();
			const selected = id ? jobs.filter((job) => job.id === id) : jobs.slice(0, 10);
			pi.sendMessage({
				customType: "background-job-status",
				content: selected.length > 0 ? selected.map(summarize).join("\n") : "No matching background jobs.",
				display: true,
				details: { jobs: selected },
			});
			ctx.ui.notify(`${selected.length} background job(s) shown`, "info");
		},
	});

	pi.registerCommand("bg-ui", {
		description: "Show or hide automatic background job progress: /bg-ui on|compact|off",
		handler: async (args, ctx) => {
			const requested = args.trim().toLowerCase();
			if (requested !== "on" && requested !== "compact" && requested !== "off") {
				ctx.ui.notify(`Background UI: ${uiMode}. Use /bg-ui on|compact|off.`, "info");
				return;
			}
			uiMode = requested;
			refreshDisplay?.();
			ctx.ui.notify(`Background UI: ${uiMode}`, "info");
		},
	});

	pi.registerCommand("bg-apply", {
		description: "Explain or request separately authorized integration of a review-ready patch",
		handler: async (args, ctx) => {
			const id = args.trim();
			if (!id) {
				ctx.ui.notify("Usage: /bg-apply <job-id>", "warning");
				return;
			}
			if (process.env.DUAL_GOVERNANCE_ROLE !== "integration") {
				ctx.ui.notify(
					"Patch application is unavailable to the proposal-only lead. Start a separately authorized integration session after audit.",
					"warning",
				);
				return;
			}
			pi.sendUserMessage(`Apply background job ${id} with background_job_apply after rechecking the explicit user authorization. Do not integrate it by any other method.`);
		},
	});

	pi.registerCommand("bg-cancel", {
		description: "Cancel an active background job after confirmation",
		handler: async (args, ctx) => {
			const id = args.trim();
			if (!id) {
				ctx.ui.notify("Usage: /bg-cancel <job-id>", "warning");
				return;
			}
			pi.sendUserMessage(`Cancel background job ${id} with background_job_cancel.`);
		},
	});

	let pollTimer: ReturnType<typeof setInterval> | undefined;
	pi.on("session_start", (_event, ctx) => {
		fs.mkdirSync(getJobsDir(), { recursive: true });
		const seen = new Map(recoverStaleJobs().map((job) => [job.id, job.status]));
		const refresh = () => {
			const jobs = recoverStaleJobs();
			const activeCount = jobs.filter((job) => isActive(job)).length;
			ctx.ui.setStatus("background-jobs", uiMode !== "off" && activeCount > 0 ? `BG:${activeCount}` : undefined);
			ctx.ui.setWidget("background-job-progress", uiMode === "on" ? progressLines(jobs) : undefined);
			for (const job of jobs) {
				const previous = seen.get(job.id);
				seen.set(job.id, job.status);
				if (uiMode !== "off" && previous && previous !== job.status && TERMINAL.has(job.status)) {
					ctx.ui.notify(summarize(job), job.status === "review_ready" ? "info" : "warning");
					pi.sendMessage(
						{
							customType: "background-job-complete",
							content: `Background job finished: ${summarize(job)}\nWorktree: ${job.worktreePath}\nJob record: ${path.join(job.jobDir, "job.json")}`,
							display: true,
							details: job,
						},
						{ deliverAs: "nextTurn" },
					);
				}
			}
		};
		refreshDisplay = refresh;
		refresh();
		pollTimer = setInterval(refresh, 2_000);
	});

	pi.on("session_shutdown", () => {
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = undefined;
		refreshDisplay = undefined;
	});
}
