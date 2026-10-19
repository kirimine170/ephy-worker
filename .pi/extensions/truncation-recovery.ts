import type { ExtensionAPI, ExtensionContext, SessionEntry } from "@earendil-works/pi-coding-agent";

const MAX_TOOL_TEXT = 10_000;
const MAX_AUTOMATIC_RETRIES = 2;
const RETAINED_CONTEXT_CHARS = 6_000;
const TRANSCRIPT_CHARS = 8_000;
const RECOVERY_BRANCH_CHARS = 24_000;
const CUSTOM_INSTRUCTIONS_CHARS = 2_000;
const MAX_SUMMARY_PATHS = 20;
const MAX_SUMMARY_PATH_CHARS = 200;
const GOVERNANCE_ACK_TOOL = "governance_ack";
const GOVERNANCE_BUNDLE_MARKER = "BEGIN REQUIRED GOVERNANCE CONTEXT BUNDLE";

function trimMiddle(text: string, maxChars: number): string {
	if (text.length <= maxChars) return text;
	const marker = "\n\n... [middle omitted by Ephy recovery extension] ...\n\n";
	const available = Math.max(0, maxChars - marker.length);
	const headChars = Math.floor(available * 0.4);
	return `${text.slice(0, headChars)}${marker}${text.slice(-(available - headChars))}`;
}

function shortenToolText(value: string): string {
	if (value.length <= MAX_TOOL_TEXT) return value;
	const head = value.slice(0, 3_000);
	const tail = value.slice(-5_000);
	return `${head}\n\n[${value.length - 8_000} characters omitted from this ordinary tool result. Re-read a targeted section if needed.]\n\n${tail}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function contentText(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((block) => {
			if (!isRecord(block)) return "";
			if (block.type === "text" && typeof block.text === "string") return block.text;
			if (block.type === "toolCall") {
				return `[tool call ${String(block.name ?? "unknown")}: ${JSON.stringify(block.arguments ?? {})}]`;
			}
			return "";
		})
		.filter(Boolean)
		.join("\n");
}

function serializeMessages(messages: unknown[], maxChars: number): string {
	const rendered = messages
		.map((message) => {
			if (!isRecord(message)) return "";
			const role = typeof message.role === "string" ? message.role : "unknown";
			const text = contentText(message.content);
			return text ? `## ${role}\n${text}` : "";
		})
		.filter(Boolean)
		.join("\n\n");
	return trimMiddle(rendered || "(no messages captured)", maxChars);
}

function entryToMessage(entry: SessionEntry): unknown | undefined {
	if (entry.type === "message") return entry.message;
	return undefined;
}

interface CheckpointDetails {
	checkpointVersion: 2;
	readFiles: string[];
	modifiedFiles: string[];
	retainedContext: string;
	role: string;
}

function stringArray(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function previousCheckpointDetails(entries: SessionEntry[]): Partial<CheckpointDetails> {
	for (let index = entries.length - 1; index >= 0; index -= 1) {
		const entry = entries[index];
		if (entry?.type !== "compaction" || !isRecord(entry.details)) continue;
		return {
			readFiles: stringArray(entry.details.readFiles),
			modifiedFiles: stringArray(entry.details.modifiedFiles),
			retainedContext: typeof entry.details.retainedContext === "string" ? entry.details.retainedContext : "",
		};
	}
	return {};
}

function mergeFileLists(
	current: { readFiles: string[]; modifiedFiles: string[] },
	previous: Partial<CheckpointDetails>,
) {
	const modifiedFiles = [...new Set([...(previous.modifiedFiles ?? []), ...current.modifiedFiles])].sort();
	const modified = new Set(modifiedFiles);
	const readFiles = [...new Set([...(previous.readFiles ?? []), ...current.readFiles])]
		.filter((path) => !modified.has(path))
		.sort();
	return { readFiles, modifiedFiles };
}

function retainedTaskContext(previous: string | undefined, messages: unknown[]): string {
	const current = messages
		.filter((message) => isRecord(message) && message.role === "user")
		.map((message) => contentText((message as Record<string, unknown>).content))
		.filter(Boolean)
		.join("\n\n");
	const parts = [previous?.trim(), current.trim()].filter(Boolean);
	return trimMiddle(parts.join("\n\n--- newer user context ---\n\n") || "(no user task context captured)", RETAINED_CONTEXT_CHARS);
}

function formatPaths(paths: string[]): string {
	if (paths.length === 0) return "- (none recorded)";
	const visible = paths.slice(0, MAX_SUMMARY_PATHS).map((path) => `- ${trimMiddle(path, MAX_SUMMARY_PATH_CHARS)}`);
	if (paths.length > visible.length) visible.push(`- (${paths.length - visible.length} additional paths retained in checkpoint details)`);
	return visible.join("\n");
}

function fileLists(fileOps: { read: Set<string>; written: Set<string>; edited: Set<string> }) {
	const modified = new Set([...fileOps.written, ...fileOps.edited]);
	return {
		readFiles: [...fileOps.read].filter((path) => !modified.has(path)).sort(),
		modifiedFiles: [...modified].sort(),
	};
}

function hasGovernanceBundle(message: Record<string, unknown>): boolean {
	if (message.toolName === GOVERNANCE_ACK_TOOL) return true;
	if (!Array.isArray(message.content)) return false;
	return message.content.some(
		(block) => isRecord(block) && block.type === "text" &&
			typeof block.text === "string" && block.text.includes(GOVERNANCE_BUNDLE_MARKER),
	);
}

function lastAssistant(ctx: ExtensionContext, allowTrailingUser = false) {
	const entries = ctx.sessionManager.getBranch();
	for (let index = entries.length - 1; index >= 0; index--) {
		const entry = entries[index];
		if (entry.type === "message" && entry.message.role === "assistant") return entry.message;
		if (!allowTrailingUser && entry.type === "message" && entry.message.role === "user") return undefined;
	}
	return undefined;
}

export default function truncationRecovery(pi: ExtensionAPI): void {
	let automaticRetries = 0;
	let recovering = false;
	let lastCompactionFailure: string | undefined;

	const compactAndRetry = (ctx: ExtensionContext, manual: boolean) => {
		if (recovering) return;
		if (!manual && automaticRetries >= MAX_AUTOMATIC_RETRIES) {
			if (ctx.hasUI) ctx.ui.notify("Truncated response: automatic retry limit reached. Use /recover after inspection.", "warning");
			console.error("Truncated response: automatic retry limit reached");
			return;
		}
		recovering = true;
		if (!manual) automaticRetries++;
		const attempt = automaticRetries;
		if (ctx.hasUI) ctx.ui.notify(`Creating a local recovery checkpoint before ${manual ? "manual" : "automatic"} retry`, "info");
		pi.appendEntry("dual-truncation-recovery", { attempt, manual, timestamp: Date.now() });
		ctx.compact({
			customInstructions:
				"Preserve the current user goal, exact modified files, verification results, background Job IDs and states, and remaining work. Do not invent completed checks.",
			onComplete: () => {
				recovering = false;
				pi.sendUserMessage(
					`Automatic continuation after an empty response was cut off by the model length limit (attempt ${attempt}). Continue from actual files and recorded Job states. Use only active governed tools and never repeat a side effect without checking its state.`,
					{ deliverAs: "followUp" },
				);
			},
			onError: (error) => {
				recovering = false;
				lastCompactionFailure = error.message;
				console.error(`Response recovery stopped: compaction failed: ${error.message}`);
			},
		});
	};

	const startRecoverySession = async (ctx: ExtensionContext) => {
		await ctx.waitForIdle();
		const parentSession = ctx.sessionManager.getSessionFile();
		const branchMessages = ctx.sessionManager
			.getBranch()
			.map(entryToMessage)
			.filter((message): message is unknown => message !== undefined);
		const branchExcerpt = serializeMessages(branchMessages, RECOVERY_BRANCH_CHARS);
		const failure = lastCompactionFailure || "Manual recovery requested.";
		const cwd = ctx.cwd;
		const result = await ctx.newSession({
			parentSession,
			withSession: async (replacementCtx) => {
				replacementCtx.ui.notify("Governed recovery session started.", "info");
				await replacementCtx.sendUserMessage(`Continue the interrupted task from this bounded local recovery brief.

Recovery cause: ${failure}
Working directory: ${cwd}
Governance role: ${process.env.DUAL_GOVERNANCE_ROLE?.trim() || "interactive"}

Required first steps:
1. Complete the fresh session's governance acknowledgement before any task action.
2. Use only the tools exposed for the current role; do not improvise unavailable shell or Integration capability.
3. Re-read identity-pinned required governance documents when continuing self-improvement planning after compaction.
4. Inspect actual files, diffs, and recorded Job states before repeating any side effect.

<recovered-session-excerpt>
${branchExcerpt}
</recovered-session-excerpt>`);
			},
		});
		if (result.cancelled) ctx.ui.notify("Recovery session creation was cancelled.", "warning");
	};

	pi.on("session_start", () => {
		automaticRetries = 0;
		recovering = false;
		lastCompactionFailure = undefined;
	});

	pi.on("context", (event) => {
		let shortened = false;
		const messages = event.messages.map((message) => {
			if (message.role !== "toolResult" || hasGovernanceBundle(message)) return message;
			const content = message.content.map((block) => {
				if (block.type !== "text" || block.text.length <= MAX_TOOL_TEXT) return block;
				shortened = true;
				return { ...block, text: shortenToolText(block.text) };
			});
			return { ...message, content };
		});
		return shortened ? { messages } : undefined;
	});

	pi.on("session_before_compact", async (event, ctx) => {
		const { preparation, reason, customInstructions, branchEntries } = event;
		const currentFiles = fileLists(preparation.fileOps);
		const previous = previousCheckpointDetails(branchEntries);
		const { readFiles, modifiedFiles } = mergeFileLists(currentFiles, previous);
		const discardedMessages = [
			...preparation.messagesToSummarize,
			...preparation.turnPrefixMessages,
		];
		const retainedContext = retainedTaskContext(previous.retainedContext, discardedMessages);
		const transcript = serializeMessages(discardedMessages, TRANSCRIPT_CHARS);
		const role = process.env.DUAL_GOVERNANCE_ROLE?.trim() || "interactive";
		const summary = `# Ephy Pi deterministic recovery checkpoint

This checkpoint was generated locally without an LLM. Continue the same task from retained context and actual external state.

## Runtime
- Working directory: ${ctx.cwd}
- Governance role: ${role}
- Compaction reason: ${reason}
- Tokens before compaction: ${preparation.tokensBefore}
- Split turn: ${preparation.isSplitTurn}

## Continuation procedure
1. Use only tools exposed for the current role and preserve uncommitted work.
2. Inspect actual files, diffs, and background Job states before repeating any side effect.
3. Re-read identity-pinned governance documents if their full content is no longer retained.
4. Re-run the smallest authorized verification before claiming completion.

## Custom compaction instructions
${customInstructions?.trim() ? trimMiddle(customInstructions.trim(), CUSTOM_INSTRUCTIONS_CHARS) : "(none)"}

## Retained task context
${retainedContext}

## Files modified so far
${formatPaths(modifiedFiles)}

## Files read so far
${formatPaths(readFiles)}

## Current bounded transcript excerpt
${transcript}
`;
		if (ctx.hasUI) ctx.ui.notify(`Ephy recovery checkpoint created locally (${preparation.tokensBefore.toLocaleString()} tokens).`, "info");
		return {
			compaction: {
				summary,
				firstKeptEntryId: preparation.firstKeptEntryId,
				tokensBefore: preparation.tokensBefore,
				details: {
					checkpointVersion: 2,
					readFiles,
					modifiedFiles,
					retainedContext,
					role,
				} satisfies CheckpointDetails,
			},
		};
	});

	pi.on("session_compact", async () => {
		lastCompactionFailure = undefined;
	});

	pi.on("session_compact_failed", async (event, ctx) => {
		if (event.aborted) return;
		lastCompactionFailure = event.errorMessage || `Compaction failed during ${event.reason}.`;
		ctx.ui.setEditorText("/recover");
		ctx.ui.notify(`Compaction failed: ${lastCompactionFailure}  Press Enter to run /recover.`, "error");
	});

	pi.on("agent_settled", (_event, ctx) => {
		if (recovering) return;
		const message = lastAssistant(ctx);
		if (!message) return;
		if (message.stopReason !== "length") {
			automaticRetries = 0;
			return;
		}
		const hasVisibleWork = message.content.some(
			(block) => block.type === "toolCall" || (block.type === "text" && block.text.trim().length > 0),
		);
		if (!hasVisibleWork) compactAndRetry(ctx, false);
	});

	pi.registerCommand("recover", {
		description: "Recover from failed compaction or retry an empty truncated response",
		handler: async (_args, ctx) => {
			if (lastCompactionFailure) {
				await startRecoverySession(ctx);
				return;
			}
			const message = lastAssistant(ctx, true);
			if (message?.stopReason !== "length") {
				ctx.ui.notify("No failed compaction or empty truncated response requires recovery.", "info");
				return;
			}
			compactAndRetry(ctx, true);
		},
	});
}
