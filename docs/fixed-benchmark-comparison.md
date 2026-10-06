# Fixed-condition coding benchmark comparison

## Scope

This offline tool summarizes existing `CodingResult` records．It reuses the existing
model profiles，coding fixtures，smoke suite and repeat runner．It does not replace
Manager／RemoteCollector，run a model，rank models，apply patches，or connect to a
user computer．The checked-in examples are synthetic，not performance results．

This change is ordinary cloud development requested by the user．It is not a
formal managed Pi self-improvement run：the prescribed gpt-oss planning，Qwen
implementation，runner attestation and fresh gpt-oss audit were not performed．
Passing the tests below must not be reported as formal `review_ready` or adoption．
The repository governance policy is unchanged．Publication is a separate decision．

## Freeze inputs before collecting results

For each task，freeze the exact suite and task JSON，checker bytes，environment，
budget and variant．Use `fingerprint` for canonical JSON and SHA-256 of raw bytes
for an external checker．Keep external checker files outside model-writable paths．
The existing independent self-improvement checker remains a separate gate；a
successful editable test alone is not proof that a candidate met requirements．

Use `make_run` with an existing `CodingResult`，the matching `CodingModelProfile`
and `CodingFixture`，and these keyword arguments：

- `suite_id`，`suite_sha256`，`checker_sha256`：fixed task-set and acceptance identity．
- `environment`：nonempty string keys `platform`，`python`，`dependencies`，`executor`．
  Record actual hardware／OS，interpreter version，dependency-lock digest，runner
  code digest；add Pi／server versions，inference settings and endpoint-to-host
  mapping where relevant．Do not include credentials or private logs．
- `budget`：`timeout_seconds` must match the fixture．Include every configured
  request／output-token／tool／retry limit and scheduling/concurrency policy．
  Explicitly represent absent limits as null；this tool does not enforce them．
- `variant`：canonical configuration of skill／tool／MCP exposure，including
  content hashes and permitted tools．`variant_label` is the display/group name．
- `repeat_index`：one-based trial ID，fixed in advance and complete for each group．
- `evidence_kind`：`synthetic` for invented control data，`mock` for deterministic
  FakePiRunner execution，`live` only for an actual observed model execution．

The adapter checks task/model identity and avoids retaining raw logs，paths，
endpoints and credentials．It hashes the complete original result for traceability．
Hashes bind the supplied metadata，not its truth：this does not observe the model
process，attest the checker，prove budget enforcement，or recover omitted settings．
Keep the original result and frozen manifests outside the repository when needed．
Do not relabel an old unbound result as a controlled measurement．

## Comparison rule

`compare_runs(records, axis="model")` allows the entire model configuration to
vary intentionally．It holds suite，task，checker，environment，budget and variant
fixed for each task．Model ID，provider，quantization，context window，reasoning
level and Pi arguments are fingerprinted together，so this compares configurations，
not a causal model-weights-only experiment．Profile labels and endpoint paths are
not configuration identity；backend/hardware changes belong in environment．

`axis="variant"` varies skill/tool configuration while also holding the model
configuration fixed．Every group must have exactly the same task/repeat grid．
The grid is derived from supplied records，not an independently frozen execution
plan．Dropping the same trials from every group cannot be detected here：the output
explicitly labels coverage as supplied-records-only．A caller must reconcile it
with the predeclared full plan before claiming experiment completion．
Duplicate run IDs，duplicate trials，missing coverage，configuration drift within
a label，identical configurations under different labels，mixed evidence kinds，
nonfinite metrics and malformed success records are rejected．Success requires
validation passed，exit code zero and no failure；unknown or nonzero exit cannot
become successful evidence．Unexecuted validation
is counted separately；environment failures stay in the pipeline denominator and
are classified by failure stage/code，not presented as model correctness failures．

Output remains per task：pipeline success rate，all-run median duration，successful
run median duration，unexecuted validation count and failure counts．There is no
weighted score，ranking，significance test，or automatic adoption decision．A fast
failure does not become a fast successful run．Different task environments are not
pooled into a misleading overall score．No causal skill benefit can be inferred
until the same actual model completes the controlled and held-out tasks repeatedly．

## Reproduce without model access

From repository root with the existing dependencies installed：

```sh
PYTHONPATH=src python -m unittest discover -s tests -p test_benchmark.py -v
PYTHONPATH=src python -m ephy_worker.benchmark tests/fixtures/benchmark/synthetic-runs.json --axis model
python scripts/validate_repository.py --check-sensitive-patterns
```

The CLI reads only the supplied JSON array and prints a summary．Malformed or
incomparable input returns exit 2 and a stable error class，without exposing input
contents．No command from a record is executed．The Pydantic model exposes a closed
JSON Schema via `BenchmarkRun.model_json_schema()`．It is an additive sidecar；the
existing CodingResult schema and ordinary evaluation output are unchanged．

For actual mock pipeline results，reuse `run_evaluation_suite(load_suite("smoke"),
profile, output_dir, repeat=N)` and then wrap its results using the frozen metadata．
For real models，the same runner can be used only in the authorized execution
environment．Real throughput，memory，model quality，Windows behavior and transfer
benefit are not verified by this cloud-only work．
