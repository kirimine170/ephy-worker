import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { stopManagedStage } from "./formal-stage-stop.ts";

// Controller-owned identity is pinned outside the writable candidate.
export default function (pi: ExtensionAPI) {
  const identity = JSON.parse(readFileSync(process.env.EPHY_STRATA_IDENTITY!, "utf8"));
  const origin = new URL(identity.base_url);
  if (origin.protocol !== "http:" || origin.hostname !== "127.0.0.1" ||
      !origin.port || origin.pathname !== "/" || origin.search || origin.hash ||
      origin.username || origin.password) throw new Error("Invalid frozen Strata origin");
  pi.registerProvider("strata-local", {
    baseUrl: identity.base_url + "/v1",
    apiKey: "local-no-auth",
    api: "openai-completions",
    compat: { supportsDeveloperRole: false, supportsReasoningEffort: true,
      maxTokensField: "max_tokens", thinkingFormat: "qwen-chat-template" },
    models: [{
      id: identity.model_id, name: identity.model_id,
      reasoning: true, input: ["text"], contextWindow: identity.context, maxTokens: 16384,
      thinkingLevelMap: { off: "none", minimal: null, low: "low", medium: "medium",
        high: "high", xhigh: null, max: null },
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    }],
  });
  pi.on("before_provider_request", async () => {
    try {
      const response = await fetch(identity.base_url + "/health", {
        signal: AbortSignal.timeout(5000), redirect: "error",
      });
      if (!response.ok) throw new Error("STRATA_IDENTITY_UNAVAILABLE");
      const health = await response.json() as any;
      if (!health.loaded || health.service !== "strata" || health.api_key !== false ||
          health.model !== identity.model_id || health.max_context !== identity.context)
        throw new Error("STRATA_IDENTITY_CHANGED");
    } catch {
      stopManagedStage("Strata identity unavailable or changed before provider request");
    }
  });
}
