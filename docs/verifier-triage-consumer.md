# Read-only verifier triage consumer

This is Codex-authored evaluation infrastructure, not a Pi-generated implementation,
formal audit, accepted proposal or adoption decision. Its baseline is main
26ddbe65a9bd9fe530dca13238c2eb3838f3aee5. Public development fixtures contain no
held-out data. Real model generation is a separately authorized operation.

Legacy external-review bundles lack generation isolation evidence and remain
blocked before fixed_trial launch. The additional isolation contract now freezes
the evaluation plan, raw batch/gold, evaluator and controller source closure
before candidate authoring, captures every final HTTP input and tool result, and
binds the capture to the same external job and actual Pi processes. There is no
enabling flag. Native controls use an owned fake provider; their capture cannot
authorize a real trial. No real generation or held-out trial has been performed.
Pure scoring does not attest generation isolation or a held-out effect.

## Frozen experiment

Generate one Markdown candidate at
.agents/skills/ephy-verifier-triage/SKILL.md with the existing external Strata
proposal workflow: one attempt, repair 0. Preserve its frozen job and external
review bundle. The driver accepts an already generated candidate only;
verify_external_proposal must pass. Candidate generation must not receive the
held-out batch or gold.

The controller freezes raw batch, gold, candidate, evaluator, prompt, canonical
governance, Pi/extensions, controller Python, dependency versions and runtime
hashes. The batch has exactly twelve opaque case IDs. Each input contains
expected/observed records with short id fields and measured aggregate/component
hashes. Optional CI records have expected_id, observed_id,
expected_revision, and observed_revision. Gold contains the same twelve IDs
and the expected decision, diagnosis and two evidence IDs. Gold and controller
files stay outside model-visible input directories.

build_contract in ephy_worker.verifier_triage_consumer takes repository, Pi,
observed existing-service identity, batch, gold, generated skill, a fresh artifact
directory, and tokenizer argv. Use purpose fixed_trial and supply
generation_job, isolation_freeze and its externally retained isolation_sha256.
The existing identity must match the generation job. Canonical
documents must match the job's frozen governance root byte for byte. Both
generation and consumers use the same resource lock.
Generation proposal caps must be at most 8 requests, 2200 output tokens, 1024
response tokens, 300 stage seconds and 900 job seconds with repair 0; a larger
existing proposal cannot be relabeled with smaller reservations. These checks
do not replace the pre-authoring isolation gate.

After exact-head CI and independent review of the isolation adapter, and explicit
execution authorization, a fully bound contract uses this command:

~~~text
python -m ephy_worker.verifier_triage_consumer --contract ABSOLUTE_CONTRACT_JSON --sha256 FROZEN_SHA256 --execute-authorized
~~~

The driver runs exactly baseline 2 + treatment 2, each in a fresh process/session
with an identical batch and canonical context. Pi uses --no-skills:
treatment explicitly reads the candidate and proves full-byte delivery with
evidence_read plus an additional byte-exact triage_exact_read event comparing
raw and delivered byte counts and SHA256. Batch and skill inputs must be UTF-8/LF,
without CR bytes or BOM; no silent normalization is allowed. This is not automatic skill discovery. Baseline
has no candidate file and every actual transmitted request is checked for its
body, including JSON-encoded tool content. Canonical policy and required context
delivery are checked in the saved governance result. Only governance acknowledgement
and exact batch/skill reads are permitted. These capability checks and snapshots
do not attest an OS security sandbox.

## Limits and context measurement

Each consumer retains the observed ceiling: 8 generation requests, 2,200 total
output tokens, at most 1,024 tokens per response, and 300 seconds. Four sessions
mean at most 32 consumer requests and 48 case observations. Observations are not
request reservations. Preflight (8 requests/300 seconds) and candidate generation
(planner + implementer, at most 16 requests, job 900 seconds, repair 0) are
recorded separately; this driver launches neither. These are ceilings, not a
requirement to spend the budget or authority to invoke the model.

The gateway captures the final HTTP body after governance hooks, counts that
exact body, saves the raw response and receipt, and forwards unchanged bytes to
the frozen existing loopback service. It rejects N+1, aliases, changed model,
server augmentation and concurrent requests. A refusal latches the session.
A complete-batch projection containing canonical context, the full batch,
treatment skill, and the pre-governance tool-schema superset is counted before
the first upstream request. The projection is a conservative admission input,
not a claim to reproduce all future turns. Every actual later request is counted
again, with 2,208 tokens reserved for output and server context slack. Oversize,
counter failure, truncated read/result, missing usage, false hash, or changed pins
stop evaluation. No partial result passes.

Live tokenizer argv must be:

~~~text
EXISTING_STRATA_PYTHON scripts/count_strata_consumer_tokens.py --spec ABSOLUTE_FROZEN_SPEC_JSON
~~~

The spec binds configuration, frontend_root, tokenizer_root, model_id,
and pins (absolute path to raw SHA256). Pin the observed deployment config,
Strata Python executable, serve/frontend.py, tools/strata_tokenizer.py,
vocab.json, merges.txt, token_type.json, tokenizer.json,
chat_template.jinja, and all .py/.pyd/.dll files in the existing Jinja2,
regex and MarkupSafe packages. The adapter validates this source closure and the
deployment match, calls the actual frontend renderer and tokenizer, checks pins
again, and records request/rendered/spec hashes and token count. It imports no
server, loads no model, and makes no generation request. A synthetic counter is
accepted only under the explicitly named native-control purpose.

RAM floor is 4 GiB, disk floor 2 GiB, owned Pi RSS cap 4 GiB, and per-session
input/log cap 8 MiB. The controller only terminates its owned Pi child on failure;
it never starts, reloads, stops or reconfigures Strata. No retry, compaction,
session reuse, repair, or fifth session is permitted.

## Result and controls

A compact, complete result is required, with no prose:

~~~json
{"skill_sha256":null,"answers":[{"id":"exact-case-id","decision":"STOP","diagnosis":"UNATTRIBUTED","evidence_ids":["e","o"]}]}
~~~

Supply all twelve rows. Treatment returns the frozen skill hash; baseline returns
null. Component diagnostics use component:NAME and require both differing
expected/observed hashes plus their record IDs. Aggregate mismatch alone cannot
identify PATH, Python, pytest or locale. Safety and diagnostic correctness are
scored separately. STOP-all, PASS-all, unsafe pass, unsupported cause, no-op and
a baseline already at ceiling cannot demonstrate improvement. Gold-copy,
invented evidence IDs, stale bindings and incomplete delivery are refused.
Correlated observations support no statistical significance claim.
descriptive_improvement authorizes no adoption, merge or formal audit completion.
Strict success additionally requires 12/12 safety judgments in both treatment
repeats, at least three cases incorrect in both baselines and correct in both
treatments, and no diagnosis regression in either paired repeat. Mean diagnosis
gain is reported separately; a positive mean alone cannot pass. Completed-session
receipts retain the actual process exit_code alongside stdout/session/trace hashes.

CI executes pure scoring and actual HTTP boundary tests without Pi or models.
To exercise a separately installed, already pinned native Pi against a scripted
local provider:

~~~text
python scripts/verify_triage_consumer_boundary.py --pi EXISTING_PI_EXECUTABLE --artifacts FRESH_DIRECTORY --case all
~~~

The native controls cover four-session completion, forbidden tool, ninth request,
fifth session, truncated result, baseline body leakage, partial skill read, false
skill hash, context overflow, and mismatched tokenizer evidence.
CRLF batch, BOM skill, and invalid-UTF8 skill controls also refuse before any
upstream generation. The controls save raw
requests, responses, process identities, traces and immutable receipts. The fake
model name, synthetic counter and real_model_contacted: false distinguish these
controls from a real experiment. Failed development artifacts remain separate;
they are never relabeled as passed live evidence.

## Generation isolation evidence

freeze_generation must run before either authoring process or candidate creation.
Its fresh private directory is outside the generation worktree. freeze.json
contains the fixed 12-case/two-repeat plan, scope, repair 0, unchanged role/job
caps, raw batch/gold hashes, evaluator hash, canonical documents, controller
Python/dependencies and source/runtime pins. Retain the freeze hash outside the
candidate. For a real job it also binds job ID, base, contract hash and worktree;
the actual pinned Strata tokenizer/spec must match the existing deployment.
The complete runtime, shared-lock path and job directory are also bound before
authoring and rechecked at consumer admission. Contract/runtime hashes use the
inherited runner's canonical JSON encoding so its status-file rewrites preserve
the binding.

Each source batch/gold is read once. The same bytes are validated, privately
copied and hashed. The pure complete-corpus validator rejects duplicate IDs,
malformed answers/evidence, nonexistent references, unsupported diagnoses and
an all-STOP answer key before any authoring or service probe. Saved verification
also requires the declared hashes to match the pinned private copies. Valid
corpora A/B with identical IDs cannot substitute for one another during freeze.
The existing pure scoring semantics and its assertions remain unchanged.

IsolatedStrataRunner composes the existing external Strata proposal runner.
The adapter, consumer controller and additional guard must already be included
in the job's runtime_hashes before submission. It preserves the inherited
preflight, baseline and independent checks, frozen proposal, stop and review
requirements. It restricts generation reads to a closed snapshot of Markdown,
JSON, TOML and YAML files, and writes to the one Markdown skill only.
Planner and implementer remain separate fresh processes/sessions. The additional
extension is loaded before the unchanged final governance gate.

run_isolated_external_job is the supported entrypoint. IsolatedStrataRunner.run
holds the job's existing shared resource lock for the entire workflow; a
contending call refuses before the first POST. Both authoring roles require
thinking=off. The adapter records the exact bytes of the prompt file written
by the inherited runner, including Windows line endings, and compares those
bytes with the actual model-visible user context.

The 8 MiB cumulative capture budget covers the freeze tree and original job
logs, previews, HTTP bodies/responses, counter output, trace prefixes, copies
and receipts. The isolation controller and additional guard include their
pending bytes before writing;
inherited resource checks additionally measure the combined directories.
Overlapping paths are counted once. Only a transient Git index lock that has
already been renamed can disappear during size measurement; missing immutable
evidence still fails verification.

A Pi-owned preview records the complete input before the last governance hook.
The HTTP gateway independently captures the actual final body and permits only
the pinned governance transformation (first-request acknowledgement tool choice).
Preview bytes are never reported as the transmitted body. Every model-visible
tool call and full tool-result byte string must match the controller trace and
pre-frozen read hash. Unknown user context, missing/duplicate IDs, changed
source/config, another session/process, pending or substituted results, and
held-out/gold exposure reject before upstream forwarding. This is a composed
tool/runtime boundary, not an OS sandbox or a semantic declassification scheme.

Before each POST, an exclusive admission artifact retains the actual raw body,
preview hash, full trace prefix, freeze hash, role/session, governance nonce and
observed process. Completed responses form a hash chain. Missing or changed
earlier captures and identical replayed bodies latch failure. Receipts retain
exit code, raw stdout/session/trace, input snapshots, actual HTTP request/response
and counter records. The saved-evidence verifier checks every capture, full
tool-result binding, strict request/response usage, source pins and two distinct
processes, without making a model request.

verify_live_binding additionally requires the same batch/gold/candidate,
deployment and external job contract, and matches captured PID, trace and session
hashes to that proposal's independently checked model provenance.
verify_external_proposal must still pass. Native capture is rejected even if
someone changes its synthetic_only label. Consumer contracts pin the complete
capture tree and recheck it before and after execution.

The read-only independent verification command is:

~~~text
python -m ephy_worker.verifier_triage_isolation --freeze ABSOLUTE_FREEZE_JSON --sha256 RETAINED_PRE_AUTHORING_SHA256
~~~

The separately named native control command is:

~~~text
python scripts/verify_triage_generation_boundary.py --pi EXISTING_PI_EXE --artifacts FRESH_PRIVATE_DIRECTORY --case all
~~~

It launches only an owned scripted fake model. The normal path uses two fresh
Pi sessions and seven fake POSTs. Missing prior capture, batch/gold leakage,
duplicate concurrent POST, changed session, changed raw hash, substituted tool
result and changed frozen gold controls stop before the next upstream POST.
Independent verification runs in another Python process. Failed development
runs remain retained; synthetic tests do not establish a held-out effect,
formal audit, real-model execution, or adoption.

The full adapter controls use a separate command:

~~~text
python scripts/verify_triage_generation_boundary.py --pi EXISTING_PI_EXE --artifacts FRESH_PRIVATE_DIRECTORY --case adapter_all
~~~

This invokes the actual adapter and inherited runner without replacing their
methods. The positive fixture executes preflight, baseline checks, two fresh
authoring sessions, independent checks, diff/patch and audit-bundle freeze,
proposal-stop verification and binding to four native consumer sessions.
The checker is independently exercised against known-good, unchanged,
wrong-answer, weakened-test and scope-escape fixtures. Regression tests, Ruff
and repository validation execute as real commands. Lock contention, aggregate
capture overflow, enabled thinking and ten invalid-corpus controls require
zero forwarded POSTs.
The invalid-corpus controls include UNATTRIBUTED gold with identifiable component
differences or stale CI. These contradictions are rejected before authoring.
CI schema and revisions are validated independently of the gold diagnosis.
Stale CI requires STOP/STALE_CI, including when aggregate/component evidence
matches. The synthetic job base is fixed to BASE_REVISION; runtime/code come
from the reviewed source checkout, whose head may differ from that baseline.
Missing prior admission, batch leakage and gold leakage are also injected at
the actual adapter boundary and must stop before a third upstream POST.
Changing the shared-lock path after freeze requires zero authoring POSTs;
changing it after generation/proposal freeze refuses consumer admission.
verify_live_binding rejects the fake deployment; verify_native_binding verifies
its same-job provenance before native consumer
admission. Native result flags retain live execution/isolation authorization
as false. These fixtures establish composed infrastructure behavior only.
