# Remote execution boundary

## Current state

This repository defines the responsibilities and security requirements of an Ephy remote worker node．The remote execution service itself is not implemented in this setup．`ephy-runtime/apps/worker/cli.py` is an existing local CLI and is not this remote node．

## Authorized job protocol requirements

- Every job carries a versioned request ID，declared capability，bounded inputs，deadline，and result destination．
- The worker accepts only explicitly allowed job types and rejects unknown fields or capabilities．
- Inputs are integrity-checked，size-limited，and stored only for the minimum execution lifetime．
- Results contain machine-readable status，logs with sensitive values removed，and artifact metadata．
- Retry and cancellation behavior must be idempotent and observable．

The transport，authentication mechanism，and queue implementation remain design decisions and require an ADR before implementation．

## Security requirements

- Use mutual authentication and least-privilege worker credentials．
- Do not send an entire `ephy-private` repository，raw conversations，Karte production data，model weights，or camera master images．
- Execute jobs in a constrained environment with explicit CPU，memory，storage，network，and duration limits．
- Keep control-plane authorization separate from untrusted job payloads．
- Return only the minimum required result and audit metadata．

## Completion boundary

Implementation work begins only after request／response schemas，authentication，authorization，artifact transfer，cancellation，audit，and failure semantics are accepted．
