import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const routerUrl = (process.env.DUAL_LLAMA_BASE_URL ?? "http://127.0.0.1:18080").replace(/\/$/, "");

  pi.registerProvider("dual-local", {
    baseUrl: `${routerUrl}/v1`,
    apiKey: "local",
    api: "openai-completions",
    compat: {
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
    },
    models: [
      {
        id: "gpt-oss-20b-MXFP4",
        name: "gpt-oss-20b lead",
        reasoning: true,
        compat: {
          supportsReasoningEffort: true,
        },
        input: ["text"],
        contextWindow: 32768,
        maxTokens: 8192,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      },
      {
        id: "Qwen3-Coder-Next-Q4_K_M",
        name: "Qwen3-Coder-Next worker",
        reasoning: false,
        input: ["text"],
        contextWindow: 32768,
        maxTokens: 4096,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      },
      {
        id: "Qwen3.8-Flash-Next-UD-IQ4_XS",
        name: "Qwen3.8-Flash-Next",
        reasoning: true,
        input: ["text"],
        contextWindow: 32768,
        maxTokens: 8192,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      },
    ],
  });
}
