# Resume safety review candidate

This is storage/review transport, not an accepted self-improvement proposal.
The repository owner explicitly allowed this draft despite incomplete Python regression evidence. No governance policy is changed by this branch. Do not merge or apply until the remaining gates are completed.

## Source identity

- Original audited baseline: `a3693671ba4e03788945b9441dc1fdb92aee8baa`
- Original worker patch SHA-256: `3f1905daa59d712fd14cb3f34904778a141ffef7c2b81c15bdde9ad022ed162c`
- Publication base: `0f6c3cb7a272bc67e2a0a0dc828e579df6823d98`
- The patch applies cleanly to this publication base; later coding/evaluation CLI additions are retained. This new combination has not passed full regression.

## Behavior and limits

Research resume is deliberately unsupported: reject mismatched identity, explicit empty IDs and unsafe existing reports before providers or writes. Reopened stores cannot enter the fresh-job execution path. Canonical store artifacts reject symlinks, junctions, nonregular files, multiple or unavailable link counts, and jobs inside Git checkouts. This is not a race-proof filesystem sandbox; the documented check-to-open race remains.

The checkpoint contract and JavaScript reference are proposals only. Their 11 scenarios model owner/fencing, budget, intent and recovery; they do not implement Python product resume or demonstrate crash durability. The runner timeout patch belongs to a separate local runner snapshot and is not part of this branch.

## Evidence

The audited source passed focused independent production-store probes with import doubles (60 invalid-path/entrypoint cases and an ordinary control), source/patch integrity checks and Python syntax parsing. The synthetic checkpoint oracle passed 11 cases. Those are not a substitute for the product pytest suite. Previous 312-pass/1-skip and 29-focused-pass results predate the final Git/hard-link guards and are not current acceptance evidence.

## Remaining validation

Use the repository's supported Python and locked dependencies. Run focused tests, the full offline regression, Ruff, repository validation and independent review against the exact final PR head. Run fixed audit fixtures from a separate checkout with `PYTHONPATH` pointing to the candidate `src` and `tests` directories; keep fixture bytes unchanged. Test native Windows junctions and hard links separately.

- `python -m pytest -q tests/test_resume_identity.py tests/test_store_open_existing.py tests/test_store_load_report.py tests/test_store_reparse_points.py`
- `python -m pytest -q verification/resume-safety/test_resume_contract.py verification/resume-safety/test_store_git_hardlink_boundary.py` (set candidate `PYTHONPATH` as above)
- `python -m pytest -q`
- `python scripts/validate_repository.py --check-sensitive-patterns`
- `node --test verification/resume-safety/checkpoint-contract.test.mjs`

No live model experiment, production adoption, merge or deployment has been performed.
