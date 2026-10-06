import { createHash, randomUUID } from "node:crypto";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const POLICY_ID = "ephy.system-development-governance.v1";
const END_MARKER = "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1";
const ACK_TOOL = "governance_ack";
const FAILURE_PREFIX = "GOVERNANCE_GATE_FAILURE";
const MAX_CONTEXT_BUNDLE_BYTES = 50 * 1024;
const MAX_CONTEXT_BUNDLE_LINES = 2000;
const READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls"]);
const PRE_ACK_TOOLS = new Set<string>();
const LEAD_CONTEXT_PATHS = [
	"docs/self-improvement-mvp.md",
	".agents/skills/ephy-worker-self-improvement/SKILL.md",
	".agents/skills/ephy-worker-self-improvement/references/eval-contract.md",
	".agents/skills/ephy-worker-self-improvement/references/audit-contract.md",
	".pi/prompts/audit-ephy-worker.md",
] as const;
const AUDITOR_CONTEXT_PATHS = [
	"policies/independent-audit.md",
	"prompts/audit-ephy-worker.md",
	"policies/audit-input.schema.json",
	"policies/evidence-manifest.schema.json",
	"policies/audit-result.schema.json",
] as const;
const AUDITOR_TOOLS = new Set([...READ_ONLY_TOOLS]);
const LEAD_TOOLS = new Set([
	...READ_ONLY_TOOLS,
	"subagent",
	"background_job_submit",
	"background_job_status",
	"background_job_cancel",
]);
const IMPLEMENTER_TOOLS = new Set([
	...READ_ONLY_TOOLS,
	"bash",
	"powershell",
	"edit",
	"write",
]);
const INTEGRATION_TOOLS = new Set([
	...READ_ONLY_TOOLS,
	"background_job_status",
	"background_job_apply",
]);

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNamedToolDefinition(value: unknown, name: string): boolean {
	if (!isRecord(value) || value.type !== "function" || !isRecord(value.function)) return false;
	return value.function.name === name;
}

type RequiredContextDocument = {
	relativePath: string;
	absolutePath: string;
	sha256: string;
	byteLength: number;
	lineCount: number;
};

type PendingContextBundle = {
	toolCallId: string;
	serializedContent: string;
};

type RuntimeArtifact = {
	name: string;
	absolutePath: string;
	sha256: string;
	byteLength: number;
};

function sha256(bytes: Uint8Array): string {
	return createHash("sha256").update(bytes).digest("hex");
}

function normalizeWorkspaceRelativePath(value: unknown, workspaceRoot: string): string | undefined {
	if (typeof value !== "string" || value.trim().length === 0) return undefined;
	const absolutePath = resolve(workspaceRoot, value);
	const relativePath = relative(workspaceRoot, absolutePath).replaceAll("\\", "/");
	if (relativePath === "" || relativePath === ".." || relativePath.startsWith("../") || isAbsolute(relativePath)) {
		return undefined;
	}
	return relativePath.toLowerCase();
}

function isPathWithinRoot(root: string, value: string): boolean {
	const relativePath = relative(root, value);
	return !(
		relativePath === ".." ||
		relativePath.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) ||
		isAbsolute(relativePath)
	);
}

function isExactAcknowledgementInput(value: unknown, policySha256: string, nonce: string, role: string): boolean {
	return (
		isRecord(value) &&
		Object.keys(value).length === 5 &&
		value.policyId === POLICY_ID &&
		value.policySha256 === policySha256 &&
		value.endMarker === END_MARKER &&
		value.nonce === nonce &&
		value.role === role
	);
}

function getValidAckToolCall(
	message: unknown,
	policySha256: string,
	nonce: string,
	role: string,
): Record<string, unknown> | undefined {
	if (!isRecord(message) || message.role !== "assistant" || !Array.isArray(message.content)) return undefined;
	let acknowledgementCall: Record<string, unknown> | undefined;

	for (const item of message.content) {
		if (!isRecord(item)) return undefined;
		if (item.type !== "toolCall") continue;
		if (
			item.name !== ACK_TOOL ||
			typeof item.id !== "string" ||
			item.id.length === 0 ||
			acknowledgementCall ||
			!isExactAcknowledgementInput(item.arguments, policySha256, nonce, role)
		) return undefined;
		acknowledgementCall = item;
	}

	return acknowledgementCall;
}

function governanceFailureMessage(message: Record<string, unknown>, reason: string): Record<string, unknown> {
	return {
		...message,
		content: [
			{
				type: "text",
				text: `${FAILURE_PREFIX}: ${reason}. This session is permanently latched; start a fresh session.`,
			},
		],
		stopReason: "stop",
	};
}

function toolCeiling(role: string): Set<string> | undefined {
	if (role === "auditor") return AUDITOR_TOOLS;
	if (role === "lead" || role === "planner") return LEAD_TOOLS;
	if (role === "implementer") return IMPLEMENTER_TOOLS;
	if (role === "integration") return INTEGRATION_TOOLS;
	return undefined;
}

export default function governanceGate(pi: ExtensionAPI) {
	let acknowledged = false;
	let violated = false;
	let pendingAckCallId: string | undefined;
	let pendingContextBundle: PendingContextBundle | undefined;
	let availableTools: string[] | undefined;
	let contextComplete = true;
	let nonce = randomUUID();
	const role = process.env.DUAL_GOVERNANCE_ROLE?.trim() ?? "";
	const workspaceRoot = resolve(process.cwd());
	const managedRuntimeRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
	const configuredGovernanceContextRoot = process.env.DUAL_GOVERNANCE_CONTEXT_ROOT?.trim();
	const policyPath =
		process.env.DUAL_GOVERNANCE_POLICY ??
		fileURLToPath(new URL("../policies/development-governance.md", import.meta.url));

	let policyText = "";
	let policySha256 = "";
	let requiredContextDocuments: RequiredContextDocument[] = [];
	let runtimeArtifacts: RuntimeArtifact[] = [];
	let auditBundleRoot: string | undefined;
	let governanceContextRoot = workspaceRoot;
	let loadError: string | undefined;

	try {
		if (!toolCeiling(role)) {
			throw new Error(
				`Missing or unsupported DUAL_GOVERNANCE_ROLE: ${role || "<empty>"}`,
			);
		}
		if (configuredGovernanceContextRoot) {
			if (!isAbsolute(configuredGovernanceContextRoot)) {
				throw new Error("DUAL_GOVERNANCE_CONTEXT_ROOT must be an absolute path");
			}
			governanceContextRoot = realpathSync(resolve(configuredGovernanceContextRoot));
			if (!statSync(governanceContextRoot).isDirectory()) {
				throw new Error("DUAL_GOVERNANCE_CONTEXT_ROOT must identify an existing directory");
			}
		}
		const bytes = readFileSync(policyPath);
		policyText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
		policySha256 = sha256(bytes);
		if (!policyText.includes(POLICY_ID)) {
			throw new Error(`Policy ID is missing: ${POLICY_ID}`);
		}
		if (!policyText.trimEnd().endsWith(END_MARKER)) {
			throw new Error(`Policy end marker is missing: ${END_MARKER}`);
		}
		if (policyText.includes("</development_governance>")) {
			throw new Error("Policy contains the reserved system-section closing tag");
		}

		if (role === "lead" || role === "planner") {
			requiredContextDocuments = LEAD_CONTEXT_PATHS.map((relativePath) => {
				const absolutePath = realpathSync(resolve(governanceContextRoot, relativePath));
				const normalized = normalizeWorkspaceRelativePath(absolutePath, governanceContextRoot);
				if (normalized !== relativePath.toLowerCase()) {
					throw new Error(`Required context path escapes DUAL_GOVERNANCE_CONTEXT_ROOT: ${relativePath}`);
				}
				const documentBytes = readFileSync(absolutePath);
				const documentText = new TextDecoder("utf-8", { fatal: true }).decode(documentBytes);
				return {
					relativePath,
					absolutePath,
					sha256: sha256(documentBytes),
					byteLength: documentBytes.byteLength,
					lineCount: documentText.split(/\r?\n/).length,
				};
			});
			contextComplete = requiredContextDocuments.length === 0;

			const configuredRunner = process.env.DUAL_JOB_RUNNER?.trim();
			if (configuredRunner) {
				const runnerPath = resolve(configuredRunner);
				const configuredSmoke = process.env.DUAL_RUNNER_SMOKE_TEST?.trim();
				const smokePath = configuredSmoke
					? resolve(configuredSmoke)
					: resolve(dirname(runnerPath), "test-background-job-runner.ps1");
				runtimeArtifacts = [
					["background_job_runner", runnerPath],
					["background_job_runner_smoke_test", smokePath],
				].map(([name, absolutePath]) => {
					const artifactBytes = readFileSync(absolutePath);
					return {
						name,
						absolutePath,
						sha256: sha256(artifactBytes),
						byteLength: artifactBytes.byteLength,
					};
				});
			}
		} else if (role === "auditor") {
			requiredContextDocuments = AUDITOR_CONTEXT_PATHS.map((relativePath) => {
				const absolutePath = resolve(managedRuntimeRoot, relativePath);
				const normalized = normalizeWorkspaceRelativePath(absolutePath, managedRuntimeRoot);
				if (normalized !== relativePath.toLowerCase()) {
					throw new Error(`Required auditor context path escapes the managed runtime: ${relativePath}`);
				}
				const documentBytes = readFileSync(absolutePath);
				const documentText = new TextDecoder("utf-8", { fatal: true }).decode(documentBytes);
				return {
					relativePath,
					absolutePath,
					sha256: sha256(documentBytes),
					byteLength: documentBytes.byteLength,
					lineCount: documentText.split(/\r?\n/).length,
				};
			});
			contextComplete = false;

			const configuredBundleRoot = process.env.DUAL_AUDIT_BUNDLE_ROOT?.trim();
			if (!configuredBundleRoot) {
				throw new Error("Missing DUAL_AUDIT_BUNDLE_ROOT for auditor role");
			}
			if (!isAbsolute(configuredBundleRoot)) {
				throw new Error("DUAL_AUDIT_BUNDLE_ROOT must be an absolute path");
			}
			const configuredRoot = resolve(configuredBundleRoot);
			const canonicalRoot = realpathSync(configuredRoot);
			if (!statSync(canonicalRoot).isDirectory()) {
				throw new Error("DUAL_AUDIT_BUNDLE_ROOT must identify an existing directory");
			}
			auditBundleRoot = canonicalRoot;
		}
	} catch (error) {
		loadError = errorMessage(error);
	}

	function latchViolation(): void {
		acknowledged = false;
		violated = true;
		pendingAckCallId = undefined;
		pendingContextBundle = undefined;
		pi.setActiveTools([]);
	}

	function setGovernedTools(): string[] {
		const ceiling = toolCeiling(role);
		const selected = contextComplete && ceiling
				? (availableTools ?? []).filter((name) => ceiling.has(name))
				: [];
		pi.setActiveTools([...new Set(selected)]);
		return [...new Set(pi.getActiveTools())];
	}

	function contextManifest(): Array<{ path: string; sha256: string; bytes: number; lines: number }> {
		return requiredContextDocuments.map((document) => ({
			path: document.relativePath,
			sha256: document.sha256,
			bytes: document.byteLength,
			lines: document.lineCount,
		}));
	}

	function runtimeManifest(): Array<{ name: string; path: string; sha256: string; bytes: number }> {
		return runtimeArtifacts.map((artifact) => ({
			name: artifact.name,
			path: artifact.absolutePath.replaceAll("\\", "/"),
			sha256: artifact.sha256,
			bytes: artifact.byteLength,
		}));
	}

	function governanceSectionText(): string {
		if (loadError) {
			return [
				"GOVERNANCE LOAD FAILURE",
				`Policy path: ${policyPath}`,
				`Failure: ${loadError}`,
				"No implementation, delegation, or formal audit is authorized.",
			].join("\n");
		}
		if (violated) {
			return [
				"GOVERNANCE SESSION FAILURE",
				`Policy path: ${policyPath}`,
				"This session is permanently latched after a governance violation.",
				"Start a fresh session; no implementation, delegation, planning, or formal audit is authorized here.",
			].join("\n");
		}
		return [
			`Policy-ID: ${POLICY_ID}`,
			`Policy-SHA256: ${policySha256}`,
			`Policy-End-Marker: ${END_MARKER}`,
			`Acknowledgement-Nonce: ${nonce}`,
			`Acknowledgement-State: ${acknowledged && contextComplete ? "verified" : "required"}`,
			`Role: ${role}`,
			`Governance-Context-Root: ${governanceContextRoot.replaceAll("\\", "/")}`,
			`Required-Context-After-Ack: ${JSON.stringify(contextManifest())}`,
			`Managed-Runtime-Artifacts: ${JSON.stringify(runtimeManifest())}`,
			`Audit-Bundle-Root: ${auditBundleRoot?.replaceAll("\\", "/") ?? "<not-applicable>"}`,
			"",
			policyText,
			"",
			...(acknowledged && contextComplete
				? [
						"The one-shot governance acknowledgement and required-context delivery are already verified for this session. Do not repeat the bootstrap acknowledgement.",
						"Continue the restored original task using only the active governed tools.",
					]
				: [
						"Before implementation, delegation, planning, or formal audit, call governance_ack with the exact envelope values above.",
						"If Required-Context-After-Ack is non-empty, governance_ack itself will deliver every listed document's complete identity-pinned content as one non-truncated bundle. The runner exposes no planning, delegation, or background tools until it verifies that exact tool result.",
					]),
			"When planning a managed runner, background Job, or runner smoke-test change, inspect the exact Managed-Runtime-Artifacts paths above. Do not invent repository-local replacements when those artifacts are outside the current worktree.",
		].join("\n");
	}

	function refreshGovernanceSection(message: unknown): unknown {
		if (!isRecord(message) || message.role !== "system" || !isRecord(message.sections)) {
			return message;
		}
		return {
			...message,
			sections: {
				...message.sections,
				development_governance: governanceSectionText(),
			},
		};
	}

	function auditorPathViolation(toolName: string, input: unknown): string | undefined {
		if (role !== "auditor" || !READ_ONLY_TOOLS.has(toolName)) return undefined;
		if (!auditBundleRoot) return "Auditor bundle root is unavailable";
		if (!isRecord(input) || typeof input.path !== "string" || input.path.trim().length === 0) {
			return `Auditor tool ${toolName} requires an explicit path inside DUAL_AUDIT_BUNDLE_ROOT`;
		}

		const requestedPath = resolve(workspaceRoot, input.path);
		let canonicalPath: string;
		try {
			canonicalPath = realpathSync(requestedPath);
		} catch (error) {
			return `Auditor tool ${toolName} path cannot be resolved: ${errorMessage(error)}`;
		}
		if (!isPathWithinRoot(auditBundleRoot, canonicalPath)) {
			return `Auditor tool ${toolName} path escapes DUAL_AUDIT_BUNDLE_ROOT`;
		}
		return undefined;
	}

	pi.registerTool({
		name: ACK_TOOL,
		label: "Acknowledge Development Governance",
		description:
			"Acknowledge the exact mandatory development-governance policy before implementation, delegation, or audit.",
		parameters: Type.Object({
			policyId: Type.Literal(POLICY_ID),
			policySha256: Type.String({ description: "Exact SHA-256 shown in the governance envelope" }),
			endMarker: Type.Literal(END_MARKER),
			nonce: Type.String({ description: "Exact acknowledgement nonce shown in the governance envelope" }),
			role: Type.String({ description: "Exact role shown in the governance envelope" }),
		}, { additionalProperties: false }),
		async execute(toolCallId, params) {
			if (loadError || violated) {
				throw new Error(
					loadError
						? `Governance policy unavailable: ${loadError}`
						: "Governance gate permanently failed after a prohibited or invalid call",
				);
			}
			if (
				toolCallId !== pendingAckCallId ||
				params.policyId !== POLICY_ID ||
				params.policySha256 !== policySha256 ||
				params.endMarker !== END_MARKER ||
				params.nonce !== nonce ||
				params.role !== role
			) {
				latchViolation();
				throw new Error("Governance acknowledgement does not match the injected envelope");
			}

			const ceiling = toolCeiling(role);
			if (!ceiling) {
				latchViolation();
				throw new Error(`Unsupported governance role: ${role || "<empty>"}`);
			}
			const requiredContext = contextManifest();
			let content: Array<{ type: "text"; text: string }>;
			let phase: "ready" | "context_delivery_pending";
			let activeTools: string[];

			if (requiredContextDocuments.length > 0) {
				const renderedDocuments: string[] = [];
				try {
					for (const document of requiredContextDocuments) {
						const currentBytes = readFileSync(document.absolutePath);
						if (
							currentBytes.byteLength !== document.byteLength ||
							sha256(currentBytes) !== document.sha256
						) {
							throw new Error(`identity changed for ${document.relativePath}`);
						}
						const documentText = new TextDecoder("utf-8", { fatal: true }).decode(currentBytes);
						renderedDocuments.push([
							`BEGIN REQUIRED GOVERNANCE DOCUMENT path=${document.relativePath} sha256=${document.sha256} bytes=${document.byteLength} lines=${document.lineCount}`,
							documentText,
							`END REQUIRED GOVERNANCE DOCUMENT path=${document.relativePath} sha256=${document.sha256}`,
						].join("\n"));
					}
				} catch (error) {
					latchViolation();
					throw new Error(`Required governance context could not be delivered: ${errorMessage(error)}`);
				}

				const bundleText = [
					`Governance acknowledged: ${POLICY_ID} ${policySha256} role=${role} phase=context_delivery_pending`,
					`BEGIN REQUIRED GOVERNANCE CONTEXT BUNDLE ${JSON.stringify(requiredContext)}`,
					...renderedDocuments,
					`END REQUIRED GOVERNANCE CONTEXT BUNDLE total=${requiredContextDocuments.length}`,
				].join("\n");
				const bundleBytes = new TextEncoder().encode(bundleText).byteLength;
				const bundleLines = bundleText.split(/\r?\n/).length;
				if (bundleBytes > MAX_CONTEXT_BUNDLE_BYTES || bundleLines > MAX_CONTEXT_BUNDLE_LINES) {
					latchViolation();
					throw new Error(
						`Required governance context exceeds the non-truncating limit: ${bundleBytes} bytes/${bundleLines} lines`,
					);
				}

				content = [{ type: "text", text: bundleText }];
				pendingContextBundle = {
					toolCallId,
					serializedContent: JSON.stringify(content),
				};
				acknowledged = true;
				pendingAckCallId = undefined;
				contextComplete = false;
				pi.setActiveTools([]);
				activeTools = [...pi.getActiveTools()];
				phase = "context_delivery_pending";
			} else {
				acknowledged = true;
				pendingAckCallId = undefined;
				contextComplete = true;
				activeTools = setGovernedTools();
				phase = "ready";
				content = [
					{
						type: "text",
						text: `Governance acknowledged: ${POLICY_ID} ${policySha256} role=${role} phase=${phase} activeTools=${JSON.stringify(activeTools)}`,
					},
				];
			}

			return {
				content,
				details: {
					policyId: POLICY_ID,
					policySha256,
					endMarker: END_MARKER,
					nonce,
					role,
					acknowledged: true,
					phase,
					activeTools,
					requiredContext,
				},
			};
		},
	});

	pi.on("session_start", () => {
		acknowledged = false;
		violated = false;
		pendingAckCallId = undefined;
		pendingContextBundle = undefined;
		contextComplete = requiredContextDocuments.length === 0;
		nonce = randomUUID();
	});

	pi.on("before_agent_start", (event) => {
		if (availableTools === undefined) {
			availableTools = pi.getAllTools()
				.map((tool) => tool.name)
				.filter((name) => name !== ACK_TOOL);
		}

		if (loadError || violated) {
			pi.setActiveTools([]);
		} else if (!acknowledged) {
			const restricted = loadError
				? []
				: [
						...availableTools.filter((name) => PRE_ACK_TOOLS.has(name)),
						ACK_TOOL,
					];
			pi.setActiveTools([...new Set(restricted)]);
		} else {
			setGovernedTools();
		}

		event.systemPromptOptions.sections.development_governance = governanceSectionText();
	});

	pi.on("context", (event) => {
		if (loadError || violated) return;
		if (acknowledged) {
			if (!contextComplete) {
				latchViolation();
				return { messages: [] };
			}
			return { messages: event.messages.map(refreshGovernanceSection) };
		}
		const systemMessages = event.messages.filter(
			(message) => isRecord(message) && message.role === "system",
		);
		const latestUserMessage = [...event.messages].reverse().find(
			(message) => isRecord(message) && message.role === "user",
		);
		if (!latestUserMessage) {
			latchViolation();
			return { messages: [] };
		}
		return {
			messages: [
				...systemMessages,
				{
					...latestUserMessage,
					content: [
						{
							type: "text",
							text: "MANDATORY GOVERNANCE BOOTSTRAP ONLY. Call governance_ack now with the exact Policy ID, SHA-256, end marker, nonce, and role from the development_governance system section. Emit no prose and do not begin the user task. The original user message will be restored after the acknowledgement result is verified.",
						},
					],
				},
			],
		};
	});

	pi.on("before_provider_request", (event) => {
		if (loadError || violated) return;
		if (acknowledged) {
			if (!contextComplete) {
				latchViolation();
				throw new Error("Governance context delivery was not finalized before the next provider request");
			}
			return;
		}
		const requiredTool = ACK_TOOL;
		if (!isRecord(event.payload) || !Array.isArray(event.payload.tools)) {
			latchViolation();
			throw new Error(`Required governance tool ${requiredTool} is missing from the provider request`);
		}
		const acknowledgementTools = event.payload.tools.filter((tool) =>
			isNamedToolDefinition(tool, requiredTool),
		);
		if (acknowledgementTools.length !== 1) {
			latchViolation();
			throw new Error(`Provider request must contain exactly one ${requiredTool} definition`);
		}
		const acknowledgementTool = acknowledgementTools[0];

		return {
			...event.payload,
			tools: [acknowledgementTool],
			tool_choice: {
				type: "function",
				function: { name: requiredTool },
			},
		};
	});

	pi.on("message_end", (event) => {
		if (!isRecord(event.message) || event.message.role !== "assistant") return;

		if (loadError || violated) {
			return {
				message: governanceFailureMessage(
					event.message,
					loadError ? `policy unavailable: ${loadError}` : "governance gate is in a failed state",
				),
			};
		}
		if (acknowledged && !contextComplete) {
			latchViolation();
			return {
				message: governanceFailureMessage(
					event.message,
					"assistant response was emitted before the required governance context bundle was verified",
				),
			};
		}
		if (acknowledged) return;

		const ackCall = getValidAckToolCall(event.message, policySha256, nonce, role);
		if (ackCall) {
			pendingAckCallId = String(ackCall.id);
			return {
				message: {
					...event.message,
					content: [ackCall],
					stopReason: "toolUse",
				},
			};
		}

		latchViolation();
		return {
			message: governanceFailureMessage(
				event.message,
				"assistant response was emitted before a valid governance acknowledgement",
			),
		};
	});

	pi.on("tool_call", (event) => {
		if (loadError || violated) {
			return {
				block: true,
				reason: "Governance gate is in a failed state",
				terminate: true,
			};
		}

		if (!acknowledged) {
			if (
				event.toolName !== ACK_TOOL ||
				event.toolCallId !== pendingAckCallId ||
				!isExactAcknowledgementInput(event.input, policySha256, nonce, role)
			) {
				latchViolation();
				return {
					block: true,
					reason: `Tool ${event.toolName} is prohibited or invalid before governance acknowledgement`,
					terminate: true,
				};
			}
		}

		if (acknowledged && !contextComplete) {
			latchViolation();
			return {
				block: true,
				reason: `Tool ${event.toolName} is prohibited until the governance acknowledgement result is verified`,
				terminate: true,
			};
		}

		const ceiling = toolCeiling(role);
		if (acknowledged && (!ceiling || !ceiling.has(event.toolName))) {
			latchViolation();
			return {
				block: true,
				reason: `Tool ${event.toolName} exceeds the enforced tool ceiling for role ${role}`,
				terminate: true,
			};
		}

		if (acknowledged) {
			const pathViolation = auditorPathViolation(event.toolName, event.input);
			if (pathViolation) {
				latchViolation();
				return {
					block: true,
					reason: pathViolation,
					terminate: true,
				};
			}
		}
	});

	pi.on("tool_result", (event) => {
		if (event.toolName !== ACK_TOOL || !pendingContextBundle) return;
		if (
			violated ||
			loadError ||
			!acknowledged ||
			contextComplete ||
			event.toolCallId !== pendingContextBundle.toolCallId ||
			event.isError ||
			JSON.stringify(event.content) !== pendingContextBundle.serializedContent
		) {
			latchViolation();
			return {
				content: [
					{
						type: "text",
						text: `${FAILURE_PREFIX}: governance acknowledgement context bundle was missing or mutated`,
					},
				],
				isError: true,
			};
		}

		pendingContextBundle = undefined;
		contextComplete = true;
		const activeTools = setGovernedTools();
		return {
			details: {
				...(isRecord(event.details) ? event.details : {}),
				phase: "ready",
				activeTools,
				governanceContext: {
					complete: true,
					documents: contextManifest(),
					activeTools,
				},
			},
		};
	});
}
