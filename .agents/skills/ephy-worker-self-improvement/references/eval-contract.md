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

After rejection，stop the iteration．A new attempt must define a new single hypothesis rather than silently broadening the failed one．

## Audit output

The gpt-oss lead reports：the hypothesis，baseline/candidate comparison，each evaluation axis，executed checks，unexecuted checks with reasons，Git status，diff summary，decision，and remaining risks．Passing gates make a proposal eligible for review; they do not authorize automatic application．
