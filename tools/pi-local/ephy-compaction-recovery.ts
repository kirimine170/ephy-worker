import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { ExtensionAPI, SessionEntry } from "@earendil-works/pi-coding-agent";
import { convertToLlm, serializeConversation } from "@earendil-works/pi-coding-agent";

const PREVIOUS_SUMMARY_CHARS = 12_000;
const TRANSCRIPT_CHARS = 24_000;
const RECOVERY_BRANCH_CHARS = 36_000;

function trimMiddle(text: string, maxChars: number): string {
	if (text.length <= maxChars) return text;

	const marker = "\n\n... [middle omitted by Ephy recovery extension] ...\n\n";
	const available = Math.max(0, maxChars - marker.length);
	const headChars = Math.floor(available * 0.4);
	const tailChars = available - headChars;
	return `${text.slice(0, headChars)}${marker}${text.slice(-tailChars)}`;
}

function entryToMessage(entry: SessionEntry): AgentMessage | undefined {
	if (entry.type === "message") return entry.message;
	if (entry.type === "compaction") {
		return {
			role: "compactionSummary",
			summary: entry.summary,
			tokensBefore: entry.tokensBefore,
			timestamp: new Date(entry.timestamp).getTime(),
		};
	}
	return undefined;
}

function serializeMessages(messages: AgentMessage[], maxChars: number): string {
	if (messages.length === 0) return "(no messages captured)";
	const serialized = serializeConversation(convertToLlm(messages));
	return trimMiddle(serialized, maxChars);
}

function formatPaths(paths: string[]): string {
	if (paths.length === 0) return "- (none recorded)";
	return paths.slice(0, 100).map((path) => `- ${path}`).join("\n");
}

function computeFileLists(fileOps: {
	read: Set<string>;
	written: Set<string>;
	edited: Set<string>;
}): { readFiles: string[]; modifiedFiles: string[] } {
	const modified = new Set([...fileOps.written, ...fileOps.edited]);
	return {
		readFiles: [...fileOps.read].filter((path) => !modified.has(path)).sort(),
		modifiedFiles: [...modified].sort(),
	};
}

export default function ephyCompactionRecovery(pi: ExtensionAPI) {
	let lastFailure: string | undefined;

	pi.on("session_before_compact", async (event, ctx) => {
		const { preparation, reason, customInstructions } = event;
		const { readFiles, modifiedFiles } = computeFileLists(preparation.fileOps);
		const discardedMessages = [
			...preparation.messagesToSummarize,
			...preparation.turnPrefixMessages,
		];

		const previousSummary = preparation.previousSummary
			? trimMiddle(preparation.previousSummary, PREVIOUS_SUMMARY_CHARS)
			: "(no previous compaction summary)";
		const transcript = serializeMessages(discardedMessages, TRANSCRIPT_CHARS);

		const summary = `# Ephy Pi deterministic recovery checkpoint

This checkpoint was generated locally without an LLM. Continue the existing task from the retained conversation and verify the repository state before editing.

## Runtime
- Working directory: ${ctx.cwd}
- Compaction reason: ${reason}
- Tokens before compaction: ${preparation.tokensBefore}
- Split turn: ${preparation.isSplitTurn}

## Continuation procedure
1. Read the retained messages after this checkpoint.
2. Run git status and inspect the relevant diff before changing files.
3. Preserve uncommitted work and do not repeat completed changes.
4. Re-run the smallest relevant verification before continuing.

## Custom compaction instructions
${customInstructions?.trim() || "(none)"}

## Previous checkpoint
${previousSummary}

## Files modified in discarded context
${formatPaths(modifiedFiles)}

## Files read in discarded context
${formatPaths(readFiles)}

## Bounded transcript excerpt
${transcript}
`;

		ctx.ui.notify(
			`Ephy recovery checkpoint created locally (${preparation.tokensBefore.toLocaleString()} tokens).`,
			"info",
		);

		return {
			compaction: {
				summary,
				firstKeptEntryId: preparation.firstKeptEntryId,
				tokensBefore: preparation.tokensBefore,
				details: { readFiles, modifiedFiles },
			},
		};
	});

	pi.on("session_compact", async () => {
		lastFailure = undefined;
	});

	pi.on("session_compact_failed", async (event, ctx) => {
		if (event.aborted) return;
		lastFailure = event.errorMessage || `Compaction failed during ${event.reason}.`;
		ctx.ui.setEditorText("/recover");
		ctx.ui.notify(
			`Compaction failed: ${lastFailure}  Press Enter to run /recover.`,
			"error",
		);
	});

	pi.registerCommand("recover", {
		description: "Start a fresh session with a bounded local recovery brief",
		handler: async (_args, ctx) => {
			await ctx.waitForIdle();

			const parentSession = ctx.sessionManager.getSessionFile();
			const branchMessages = ctx.sessionManager
				.getBranch()
				.map(entryToMessage)
				.filter((message): message is AgentMessage => message !== undefined);
			const branchExcerpt = serializeMessages(branchMessages, RECOVERY_BRANCH_CHARS);
			const failure = lastFailure || "Manual recovery requested.";
			const cwd = ctx.cwd;

			const prompt = `Continue the interrupted coding task from the recovery brief below.

Recovery cause: ${failure}
Working directory: ${cwd}

Required first steps:
1. Run git status and inspect the current diff.
2. Preserve all existing uncommitted changes.
3. Infer the active user request from the recovered transcript.
4. Continue implementation and verification without repeating completed work.

<recovered-session-excerpt>
${branchExcerpt}
</recovered-session-excerpt>`;

			const result = await ctx.newSession({
				parentSession,
				withSession: async (replacementCtx) => {
					replacementCtx.ui.notify("Recovery session started.", "info");
					await replacementCtx.sendUserMessage(prompt);
				},
			});

			if (result.cancelled) {
				ctx.ui.notify("Recovery session creation was cancelled.", "warning");
			}
		},
	});
}
