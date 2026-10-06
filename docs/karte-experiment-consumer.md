# Karte experiment consumer v1

`ephy_worker.karte_experiment_consumer.consume_result` consumes the JSON returned
by Karte PR323's `prepare`，`publish`，or `status` commands．It is a local，read-only
library boundary for the explicitly synthetic `karte.worker-experiment.v1`
adapter．No real-model execution，Job transition，Karte invocation，canonical
write，skill discovery，patch application，or adoption is implemented here．

The caller supplies an independently retained candidate ID and complete payload
SHA256，the exact `karte.experiment-payload.v1` binding bytes，and all 24 immutable
artifact byte strings keyed by `logical_ref`．Do not obtain the expected hash
from the same untrusted status or receipt being checked．The producer binding is
under `.mdsys/ephy/experiment-producer/CANDIDATE.json`，and imported artifacts are
under `.mdsys/ephy/experiments/CANDIDATE/LOGICAL_REF`．The caller owns snapshot
acquisition and must use the existing producer's validated `status` command；a
raw receipt file is insufficient because receipt v1.1 has no proposal digest．

The consumer checks the candidate and payload pins，supported schema versions，
all imported byte counts and hashes，the complete ordered Worker inventory，
manifest/adapter identities，all shared record/adapter provenance fields，record
evidence and candidate patch hash．The observations must match the producer's
Worker result prefix followed by the original metadata observations．Inputs
must be bounded immutable bytes．Artifact paths in the Worker manifest are
never opened by this library．Malformed JSON，duplicate keys，missing evidence，
modified bytes，unknown phases and authority claims fail closed．This does not
replace the producer's archive/receipt association or Worker formal audit gates．

The frozen `Observation` retains a `ReviewTarget` with candidate，complete payload，
candidate patch，experiment，run，attempt and target commit identity．Persist it in
the caller's existing Job storage and pass it as `previous` on retry．Identical
observations return `duplicate=True`．Pending can progress to a receipt；terminal
receipt replacement and state regression are refused．Cancellation latches for
that candidate，including when a report receipt arrives later；the report fact
remains recorded and cancellation cannot become adoption．Missing evidence is
still an error on an otherwise identical retry．This library adds no separate
durable queue，scheduler or retry process．

`report_accepted` records receipt of a report．Worker `strict_pass` remains an
unverified label，and rejected，conflict，invalid and halted results grant no
authority．Every observation has `adopted=False` and `review_ready=False`．The
review target is evidence identity，not a positive review result．A later
integration operator must use the existing post-review Worker gate，bind its
reviewed patch/result to the same immutable review target，and obtain explicit
human authorization before adoption．This synthetic adapter supplies neither
formal audit provenance nor external-current-head review completion．

Example in a controller which already owns verified byte snapshots：

```python
from ephy_worker.karte_experiment_consumer import consume_result

received = consume_result(
    status_bytes, binding_bytes, artifact_bytes,
    candidate_id=retained_candidate_id,
    payload_sha256=retained_payload_sha256,
    previous=previous_observation,
    cancelled=cancel_requested,
)
observation = received.observation
```

The new tests use `unittest` and synthetic bytes only．They run without Pi，
Strata，network access or extra packages：

```text
python -m unittest discover -s tests -p test_karte_experiment_consumer.py -v
python scripts/validate_repository.py --check-sensitive-patterns
```

## Existing Job connection

`ephy_worker.karte_experiment_job.record_result` is an explicit post-run controller
entrypoint for an existing Worker Job schemaVersion 2．Only the canonical
`JOB_DIR/job.json` is accepted；a copied JSON file cannot supply a stopped status．
It checks the Job ID，base，
contract and original `audit-bundle` artifact bytes against the same consumer
binding．The frozen task artifact must contain the Job's exact contract，and the
standalone `candidate.patch` must match the bundle patch．Queued，running or
`audit_pending` Jobs
are refused．The canonical `job.json`，frozen
bundle，model runner and adoption gates are unchanged．The adapter never launches
Pi，contacts a model，or calls an integration operation．

Current bundle consistency is insufficient．The adapter also checks the original
`audit-input.json` and bundle-integrity bindings，including the manifest，all final
and control hashes，Job contract，models and verifier．The retained
`external-review.json` pins that audit input for external review；formal Jobs
require matching `audit-result.json` and `audit-execution-attestation.json`
bindings．Missing or stale freeze records are refused，including Jobs which
stopped before producing the required frozen evidence．These checks establish
evidence identity only；they do not replace the full external-review or formal
integration gates，or supply a positive audit decision．

The separate `JOB_DIR/karte-consumer/state.json` records `received → verified →
review_pending` with the immutable review target．This sidecar is consumer
progress，not formal workflow completion or a positive review decision．Rejected，
conflict，invalid，halted or failed Worker results end at `result_failed`；cancellation
latches at `cancelled`．Missing or changed evidence raises an error．An initial
invalid result records `verification_failed`；a failed retry preserves the last
validated `state.json` and records a separate content-addressed
`verification-failure-SHA256.json` diagnostic，bound to the raw status and prior
state hashes and stable error identity．Identical failed retries reuse that
diagnostic without growing or changing the sidecar．Restoring evidence allows the original duplicate receipt to remain
idempotent without losing cancellation or prior review progress．Every state
retains `adopted=false` and `review_ready=false`．
Identical retries revalidate evidence and leave saved files unchanged．

Payload，adapter metadata and raw status snapshots are retained without replacing
existing bytes．A separate nonblocking kernel lock serializes only this adapter's
calls．The existing model resource lock and any process are untouched．Links and
hard links are refused before writes．As with the existing filesystem helpers，
the trusted controller owns a stable Job directory during a call；this adapter
does not provide an OS sandbox against concurrent host path replacement．

The caller must use producer-validated status output and independently retained
candidate/payload pins．An explicit local command is available：

```text
python -m ephy_worker.karte_experiment_job --job JOB_DIR/job.json --status VALIDATED_STATUS.json --payload BINDING.json --metadata ADAPTER_METADATA.json --candidate-id RETAINED_ID --payload-sha256 RETAINED_SHA256
```

Use `--cancel` to latch a controller cancellation．An existing `cancel.request`
is also respected．No running user Job is modified as part of adapter tests．

This addition is separate from PR19's benchmark and transfer evaluation and the
existing real-model verifier-triage consumer．It changes no existing runner or
checker，and local self-checks are not independent audit or adoption approval．
