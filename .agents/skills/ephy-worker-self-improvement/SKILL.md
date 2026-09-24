---
name: ephy-worker-self-improvement
description: Run a proposal-only improvement loop for the ephy-worker repository with gpt-oss leading evaluation and audit and Qwen implementing one hypothesis. Use only when the user asks to improve this repository itself; do not use for ordinary feature work or another repository.
---

# ephy-worker self-improvement

Use this skill only when the current repository is `ephy-worker` and the request concerns improving its codebase，documentation，tests，or development process．

Before acting，read [`AGENTS.md`](../../../AGENTS.md) and the complete [system development governance](../../../docs/system-development-governance.md)，check Git status，and inspect only the source，tests，and documents relevant to the proposed improvement．Preserve existing work and authorization boundaries．For managed Pi runs，a valid `governance_ack` must precede every write-capable or delegation tool．The external runner gate，not the agent's statement，is authoritative．

## Roles

- gpt-oss planner：define the hypothesis，scope，acceptance evidence，environment，and baseline without editing the candidate．
- Qwen worker：implement the single delegated hypothesis and fix tests within that scope．
- Independent verifier：run the frozen checks after Qwen exits without repairing the candidate．
- Fresh gpt-oss auditor：read the frozen audit bundle in a read-only process，return one schema-valid decision，and never edit or re-run checks．
- Human or Codex reviewer：decide whether a reviewed proposal is applied．The loop must not commit，merge，push，deploy，or automatically adopt its own result．

## Proposal-only loop

1. Before any model implements，run preflight and freeze exactly one improvement hypothesis，base commit，clean worktree，file and semantic scope，acceptance condition，environment contract，checks，models，limits，and required document hashes．Stop if any contract is missing or inconsistent．
2. Start a fresh gpt-oss planning stage．It records the plan and baseline but has no candidate write authority．
3. For each frozen attempt，delegate only that hypothesis to Qwen in the isolated worktree．The lead must not perform even a small direct edit as a substitute．
4. After Qwen exits，measure the candidate with the same command，fixture，and environment used for the baseline，then run every hard gate in [the evaluation contract](references/eval-contract.md) independently．The verifier must not change the candidate．If a required tool is unavailable，report the check as unexecuted; never count it as passing．
5. If and only if a candidate-origin hard gate fails，record the exact result and，within the fixed limit，return to step 3 for a Qwen repair．Keep the hypothesis，scope，fixture，environment，and verification commands fixed．A repeated patch state，invalid command，environment failure，wrong model，scope violation，verifier mutation，or missing evidence stops the Job without repair．
6. After one attempt passes every independent gate，freeze that exact final patch，candidate snapshot，verification results，workflow events，model provenance，and their hashes into an audit bundle．Validate its [audit input schema](references/audit-input.schema.json) and [evidence manifest schema](references/evidence-manifest.schema.json) outside the model．
7. Start a fresh gpt-oss process with only read-only tools and apply [the independent audit contract](references/audit-contract.md) to that exact bundle．Its single JSON result must satisfy [the audit result schema](references/audit-result.schema.json)．
8. The runner attests the actual auditor model，tool trace，schema validation，and unchanged candidate before accepting the audit result．`ACCEPT_PROPOSAL` means reviewable only，not applied．
9. If any required gate or attestation fails，reject or mark the proposal inconclusive and retain its patch and logs．Never treat an agent narrative as a gate result．

Keep `SKILL.md` focused on the reusable workflow．Read [the evaluation contract](references/eval-contract.md) when defining measurements，read [the independent audit contract](references/audit-contract.md) before assembling or auditing a bundle，and use [the MVP description](../../../docs/self-improvement-mvp.md) for the implemented boundary．The external Windows runtime still needs workspace-root confinement and the formal audit connection before this loop can be called complete．

## PR review transport

The proposal Job itself stops before commit，push，PR，or merge．After that stop，a separately authorized Integration／release operator may bind the frozen patch SHA-256 to a dedicated branch and PR solely to obtain external review．For a self-improvement PR，apply the repository `Code Review Rules` and use [the Codex Review prompt](../../../.pi/prompts/review-self-improvement-pr.md) for a manual request．Require CI and Codex Review on the current PR head，invalidate both after any new push，and leave merge to an explicit human decision．Never report this bootstrap path as a completed formal gpt-oss audit or runner-attested `review_ready` state．
