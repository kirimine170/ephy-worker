# Evaluation contract

Use this contract when preparing or auditing one ephy-worker improvement proposal．It defines evidence fields and gates，not a composite score．

## Evidence record

Record the following for both baseline and candidate：

- hypothesis identifier and allowed file scope;
- exact command and fixture;
- start and finish timestamps;
- exit code and captured output;
- Git status and diff summary;
- additional human instructions after the job started．

Baseline and candidate must use the same command，fixture，environment assumptions，and success condition．If that comparison cannot be completed，stop without recommending adoption．

## Initial evaluation axes

- correctness：exit code of the specified test or fixture．
- regression gate：repository validation，configured lint，and `git diff --check`．
- scope：presence or absence of changes outside the allowed files．
- latency：wall-clock time from job start to `review_ready`．
- intervention：count of additional human instructions after submission．

Report each axis separately．Do not calculate a weighted or aggregate score．Security hard constraints remain in force，but security analysis and cost optimization are not improvement axes in this MVP．

## Hard gates and stop conditions

Reject the candidate and retain the worktree patch and logs when any of these occurs：

- the specified correctness check fails;
- an available repository validation or lint gate fails;
- `git diff --check` fails;
- the candidate changes files outside the declared scope;
- baseline and candidate were not measured under the same conditions;
- a required check is unexecuted because its toolchain is unavailable;
- the lead cannot inspect the actual diff or verification evidence．

The Windows runner may diagnose and repair the same candidate at most twice after the first independent check．For each attempt，retain the failed check output，candidate patch fingerprint，and subsequent full gate results．The hypothesis，scope，fixture，and commands cannot change．A repeated patch state，invalid verification command，or unavailable toolchain stops repair immediately．After the bound，reject and retain the worktree patch and logs．A later new iteration requires a newly declared single hypothesis．

`scripts/validate_self_improvement.py` is the candidate evidence gate：pass distinct Git roots with `--baseline-root` and `--candidate-root`，the same relative `--fixture` bytes，one or more exact `--allowed` paths，and a single argv after `--`．For example，`python scripts/validate_self_improvement.py --baseline-root BASE --candidate-root CANDIDATE --fixture tests/fixture.json --allowed scripts/example.py -- python -m pytest -q tests/test_example.py`．The JSON output contains both measurements and each candidate hard gate．Run this only when the baseline and candidate toolchains are available; never use a skip flag to turn an unexecuted check into a pass．
For a Python measurement，select one existing interpreter with `--python`，declare the package with `--source-package` and required dependencies/files with `--required-module`／`--required-file`，and pin both roots to `--expected-head`．The gate checks that each worktree imports its own `src`，uses that worktree as `cwd`，and runs offline．For locale-sensitive cases，remove the same environment variables from both runs with `--unset-env`．When a specific baseline failure is required，use `--baseline-failure-text` so an unrelated failure cannot count as improvement．A task-specific `--fixed-check` must live outside both measured worktrees and is hashed before and after execution．Save `--evidence` outside both worktrees．These options do not replace the separate preflight needed before the external Pi background runner starts an agent．
The Ruff gate checks declared allowed Python files that exist in the candidate，and records its exact targets．When there are no such files，it checks Ruff availability and records lint as not applicable，not as a full-repository lint pass．This avoids treating unrelated lint debt in the baseline as a candidate regression．

## Audit output

The gpt-oss lead reports：the hypothesis，baseline/candidate comparison，each evaluation axis，executed checks，unexecuted checks with reasons，Git status，diff summary，decision，and remaining risks．Passing gates make a proposal eligible for review; they do not authorize automatic application．
