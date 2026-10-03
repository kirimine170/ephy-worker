# Existing Strata proposal runtime

This explicitly authorized bootstrap profile uses an already loaded Strata model
with the managed Pi runner. It supports one Markdown file per candidate and zero
repairs. The controller freezes the task, model roles, checks, environment and
budgets before execution. Codex bootstrap edits are attributed separately from
the subsequent Pi implementation.

The default owned llama router and formal audit profile retain their existing
behavior. The external profile requires `backend: external_strata`,
`provider_id: strata-local`, `review_mode: external_codex`, and all three
`model_roles` pinned to the actual API model ID. Use `tools/pi-local/strata-provider.ts`
and `tools/pi-local/strata-worker.md`; the existing governance gate must be the
last extension. The dedicated managed Pi `settings.json` must be pinned in
`runtime_hashes` and contain `retry.enabled: false` and `compaction.enabled: false`.
Missing pins, changed bytes, default settings or enabled automatic requests
reject the profile before a Pi process starts.

## Freeze the existing service

Discover the real loopback origin, engine PID and configuration path from the
running deployment before capture. An explicit controller command records them:

```text
python -m ephy_worker.strata_runtime --capture <private-identity.json> --base-url <observed-origin> --engine-pid <observed-pid> --configuration <observed-config>
```

Capture reads `/health` and `/v1/models` without redirects or environment proxies.
The identity binds their loaded model and context values, the exact listening
PID, process start times, executable SHA-256 values and configuration SHA-256.
It attests the observed deployment, not model weight bytes in GPU memory or the
actual KV cache representation. Changes invalidate the run. The adapter never
starts, stops, reloads or changes the existing service.

Supply this identity file as the runtime `models_ini`, and the same complete
identity as the observed model's `model_manifests` entry. Freeze the provider,
worker prompt, adapter, existing runner modules, managed context documents,
executables and configuration in `runtime_hashes`. The canonical policy, skill,
schemas and fixed checker remain outside the writable candidate. Use the existing
trusted submission API and `formal_runtime --job <job.json> --execute-authorized`.
Invocation without explicit user authorization is prohibited.

## Verifier identity diagnostics

Each verifier identity comparison appends a private `verifier-identity.jsonl`
record before any check can run. It retains the expected and observed identities,
the decision, measurement phase, checks hash, per-variable environment hashes,
normalized path hashes, file hashes and frozen-pin comparisons. Raw environment
values, paths, command arguments and exception messages are omitted. Failed
measurements retain their available components and exception type. A changed
file during hashing, missing diagnostic output or exhausted diagnostic log
budget stops the job. Identity mismatches remain failures.

Capture the same optional `observation` dictionary when freezing the expected
identity if component-level comparison is needed. Keep diagnostics outside
candidates and Git. Offline controls cover matching and differing child-process
environments, equivalent normalized paths, canonical mapping order, changed
arguments, invalid pins, concurrent file changes and diagnostic write failures.
These observations do not recover an unrecorded measurement from a historical
failed job or authorize its replay.

For a background execution, construct a fresh submission draft without
`verifier_identity`, then call `freeze_submission_identity(draft,
observation=observation)` inside the actual controller process before submitting
or executing any job. The returned spec copies the draft and freezes that
controller's environment, executable resolution, checks and pinned artifacts.
Keep the same controller environment for preflight, the bounded pilot and the
campaign. Freezing in a tool parent and launching a different controller can
produce a different inherited `PATH`.

The freeze function refuses an already frozen spec, an existing job and unknown
fields. It never runs from a verifier comparison and never updates a job's
expected identity. Changes after freeze still reject before independent checks.
An offline child-process regression freezes a distinct launch environment,
confirms it matches, then proves changed `PATH` or check arguments reject while
retaining the original expected identity. Historical failed jobs remain intact;
new execution requires a new draft and job.

## Verify and stop

The planner and implementer use distinct fresh Pi processes and sessions. The
canonical policy acknowledgement precedes task tools. Only the single allowed
Markdown path is writable. Shell, delegation, network tools, check changes,
commit, push and adoption are unavailable to the candidate agent. Provider
requests and output tokens are capped by the existing stage guard.

`max_requests` counts admitted generation calls, including the one currently in
flight. Health reads do not count. The N+1 generation call is rejected before
sending: the guard records the violation and exits only its own managed Pi
process with code 78. This native exit survives Pi's callback exception handling.
Requests are serialized, retry is disabled, and the supplied
`scripts/verify_pi_request_boundary.py --pi <existing-pi> --artifacts <fresh-private-dir>`
checks raw HTTP POST counts with the actual pinned Pi binary against a synthetic
server. Its controls cover caps 1/2/8, remaining output tokens, an in-flight
response, scope/model/identity failure, HTTP errors without hidden retries, and
evidence-storage failure before sending.
The guard removes Pi's competing `max_completion_tokens` field, which Strata
otherwise prioritizes over `max_tokens`. Raw POST controls check both fields,
including a later payload hook and a reduced remaining budget. A response whose
reported usage exceeds its admitted per-response cap terminates the owned Pi
process before any next request, even when the total stage budget remains.
This is transport evidence, not a real model or completed workflow.

An already admitted response can finish within its stage deadline. Timeouts kill
only the owned Pi command; they do not restart or stop Strata. Closing the client
connection does not attest cancellation of server-side work, so each request also
receives `max_tokens` bounded by its response cap and remaining output budget.
The stop module is pinned together with the provider and guard before execution.

The independent controller verifies the frozen baseline and candidate with all
five fixed checks, including regression, lint and repository validation. The
external checker has known good, unchanged, wrong answer, weakened test and scope
escape controls. It runs outside the measured repository, cannot repair the
candidate, and preserves exact commands, exit codes and hashes.

A successful candidate freezes the full pre-audit evidence bundle, then stops at
`external_review_pending`. It has no executed formal auditor and is unapplied.
Read-only verification of this transport boundary is available with:

```text
python -m ephy_worker.strata_runtime --verify <job.json>
```

The existing formal integration gate refuses this profile, even if its status
is manually relabeled. Separately authorized review transport can publish the
sanitized frozen patch. Adoption still requires matching current-head CI,
independent Codex Review with no unresolved P0/P1, and explicit human merge
approval. A new push invalidates previous CI and review. Raw logs, local paths,
authorization transcripts and private evidence stay outside Git.

## Named bootstrap adoption alignment draft

The [Proposed ADR](adr/0004-authorized-codex-bootstrap-adoption.md) maps the
user-authorized Codex infrastructure work to a separate, named bootstrap
evaluation proposal. It combines PR15 and PR16 for a main-targeted full-scope
review without changing their owner branches or automatically resolving old
findings. The proposal discloses that Pi did not implement this infrastructure
through the formal pre-audit workflow. Its scope is the original 40-file source
plus documentation alignment, totaling 42 files.

This is a draft policy alignment, not an active adoption exception, completed
formal audit, new runner state or permission for any candidate to merge. The
existing integration rejection and all profile limits remain. Source875c4ba CI,
review and live Markdown results keep their original head/policy/candidate
bindings. They are not current-head CI or review for this new draft, nor live
execution under changed governance hashes. New draft CI and independent review
must cover the full main-targeted source; any final bootstrap adoption decision
and merge are deferred. General self-improvement workflow gates are unchanged.

## Finite campaigns

Reuse `formal_campaign` with immutable drafts, the same kernel-held resource lock
and fresh worktrees. External Strata campaigns are capped at four wall-clock
hours and twenty jobs. The first complete independently verified cycle must pass
before further trials. Three consecutive failed candidates stop the campaign;
infrastructure, permissions, resource, budget, identity or scope failures stop
immediately without repair. A `STOP` file requests cancellation. Resume never
replays an interrupted job and retains the original absolute deadline.
An interrupted active Strata job is recorded as an infrastructure failure and
stops a resumed campaign even when an earlier trial succeeded.

Campaign plans contain fresh submission drafts without `verifier_identity`.
The standard `run_campaign` path freezes them inside its executing controller,
retains a separate immutable `verifier-freeze.json` bound to the plan and state,
then calls the draft-freeze API immediately before each submission and compares
the result with the original expectation. It rejects pre-frozen plan inputs and
any later environment, executable, pin or command drift before creating another
job. Private redacted `verifier-submission-<index>.json` records preserve these
comparisons, including rejected submissions with no job. Resume retains the
original expectations and absolute deadline; missing or changed freeze evidence
cannot be recovered by remeasuring. Legacy interrupted jobs are still accounted
without replay. Every terminal-state return, including resume of a completed,
stopped or failed campaign, also validates the retained freeze record. Missing
or modified evidence rejects rather than reporting the old terminal result;
the historical state and jobs remain unchanged. No new trial can proceed without
the bound freeze record.
Resume validates that record immediately after the plan binding, before processing
an active job, changing its status, appending results or saving campaign state.

Every successful trial remains an isolated external-review proposal. Repeated
successful trials are reliability measurements; they do not establish an
accumulating improvement, a completed formal audit or automatic adoption.
