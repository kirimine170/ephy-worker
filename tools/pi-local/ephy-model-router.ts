import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const DEFAULT_PROVIDER = "llama-local";
const DEFAULT_REASONING_MODEL = "Qwen3.8-27B-UD-Q4_K_M";
const DEFAULT_CODER_MODEL = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M";
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high"] as const;
const MAX_SWITCHES = 6;
const MIN_SWITCH_INTERVAL_MS = 2_000;

type Route = "reasoning" | "coder";
type ThinkingLevel = (typeof THINKING_LEVELS)[number];

function enabled(value: string | undefined): boolean {
	return ["1", "true", "yes"].includes(value?.trim().toLowerCase() ?? "");
}

function configured(name: string, fallback: string): string {
	const value = process.env[name]?.trim();
	return value || fallback;
}

function auditPath(cwd: string): string {
	const configuredPath = process.env.EPHY_PI_MODEL_ROUTER_AUDIT?.trim();
	if (!configuredPath) return resolve(cwd, ".ephy-worker", "model-routing.jsonl");
	return isAbsolute(configuredPath) ? configuredPath : resolve(cwd, configuredPath);
}

function writeAudit(cwd: string, entry: Record<string, unknown>): string | undefined {
	try {
		const path = auditPath(cwd);
		mkdirSync(dirname(path), { recursive: true });
		appendFileSync(path, `${JSON.stringify({ timestamp: new Date().toISOString(), ...entry })}\n`, "utf8");
		return undefined;
	} catch (error) {
		return error instanceof Error ? error.message : String(error);
	}
}

export default function ephyModelRouter(pi: ExtensionAPI) {
	// Formal managed sessions pin model identity to a governed role．The autonomous
	// router is an opt-in interactive/evaluation path and must remain absent there．
	if (!enabled(process.env.EPHY_PI_MODEL_ROUTER) || process.env.DUAL_GOVERNANCE_ROLE?.trim()) return;

	const provider = configured("EPHY_PI_MODEL_PROVIDER", DEFAULT_PROVIDER);
	const models = {
		reasoning: configured("EPHY_PI_REASONING_MODEL", DEFAULT_REASONING_MODEL),
		coder: configured("EPHY_PI_CODER_MODEL", DEFAULT_CODER_MODEL),
	} as const;
	let switchCount = 0;
	let lastSwitchAt = 0;
	let lastReason = "";

	pi.on("session_start", async (_event, ctx) => {
		switchCount = 0;
		lastSwitchAt = 0;
		lastReason = "";
		ctx.ui.setStatus("ephy-model", `model: ${ctx.model?.id ?? "unknown"}`);
	});

	pi.on("model_select", async (event, ctx) => {
		ctx.ui.setStatus("ephy-model", `model: ${event.model.id}`);
	});

	pi.registerTool({
		name: "ephy_select_model",
		label: "Select Ephy Model",
		description:
			"Switch the next inference in this Pi session between configured local reasoning and coding models. Use reasoning for substantial architecture, ambiguous diagnosis, security analysis, or evidence-heavy audit. Use coder before substantial implementation, refactoring, or test-fix loops. Do not switch merely to summarize completed work.",
		promptSnippet: "Select the configured local reasoning or coding model for the next substantial phase",
		promptGuidelines: [
			"Call ephy_select_model only when the next substantial phase is better suited to the other configured model.",
			"Use target=coder before substantial code edits and iterative test fixes; coder always uses thinking=off.",
			"Use target=reasoning for nontrivial architecture, ambiguous diagnosis, security analysis, or evidence-heavy audit.",
			"Do not switch back just to write a short completion summary, and complete useful work before switching again.",
		],
		parameters: Type.Object(
			{
				target: Type.Union([Type.Literal("reasoning"), Type.Literal("coder")]),
				thinking: Type.Optional(
					Type.Union([
						Type.Literal("off"),
						Type.Literal("minimal"),
						Type.Literal("low"),
						Type.Literal("medium"),
						Type.Literal("high"),
					]),
				),
				reason: Type.String({ minLength: 8, maxLength: 240 }),
			},
			{ additionalProperties: false },
		),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const target = params.target as Route;
			const requestedThinking = params.thinking as ThinkingLevel | undefined;
			const modelId = models[target];
			const currentId = ctx.model?.id;
			const effectiveThinking: ThinkingLevel =
				target === "coder" ? "off" : requestedThinking ?? "medium";

			if (switchCount >= MAX_SWITCHES) {
				return {
					content: [{ type: "text", text: `Model switch rejected: session limit ${MAX_SWITCHES} reached.` }],
					details: { switched: false, code: "switch_limit", switchCount },
				};
			}

			if (currentId === modelId) {
				pi.setThinkingLevel(effectiveThinking);
				lastReason = params.reason;
				const auditError = writeAudit(ctx.cwd, {
					event: "thinking_update",
					model: modelId,
					thinking: effectiveThinking,
					reason: params.reason,
					cwd: ctx.cwd,
				});
				return {
					content: [{
						type: "text",
						text: `Already using ${target}; thinking is ${effectiveThinking}. Continue the current phase.${auditError ? ` Audit warning: ${auditError}` : ""}`,
					}],
					details: { switched: false, target, modelId, thinking: effectiveThinking, auditError },
				};
			}

			const now = Date.now();
			if (lastSwitchAt && now - lastSwitchAt < MIN_SWITCH_INTERVAL_MS) {
				return {
					content: [{ type: "text", text: "Model switch rejected: complete useful work before switching again." }],
					details: { switched: false, code: "switch_cooldown", switchCount },
				};
			}

			const model = ctx.modelRegistry.find(provider, modelId);
			if (!model) {
				return {
					content: [{ type: "text", text: `Model switch failed: ${provider}/${modelId} is unavailable.` }],
					details: { switched: false, code: "model_unavailable", target, modelId },
				};
			}

			const previousModel = currentId ?? "unknown";
			const success = await pi.setModel(model);
			if (!success) {
				return {
					content: [{ type: "text", text: `Model switch failed: ${provider} is unavailable.` }],
					details: { switched: false, code: "provider_unavailable", target, modelId },
				};
			}

			pi.setThinkingLevel(effectiveThinking);
			switchCount += 1;
			lastSwitchAt = now;
			lastReason = params.reason;
			const auditError = writeAudit(ctx.cwd, {
				event: "model_switch",
				from: previousModel,
				to: modelId,
				target,
				thinking: effectiveThinking,
				reason: params.reason,
				switchCount,
				cwd: ctx.cwd,
			});
			ctx.ui.notify(`Model switched to ${modelId} (${effectiveThinking}).`, "info");

			return {
				content: [{
					type: "text",
					text: `Switched from ${previousModel} to ${modelId} with thinking=${effectiveThinking}. The selected model handles the next inference.${auditError ? ` Audit warning: ${auditError}` : ""}`,
				}],
				details: {
					switched: true,
					from: previousModel,
					to: modelId,
					target,
					thinking: effectiveThinking,
					reason: params.reason,
					switchCount,
					auditError,
				},
			};
		},
	});

	pi.registerCommand("model-route-status", {
		description: "Show autonomous model routing status for this session",
		handler: async (_args, ctx) => {
			ctx.ui.notify(
				`model=${ctx.model?.id ?? "unknown"} thinking=${pi.getThinkingLevel()} switches=${switchCount}/${MAX_SWITCHES}${lastReason ? ` last=${lastReason}` : ""}`,
				"info",
			);
		},
	});
}
