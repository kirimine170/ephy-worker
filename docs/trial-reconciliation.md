# Full trial reconciliation sidecar

This offline sidecar checks a predeclared experiment plan against **all** submitted
BenchmarkRun records, original CodingResult records, and a separately committed
execution ledger. It never invokes a model, runs result commands, adopts patches,
or changes the existing benchmark/transfer contracts.

All examples and tests here are **synthetic**. Passing them is implementation
evidence, not evidence of live-model improvement or a formal Pi audit.

## Commitments and trust boundary

Two commitments are required; neither is optional or inferred by the verifier:

1. Before execution, an independent controller fixes a TrialPlan and retains
   `plan_sha256(plan)` outside the result-producing process.
2. A trusted recorder captures the actual original CodingResult and its
   BenchmarkRun adapter output at outcome collection. It adds
   `record_execution(run, result)` to an ExecutionLedger bound to the plan digest.
   When capture closes, the controller retains
   `execution_ledger_sha256(ledger)` outside the submitted evidence bundle.
   Scheduled trials that were never executed need explicit `not_executed`
   records with a nonblank reason; they must not be fabricated failures.

The recorder and controller must be trusted independently of the submitted
results. Rebuilding a ledger from untrusted result files immediately before
verification and accepting its newly calculated digest defeats this contract.
A plain hash does not authenticate its author or timestamp. An attacker who can
replace both trusted commitments and the evidence can fabricate a consistent
history; this module cannot detect that. The module does not install or integrate
a trusted recorder into an executor, prove physical execution, enforce budgets,
or attest the chronology. A runner/controller integration retaining these
commitments is required before claiming execution assurance.

There is no metadata-only success fallback. `ok` and
`record_integrity_verified` mean exact record agreement relative to the two
trusted commitments. `execution_verified` is always false: supplied evidence
alone is not an execution attestation. An actual recorded profile failure can
have validation unexecuted while still being a fully accounted outcome.

## Independent module and existing contracts

`ephy_worker.trial_reconciliation` reuses:

- `benchmark.Conditions`, `BenchmarkRun`, `fingerprint`, and `compare_runs`;
- original `CodingResult` with strict validation;
- `transfer_evaluation.comparison_plan` through `make_transfer_trial_plan`.

The sidecar adds no fields to those existing models and changes no existing CLI
or checker. Plans contain run IDs, task/repeat, both labels, evidence kind, axis,
and every condition hash including budget. A plan must have at least two groups,
the same task/repeat grid, unique IDs/trials, matched fixed conditions and one
distinct variable configuration per group. Empty/blank IDs and suite IDs,
duplicate JSON keys, invalid types, and nonfinite result numbers are rejected.
The integer schema version rejects booleans, floats and strings at raw ingestion;
callers must not normalize untrusted JSON through a permissive model first.

`make_transfer_trial_plan` preserves transfer ordering and variant rotation,
assigns IDs before results exist, and stores a source-plan hash. Use those
assigned IDs with the executor's existing `run_id` argument. A valid plan ID
for this adapter should also satisfy the executor/store's run-ID rules. An
independent user-selected plan ID distinguishes otherwise identical experiments.

## Synthetic API example

```python
from ephy_worker.trial_reconciliation import (
    ExecutionLedger, ExecutionRecord, execution_ledger_sha256,
    plan_sha256, reconcile_trials, record_execution,
)

# Controller phase, before execution; plan already contains all scheduled IDs.
pinned_plan = plan_sha256(plan)

# Trusted recorder phase. Each run comes from unchanged benchmark.make_run(...)
# over the corresponding original result, with the frozen plan metadata.
ledger = ExecutionLedger(
    plan_sha256=pinned_plan,
    records=tuple(record_execution(run, result)
                  for run, result in zip(runs, original_results, strict=True)),
)
pinned_ledger = execution_ledger_sha256(ledger)

# Retain both pinned values independently. Submit the four evidence documents.
report = reconcile_trials(
    plan, runs,
    expected_plan_sha256=pinned_plan,
    ledger=ledger,
    raw_results=original_results,
    expected_ledger_sha256=pinned_ledger,
)
```

For a scheduled trial that was not started, the trusted recorder adds:

```python
ExecutionRecord(
    run_id=scheduled_run_id,
    state="not_executed",
    reason="Synthetic control: trial deliberately not started",
)
```

That record has no result hashes. It makes reconciliation incomplete and
`comparison=None`, even if someone submits a forged successful record using the
same ID. Truly recorded failed/cancelled outcomes use `state="recorded"` and
remain in the existing comparison denominator.

## CLI and hash contracts

Save UTF-8 JSON as `plan.json` (TrialPlan), `records.json` (BenchmarkRun array),
`original-results.json` (CodingResult array), and `ledger.json`
(ExecutionLedger). Avoid distributing raw results beyond the authorized evidence
location: original results can contain local paths. The report emits only
IDs, hashes, field names, counts and the existing descriptive comparison.

```text
python -m ephy_worker.trial_reconciliation plan.json records.json original-results.json ledger.json --plan-sha256 <independently-pinned-plan-digest> --ledger-sha256 <independently-pinned-ledger-digest>
```

Exit 0 means exact complete evidence agreement; exit 1 means missing, extra,
duplicate, not-executed or mismatched evidence; exit 2 means invalid input or a
commitment mismatch. No failing path produces partial statistics.

These hashes are different contracts:

| Value | Exact input |
|---|---|
| `plan_sha256(plan)` | Canonical JSON of fully validated TrialPlan |
| `source_plan_sha256` | Canonical JSON of the original transfer comparison plan |
| `BenchmarkRun.result_sha256` | Canonical JSON of the entire original CodingResult |
| `ExecutionRecord.record_sha256` | Canonical JSON of the entire BenchmarkRun |
| `execution_ledger_sha256(ledger)` | Canonical JSON of `{"kind":"trial-execution-ledger-v1","ledger":<validated ledger>}` |
| File SHA-256 | Exact file bytes, for artifact transport/audit only |

Do not substitute a raw JSON file hash, transfer source-plan hash or result hash
for a controller commitment. Whitespace and object-key order are irrelevant to
canonical hashes but matter to file hashes. List ordering is retained. Ledger
order is part of its commitment; submitted results may be reordered without
changing the comparison.

## Reconciliation and synthetic controls

The sidecar checks both/all-condition simultaneous omissions, missing whole
tasks, zero results, duplicates before indexing, balanced extras, labels, IDs,
repeat numbers and every condition against the predeclared plan. Every planned
ID also needs exactly one ledger record; every recorded execution needs exactly
one original result. It rejects ledger replay from another plan and requires an
unchanged independently retained ledger digest.

For each result it recomputes the original canonical hash and compares run ID,
task ID, model profile, status, validation outcome, exit code, wall time and
failure code/stage to the BenchmarkRun projection. Full-record commitments also
bind fields outside that projection. Same-ID content changes, original-result
changes, missing raw results and unrecorded successful rows cannot pass.

Only complete agreement calls unchanged `compare_runs`. Failures/cancellations
and validation-not-run counts retain the existing semantics; no model ranking,
causal inference, adoption decision or success-rate threshold is added.
Synthetic regression controls include the original metadata cases, trusted
commitments fixed before adversarial mutations, raw/summary inconsistencies,
explicit nonexecution, malformed plans and cross-contract hash confusion.

```text
python -m pytest -q tests/test_trial_reconciliation.py tests/test_benchmark.py tests/test_transfer_evaluation.py
python -m ruff check src/ephy_worker/trial_reconciliation.py tests/test_trial_reconciliation.py
python scripts/validate_repository.py --check-sensitive-patterns
```
