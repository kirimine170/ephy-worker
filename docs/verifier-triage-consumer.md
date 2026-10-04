# Read-only verifier triage consumer

This is Codex-authored evaluation infrastructure, not a Pi-generated implementation,
formal audit, accepted proposal or adoption decision. Its baseline is main
26ddbe65a9bd9fe530dca13238c2eb3838f3aee5. Public development fixtures contain no
held-out data. Real model generation is a separately authorized operation.

The fixed_trial path is currently blocked before consumer launch. Existing
external-review bundles do not contain a pre-authoring held-out/gold freeze or
closed capture of actual generation HTTP inputs. This version cannot prove that
the candidate was authored without exposure to that data. No flag or supplied
success statement enables live trials: the capture workflow must first be
integrated, tested, bound to the generation job, and independently reviewed.
The working native_controls path uses only public synthetic fixtures. Pure
scoring does not attest generation isolation or a held-out effect.

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
generation_job. The existing identity must match the generation job. Canonical
documents must match the job's frozen governance root byte for byte. Both
generation and consumers use the same resource lock.
Generation proposal caps must be at most 8 requests, 2200 output tokens, 1024
response tokens, 300 stage seconds and 900 job seconds with repair 0; a larger
existing proposal cannot be relabeled with smaller reservations. These checks
do not replace the blocked pre-authoring isolation gate.

Once the blocked capture gate is implemented and independently reviewed, and
explicit execution authorization is given, the intended command is:

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
