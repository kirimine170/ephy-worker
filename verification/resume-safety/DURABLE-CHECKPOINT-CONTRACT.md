# Durable checkpoint contract proposal (local research mode)

Status: design and independent synthetic fixture only. `--resume-job-id` and
`ResearchExecutor.run(existing_report=...)` must continue to return
`resume_checkpoint_unsupported`. No existing schema-0.1 report is a checkpoint.

## Evidence from the current pipeline

- `ResearchExecutor.__init__` creates a fresh `Budget`, candidate map and passage
  set (`research.py:47-63`). `run` starts `_execute` at planning (`:593-620`).
- Local mode runs plan, each search query, selection, each source fetch,
  extraction, review, then an optional second round (`:103-214`, `:216-423`).
  `examine` joins extraction and review; it needs a durable boundary between
  those calls (`:232-364`). Collector mode has different batch operations
  (`:424-511`) and is outside the first resume implementation.
- Model transport charges each actual request before dispatch (`models.py:70-81`).
  Search and fetch also charge before their requests (`search.py:145`,
  `fetch.py:303`). Budget stores counts, dedupe keys and elapsed time only in
  process memory (`budget.py`). `snapshot()` is exported only in final metrics.
- `JobStore.event` appends JSONL and `status` atomically replaces one file,
  but neither is a consistent state snapshot (`store.py:133-181`). `save`
  writes report, markdown, metrics, manifest and status in separate steps at
  job exit (`store.py:212-239`; `research.py:639-687`). `atomic_write` flushes
  Python buffers and renames one file but does not fsync a transaction. A crash
  between those writes may leave a mixed generation.

## Version 1 record and immutable identity

A checkpoint is a versioned, immutable generation plus an atomically selected
head. It contains all state needed for the next step, not merely a report path.
Unknown fields or versions fail closed. Minimum fields:

| Field | Required meaning |
| --- | --- |
| `schema_version`, `workflow_version`, `checkpoint_revision` | Decoder and exact state-machine revision; revision strictly increases. |
| `job_id`, `run_id`, `attempt_id` | Same durable job; unique originating run and one ID per accepted resume attempt. |
| `active_owner`, `fence_epoch` | `active_owner` is null or the one accepted attempt ID with its fencing token and process identity. The epoch increases on every acquisition and never resets. A revision bump alone does not release ownership. |
| `profile_id`, `input_sha256`, `target_revision`, `policy_revision` | Bind the exact model profile, canonical question + supplemental URLs + mode, code/workflow revision, and effective nonsecret config + limits + safety policy. Compare before provider construction or any job write. |
| `stage`, `round`, `cursor`, `next_operation_id` | One unambiguous next step, including query/source index, validated selection IDs, and deterministic IDs for evidence. Only enumerated boundaries are accepted. |
| `report`, `report_sha256`, `event_high_water` | Full canonical state and its digest; events are diagnostics, not replay authority. Verify all referenced source/passages/claims and IDs. |
| `budget` | Cumulative counts for all request types and limits, query/source dedupe keys, token usage, stage seconds, elapsed time, and an absolute deadline. Restore before any provider is made. Limits may not increase across attempts. |
| `operation` | `none`, or a durable intent with operation ID, kind, input digest, reserved budget, owner attempt ID, fencing token and state `inflight`. An uncommitted intent is never replayed automatically. |

The digest detects accidental corruption, not coordinated alteration of both
payload and co-located metadata. If adversarial tampering matters, use an
authenticated record with a key outside the job directory. Secrets and raw
environment values must not enter the checkpoint.

## Allowed restart boundaries

Persist a `ready` generation only after the complete result and cumulative
budget are included. In local mode, the first implementation can resume after:

1. validated plan, before query 0;
2. each completed search query, before the next query;
3. completed selection with fixed candidate IDs, before source 0;
4. each completed source fetch, before the next source;
5. validated extraction and evidence, before review;
6. completed review/round, before bounded follow-up or finalization.

The cursor must identify the next query/source and preserve initial versus
follow-up selection. A source ID or evidence ID already committed cannot be
reallocated. Search candidate normalization, source dedupe, passages selected
for extraction, review inputs, and repair-attempt count must be restored from
the generation or deterministically derived from its immutable contents.

Before **each** model/search/fetch operation, durably commit an `inflight`
intent and reserve its maximum allowed request charge. Only the active owner
holding the current fencing token may publish that intent or dispatch. The
owner check and intent publication must be serialized under the per-job lock;
the effect must remain associated with the committed intent and token. Only
then dispatch.
After a successful response, atomically publish its result, actual usage and
the next `ready` generation. If the process dies or a write fails after the
intent but before that publication, the operation outcome is unknown. Resume
must stop with `checkpoint_operation_uncertain`; it cannot issue the same
external call again merely because no result appears in the report. An
idempotency key honored by the external provider, or separately reconciled
evidence of the outcome, would be needed to clear that state. Local model,
search and fetch providers have no such proven facility today. A pre-intent
failure has no external effect and can retry from the preceding ready state.

## Atomicity, concurrency and budget

- Store a single canonical checkpoint payload for each immutable generation.
  Write to a new file, flush it to durable storage, then atomically advance
  the head under an exclusive per-job lock. The head includes generation hash
  and revision. Keep the previous generation until the new head is durable.
  The report/markdown/status files are projections and cannot authorize resume.
- Accept resume with a lock and compare-and-swap on the expected head revision
  **only when `active_owner` is null and no intent is in flight**. Record the
  new attempt and increment `fence_epoch` in the same durable head update.
  Every later mutation and external dispatch checks both owner attempt ID and
  token. A second simultaneous or repeated resume is rejected even if it
  reads the latest revision; revision alone cannot transfer ownership.
- The owner may release only after all its tasks and external calls have
  finished, no intent is in flight, and it has stopped scheduling new work.
  Release atomically clears `active_owner` under the same lock. A subsequent
  attempt receives a higher token; an old owner cannot publish or dispatch
  using its stale token. If the owner crashes without release, a trusted
  supervisor must verify that process has terminated and all local tasks are
  quiescent before clearing ownership. An expired lease or wall-clock deadline
  is **not** proof of termination. If a durable intent remains `inflight`,
  recovery still stops with `checkpoint_operation_uncertain` even after owner
  death. If the external service can accept work independently after the local
  process exits, safe takeover additionally needs provider-enforced fencing
  or reconciliation; a local token check alone cannot cancel that request.
- Restore cumulative request counts and dedupe keys exactly. Persist elapsed
  time plus an absolute deadline; process-local `time.monotonic()` cannot be
  restored. Remaining time is the minimum of the persisted allowance and
  deadline minus current time; clock ambiguity fails closed. Reserve the full
  possible request/retry count before dispatch. Never reset limits on resume.
- Verify job identity, profile, input hash, target/policy revisions, checkpoint
  schema/hash, stage/cursor, budget bounds, report references and active
  attempt ownership before constructing model/search/fetch clients. Old
  schema-0.1 reports, collector mode and terminal checkpoints stay unsupported.

## Fixed synthetic acceptance scope

`checkpoint-contract.test.mjs` is an independent reference fixture: no model,
network, Pi, credentials, or product code is invoked. It covers restart at a
committed boundary, double resume, active-owner rejection at the current
revision, wrong-owner dispatch/commit, old-owner fencing after a new owner,
trusted-stop recovery, stale identity/revisions, cumulative budget, failed
pre-intent publication and failed post-effect publication. The trusted-stop
token in the oracle represents evidence supplied by a privileged supervisor;
the fixture does not establish any real OS termination proof. It does **not**
prove the Python product implements this contract. Keep it fixed when wiring a
future product adapter; the existing refusal fixture remains the gate until
that adapter and an actual interruption/restart test pass. Preserve baseline
and candidate outputs under the same command and do not weaken hard checks.
