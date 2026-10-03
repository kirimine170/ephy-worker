# Strata campaign guide

Operating guide for the EXISTING Strata proposal campaign. The campaign produces reviewable proposals only; nothing is adopted automatically.

```json
{"backend": "external_strata", "review_mode": "external_codex", "max_trials": 20, "deadline_seconds": 14400, "max_repairs": 0, "max_consecutive_failures": 3, "adopted": false, "stop_on_infrastructure_failure": true}
```

## Preparation

- Freeze the baseline commit before any agent starts; every candidate worktree is created from this frozen base.
- Freeze the fixed checker, verification commands, and environment contract so baseline and candidate runs are compared under identical conditions. Never modify checks or evaluation definitions mid-campaign.
- Start each attempt from a fresh, clean worktree. If the worktree is dirty or based on the wrong commit, stop instead of continuing.

## Running

- Planner and implementer run as separate Pi processes and sessions. The planner plans only and never edits the candidate; the implementer edits only the allowed file scope.
- Independent verification runs the frozen checks after the implementer process exits. The verifier measures the candidate and never repairs it.
- Keep the campaign scoped to a single Markdown file per proposal (here, this guide). Out-of-scope edits fail the campaign rather than being accepted.

## Stopping

- Infrastructure failures — environment mismatch, invalid command, wrong model, missing evidence — stop the campaign; they are never converted into candidate repairs (`max_repairs: 0`).
- STOP honors the frozen controls: `stop_on_infrastructure_failure: true`, `max_consecutive_failures: 3`, `max_trials: 20`, and `deadline_seconds: 14400`.
- All evidence (patches, logs, hashes, stage records) is retained after a stop.
- Resume continues from retained state without replaying completed stages or overwriting prior evidence.

## Review

- The external Strata backend stops at `external_review_pending`; proposals stay unapplied (`adopted: false`) with no commit or merge performed by the campaign.
- A proposal becomes reviewable only with current-head CI and an independent Codex Review on that same head; any new push invalidates both.
- Explicit human approval is required before any merge. This guide does not claim a completed formal audit or `review_ready` status.
