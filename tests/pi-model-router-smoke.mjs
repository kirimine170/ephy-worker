import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const repositoryRoot = resolve(import.meta.dirname, "..");
const routerPath = join(repositoryRoot, "tools", "pi-local", "ephy-model-router.ts");
const reasoningId = "Qwen3.8-27B-UD-Q4_K_M";
const coderId = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M";

class FakePi {
  constructor() {
    this.commands = new Map();
    this.handlers = new Map();
    this.tools = new Map();
    this.thinking = "off";
    this.selectedModel = undefined;
  }

  getThinkingLevel() {
    return this.thinking;
  }

  on(name, handler) {
    this.handlers.set(name, handler);
  }

  registerCommand(name, command) {
    this.commands.set(name, command);
  }

  registerTool(tool) {
    this.tools.set(tool.name, tool);
  }

  async setModel(model) {
    this.selectedModel = model;
    return true;
  }

  setThinkingLevel(level) {
    this.thinking = level;
  }
}

function context(cwd) {
  const models = new Map([
    [`llama-local/${reasoningId}`, { provider: "llama-local", id: reasoningId }],
    [`llama-local/${coderId}`, { provider: "llama-local", id: coderId }],
  ]);
  return {
    cwd,
    model: models.get(`llama-local/${reasoningId}`),
    modelRegistry: {
      find(provider, id) {
        return models.get(`${provider}/${id}`);
      },
    },
    ui: {
      notify() {},
      setStatus() {},
    },
  };
}

function resetEnvironment() {
  delete process.env.DUAL_GOVERNANCE_ROLE;
  delete process.env.EPHY_PI_MODEL_PROVIDER;
  delete process.env.EPHY_PI_REASONING_MODEL;
  delete process.env.EPHY_PI_CODER_MODEL;
  delete process.env.EPHY_PI_MODEL_ROUTER_AUDIT;
}

const { default: modelRouter } = await import(pathToFileURL(routerPath).href);

resetEnvironment();
delete process.env.EPHY_PI_MODEL_ROUTER;
const disabledPi = new FakePi();
modelRouter(disabledPi);
assert.equal(disabledPi.tools.size, 0);

process.env.EPHY_PI_MODEL_ROUTER = "1";
process.env.DUAL_GOVERNANCE_ROLE = "implementer";
const governedPi = new FakePi();
modelRouter(governedPi);
assert.equal(governedPi.tools.size, 0);

resetEnvironment();
process.env.EPHY_PI_MODEL_ROUTER = "1";
const root = await mkdtemp(join(tmpdir(), "ephy-model-router-"));
try {
  const audit = join(root, "audit", "model-routing.jsonl");
  process.env.EPHY_PI_MODEL_ROUTER_AUDIT = audit;
  const pi = new FakePi();
  modelRouter(pi);
  assert.deepEqual([...pi.tools.keys()], ["ephy_select_model"]);
  assert.deepEqual([...pi.commands.keys()], ["model-route-status"]);

  const result = await pi.tools.get("ephy_select_model").execute(
    "switch-1",
    {
      target: "coder",
      thinking: "high",
      reason: "Implement and test a substantial code change",
    },
    undefined,
    undefined,
    context(root),
  );

  assert.equal(result.details.switched, true);
  assert.equal(result.details.from, reasoningId);
  assert.equal(result.details.to, coderId);
  assert.equal(result.details.thinking, "off");
  assert.equal(pi.selectedModel.id, coderId);
  assert.equal(pi.thinking, "off");
  const records = (await readFile(audit, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(records.length, 1);
  assert.equal(records[0].event, "model_switch");
  assert.equal(records[0].from, reasoningId);
  assert.equal(records[0].to, coderId);
} finally {
  resetEnvironment();
  delete process.env.EPHY_PI_MODEL_ROUTER;
  await rm(root, { recursive: true, force: true });
}

const modelConfig = JSON.parse(
  await readFile(join(repositoryRoot, "configs", "pi-autonomous-models.example.json"), "utf8"),
);
assert.deepEqual(
  modelConfig.providers["llama-local"].models.map((model) => model.id),
  [reasoningId, coderId],
);
for (const script of ["run.sh", "run.ps1"]) {
  const content = await readFile(join(repositoryRoot, "tools", "pi-local", script), "utf8");
  assert.match(content, /--no-extensions/);
  assert.match(content, /ephy-model-router\.ts/);
  assert.match(content, /EPHY_PI_MODEL_ROUTER/);
}

console.log("PASS: Pi model router opt-in, governance isolation, switch, audit, and launch contract");
