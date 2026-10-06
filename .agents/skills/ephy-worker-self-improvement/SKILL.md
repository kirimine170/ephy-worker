---
name: ephy-worker-self-improvement
description: Run a proposal-only improvement loop for the ephy-worker repository with gpt-oss leading evaluation and audit and Qwen implementing one hypothesis. Use only when the user asks to improve this repository itself; do not use for ordinary feature work or another repository.
---

# ephy-worker self-improvement

Use this skill only when the current repository is `ephy-worker` and the request concerns improving its codebase，documentation，tests，or development process．

Before acting，read [`AGENTS.md`](../../../AGENTS.md)，check Git status，and inspect only the source，tests，and documents relevant to the proposed improvement．Preserve existing work and authorization boundaries．

## Roles

- gpt-oss lead：define the hypothesis and acceptance evidence，record the baseline，audit the candidate diff and results，and make the final recommendation．
- Qwen worker：implement the single delegated hypothesis and fix tests within that scope．
- Human or Codex reviewer：decide whether a reviewed proposal is applied．The loop must not commit，merge，push，deploy，or automatically adopt its own result．

## Proposal-only loop

1. State exactly one improvement hypothesis，its allowed files，and its observable acceptance condition．
2. Record a baseline using a fixed command and fixture．Record the command，fixture，exit code，and result rather than relying on a narrative judgment．
3. Delegate only that hypothesis to Qwen in an isolated worktree．
4. Measure the candidate with the same command and fixture used for the baseline．Do not change the benchmark after seeing the candidate．
5. Run the hard gates in [the evaluation contract](references/eval-contract.md)．If a required tool is unavailable，report the check as unexecuted; never count it as passing．
6. Have gpt-oss inspect the actual diff，scope，baseline/candidate evidence，and hard-gate output．
7. If any hard gate fails，reject the candidate，retain its patch and logs for audit，and stop．If the gates pass，leave the patch for an explicit human or Codex application decision．

Keep `SKILL.md` focused on the reusable workflow．Read [the evaluation contract](references/eval-contract.md) when defining measurements or auditing a candidate，and use [the MVP description](../../../docs/self-improvement-mvp.md) for the implemented boundary．
