---
name: ephy-worker-self-improvement
description: Run a proposal-only improvement loop for the ephy-worker repository with frozen model roles, independent evaluation and review of one Pi-implemented hypothesis. Use only when the user asks to improve this repository itself; do not use for ordinary feature work or another repository.
---

# ephy-worker self-improvement

Use this skill only when the current repository is `ephy-worker` and the request concerns improving its codebase，documentation，tests，or development process．

Before acting，read [`AGENTS.md`](../../../AGENTS.md) and the complete [system development governance](../../../docs/system-development-governance.md)，check Git status，and inspect only the source，tests，and documents relevant to the proposed improvement．Preserve existing work and authorization boundaries．For managed Pi runs，a valid `governance_ack` must precede every write-capable or delegation tool．The external runner gate，not the agent's statement，is authoritative．

## Roles

- designated-model planner：define the hypothesis，scope，acceptance evidence，environment，and baseline without editing the candidate．
- designated Pi worker：implement the single delegated hypothesis and fix tests within that scope．
- Independent verifier：run the frozen checks after Pi exits without repairing the candidate．
- Fresh designated-model auditor：read the frozen audit bundle in a read-only process，return one schema-valid decision，and never edit or re-run checks．The explicitly authorized external Strata profile instead stops at `external_review_pending` for independent Codex Review；it cannot claim a completed formal audit．
- Human or Codex reviewer：decide whether a reviewed proposal is applied．The loop must not commit，merge，push，deploy，or automatically adopt its own result．

## Proposal-only loop

1. Before any model implements，run preflight and freeze exactly one improvement hypothesis，base commit，clean worktree，file and semantic scope，acceptance condition，environment contract，checks，models，limits，and required document hashes．Stop if any contract is missing or inconsistent．
2. Start a fresh designated-model planning stage．It records the plan and baseline but has no candidate write authority．
3. For each frozen attempt，delegate only that hypothesis to the designated Pi implementer in the isolated worktree．The lead must not perform even a small direct edit as a substitute．
4. After the Pi implementer exits，measure the candidate with the same command，fixture，and environment used for the baseline，then run every hard gate in [the evaluation contract](references/eval-contract.md) independently．The verifier must not change the candidate．If a required tool is unavailable，report the check as unexecuted; never count it as passing．
5. If and only if a candidate-origin hard gate fails，record the exact result and，within the fixed limit，return to step 3 for a Pi repair．Keep the hypothesis，scope，fixture，environment，and verification commands fixed．A repeated patch state，invalid command，environment failure，wrong model，scope violation，verifier mutation，or missing evidence stops the Job without repair．
6. After one attempt passes every independent gate，freeze that exact final patch，candidate snapshot，verification results，workflow events，model provenance，and their hashes into an audit bundle．Validate its [audit input schema](references/audit-input.schema.json) and [evidence manifest schema](references/evidence-manifest.schema.json) outside the model．
7. Start a fresh designated-model process with only read-only tools and apply [the independent audit contract](references/audit-contract.md) to that exact bundle．Its single JSON result must satisfy [the audit result schema](references/audit-result.schema.json)．
8. The runner attests the actual auditor model，tool trace，schema validation，and unchanged candidate before accepting the audit result．`ACCEPT_PROPOSAL` means reviewable only，not applied．
9. If any required gate or attestation fails，reject or mark the proposal inconclusive and retain its patch and logs．Never treat an agent narrative as a gate result．

Keep `SKILL.md` focused on the reusable workflow．Read [the evaluation contract](references/eval-contract.md) when defining measurements，read [the independent audit contract](references/audit-contract.md) before assembling or auditing a bundle，and use [the MVP description](../../../docs/self-improvement-mvp.md) for the implemented boundary．The external Windows runtime still needs workspace-root confinement and the formal audit connection before this loop can be called complete．

## PR review transport

The proposal Job itself stops before commit，push，PR，or merge．After that stop，a separately authorized Integration／release operator may bind the frozen patch SHA-256 to a dedicated branch and PR solely to obtain external review．For a self-improvement PR，apply the repository `Code Review Rules` and use [the Codex Review prompt](../../../.pi/prompts/review-self-improvement-pr.md) for a manual request．Require CI and Codex Review on the current PR head，invalidate both after any new push，and leave merge to an explicit human decision．Never report this bootstrap path as a completed formal designated-model audit or runner-attested `review_ready` state．

## Accepted named Codex bootstrap adoption

The [accepted bootstrap adoption ADR](../../../docs/adr/0004-authorized-codex-bootstrap-adoption.md) records the user's 2026-10-04 approval to adopt only the named PR17 infrastructure and six-document alignment with absent Pi implementation and formal pre-audit provenance disclosed．The Integration／release operator must bind the final full patch／tree／head，pass current-head CI and independent full-scope Codex Review，and resolve all P0／P1 before the approved merge．This is a manually checked adoption condition，not a new runner state or a declaration that missing historical gates passed．

Do not apply this limited route to an ordinary proposal Job or grant a candidate new tools，roles，scope，repair budgets，or adoption authority．Keep every loop step，normal provenance／audit／budget requirement and current-head review requirement above．Existing Pi Markdown traces remain evidence for their exact candidates and source／policy hashes；they do not show that Pi implemented the bootstrap code．PR15／PR16 cleanup and new experiments are outside this adoption．
