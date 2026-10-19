# Standalone plan completion checker

`python -m ephy_worker.plan_completion` checks whether one final Pi assistant
message contains a structurally complete plan bound to an externally frozen
contract. The checker uses only the Python standard library. It reads two input
files, emits one deterministic JSON report, and never executes contract commands,
calls a model, writes artifacts, or modifies a plan.

This is a new **standalone sidecar**, not a gate integrated into
`formal_runtime.py`. The existing planner-only prompt, contract, artifacts,
`planned_only` result, and nonempty-text completion behavior are unchanged.
Existing planner prose may therefore pass the legacy path and fail this checker.
Neither this result nor the new tests retroactively pass any old trial.

## Trust and assurance boundary

The caller must freeze the requirement contract outside candidate control and
supply its expected raw SHA-256 from a trusted source. Giving the checker a
candidate-controlled contract together with that candidate's hash does not
establish the intended requirements. Hash matching binds bytes; it does not
establish authenticity, model identity, source provenance, governance receipt,
authorized workflow, or audit approval.

A `complete` result establishes only the checks described below. Every report
contains `assurance: "structural_only"`,
`semantic_correctness: "not_assessed"`, and `checks_executed: false`.
The checker does not assess whether a hypothesis is sound, a criterion is useful,
a check can verify a criterion, references are substantively justified, or prose
obeys the prohibitions. Even a criterion of `"done"` is structurally nonblank.
Exact equality of the semantic scope text is an identity check, not semantic
analysis. The checker does not inspect proposed patches or filesystem contents.

Requirement extraction is the caller's trusted preparation/review responsibility.
Coverage is over the frozen requirement IDs; the checker does not automatically
extract or validate requirements from arbitrary original task prose.

Live model quality, independent verification, provenance, and audit remain
separate obligations. Future runtime integration requires a separately authorized
change to the pinned bootstrap, prompts, and contracts, with compatibility and
regression verification. A standalone report cannot replace those gates.

## Frozen contract: `ephy.plan-completion-contract.v1`

The input must be one strict UTF-8 JSON object with exactly these fields:

```json
{
  "schema": "ephy.plan-completion-contract.v1",
  "requirements": [{"id": "R1", "text": "Reject plans missing a required field."}],
  "allowed_files": ["src/checker.py"],
  "semantic_scope": "Check plan structure.",
  "prohibitions": [{"id": "P1", "text": "Do not execute checks."}],
  "checks": [{"id": "C1", "argv": ["python", "-m", "pytest", "-q"]}]
}
```

All arrays are nonempty. Every record has exactly the fields shown.
Requirements, prohibitions, and checks each have their own ID namespace.
IDs must match `[A-Za-z][A-Za-z0-9_.-]{0,63}` and be unique within their namespace.
Requirement and prohibition text, semantic scope, and each argv element must be
nonblank strings. An argv element cannot contain tab, CR, or LF.
Arguments are data only: the checker does not find, authorize, or run commands.

File paths use exact, case-sensitive, slash-separated relative names.
The checker rejects absolute paths, backslashes, colons, wildcards, Windows
reserved characters, empty or `.`/`..` components, surrounding component
whitespace, trailing dots, and Windows device basenames such as `CON.txt`.
Tabs and line breaks are forbidden. Duplicate paths and case-folding collisions
within one file list are rejected. Ordinary Unicode names are allowed; Unicode
normalization and filesystem canonicalization are not performed. There is no
existence, symlink, hard-link, or actual write-confinement check.

## Final Pi assistant message

`--response` takes the raw JSON bytes of one final Pi assistant message,
not a transcript, JSONL stream, provider delta, or outer event wrapper:

```json
{
  "role": "assistant",
  "stopReason": "stop",
  "content": [{"type": "text", "text": "<one JSON plan serialized as text>"}]
}
```

The role must be exactly `assistant`, and the stop reason must be exactly
`stop`. Missing or other stop reasons, including `length`, `max_tokens`,
`toolUse`, `error`, and `aborted`, never complete a plan, even with a
complete-looking body.

Every content block must be either:

- `{"type": "text", "text": "..."}`, with string text; or
- a known Pi `{"type": "thinking", "thinking": "..."}` block, with string
  thinking content.

There must be at least one text block. Thinking blocks are ignored as plan
evidence, to support normal Pi reasoning output. Thinking-only messages cannot
complete a plan. Malformed thinking blocks are invalid input.
All `toolCall` blocks and unknown block types are rejected, including a tool
call beside an otherwise valid plan.

The text blocks are concatenated in order **without separators or repair**.
A JSON token may span two text blocks. The checker does not strip fences,
extract embedded JSON, select the last object, append omitted delimiters, or use
reasoning as a substitute for final text. The concatenation must parse as one
complete JSON plan, allowing only ordinary JSON whitespace around it.

Other envelope fields, such as model, provider, usage, and timestamp, and other
block fields, such as textSignature, are ignored after generic JSON safety
validation. Metadata never supplies a missing top-level role, stop reason, or
plan. This specifically validates Pi content-block shape, not arbitrary foreign
provider fields such as `tool_calls`, or the validity of an entire transcript.
The caller must supply the original final message without silently stripping
unsupported blocks. The checker does not persist or echo raw reasoning.
The response hash binds the complete supplied message, including thinking and
ignored metadata; it does not prove where that message came from.

## Plan: `ephy.plan.v1`

The extracted object must have exactly these fields:

```json
{
  "schema": "ephy.plan.v1",
  "contract_sha256": "<64 lowercase hex digits for the raw frozen contract bytes>",
  "hypothesis": "Checking fields detects omissions.",
  "scope": {
    "files": ["src/checker.py"],
    "semantic_scope": "Check plan structure."
  },
  "prohibitions": ["P1"],
  "acceptance": [
    {"id": "A1", "requirement_ids": ["R1"], "criterion": "Missing fields are rejected."}
  ],
  "verification": [
    {"check_id": "C1", "acceptance_ids": ["A1"]}
  ]
}
```

The checker enforces:

- The plan's `contract_sha256` field equals both the supplied caller pin and the raw contract digest.
- Hypothesis and every acceptance criterion are nonblank strings.
- Scope files form a nonempty exact subset of the allowed files, and semantic
  scope equals the frozen text exactly.
- Every frozen prohibition ID occurs exactly once.
- Every acceptance has a unique ID and a nonempty, unique list of known
  requirement IDs. Every frozen requirement has acceptance coverage.
- Every mandatory check occurs exactly once, with a nonempty, unique list of
  known acceptance IDs. Every acceptance has verification coverage.
- Unknown fields, unknown references, and duplicate records or list entries are
  rejected.

References may recur across different records: one requirement can have several
criteria, and one acceptance can have several checks. IDs are scoped by record
kind. None of the matching uses case folding, trimming, or fuzzy comparison.

The exact text `NO_HYPOTHESIS`, after trimming surrounding Unicode whitespace,
is a separate non-success outcome. It is recognized only after a valid pinned
contract and final-message checks. The JSON string `"NO_HYPOTHESIS"`, embedded
mentions, and nonterminal messages are not that sentinel.

## Strict parsing and fixed bounds

The v1 implementation enforces these limits:

| Input or field | Maximum |
|---|---:|
| Raw contract | 256 KiB |
| Raw response | 1 MiB |
| Extracted plan, encoded as UTF-8 | 256 KiB |
| JSON object/array nesting | 32 |
| JSON nodes, including object keys | 32,768 per decoded input |
| Schema arrays, references, and content blocks | 256 items |
| Requirement/prohibition text, scope text, hypothesis, criterion | 8,192 characters |
| File path | 512 characters |
| ID | 64 ASCII characters |
| argv | 128 elements |
| argv element | 1,024 characters |
| JSON numeric token | 128 characters |

The CLI reads at most the applicable raw-byte limit plus one byte.
JSON nesting is checked before decoding; node traversal is iterative.
Duplicate keys anywhere, including ignored metadata, are invalid.
UTF-8 errors, BOMs, malformed JSON, fences, trailing data, NaN, Infinity, numeric
overflow to infinity, and over-limit input are rejected without repairing input.

All decoded JSON strings, including keys and ignored metadata, reject Unicode
control, format, and surrogate categories (`Cc`, `Cf`, `Cs`), except tab,
CR, and LF. This explicitly excludes bidi controls and zero-width formatting
characters. The permitted whitespace can occur in prose, but not in IDs, paths,
or argv elements. Normal Japanese text and ordinary Unicode characters are
accepted; there is no minimum prose length heuristic.

## API and result

```python
from ephy_worker.plan_completion import check_plan_completion

report = check_plan_completion(
    contract_bytes,
    response_bytes,
    expected_contract_sha256=trusted_contract_sha256,
)
```

Both inputs must be `bytes`. The expected digest must be 64 lowercase hex
characters. The API performs no I/O and returns a fresh dictionary. It checks
the contract first, then the envelope and extracted plan, and reports the first
failure with a stable diagnostic `code`, `path`, and explanatory `message`.

| Status | passed | CLI exit | Meaning |
|---|---|---:|---|
| `complete` | true | 0 | All structural checks passed |
| `incomplete` | false | 1 | Nonterminal/wrong-role/unsupported-block message or incomplete plan structure |
| `no_hypothesis` | false | 1 | Explicit final sentinel |
| `invalid_contract` | false | 2 | Bad caller pin, raw contract mismatch, invalid contract JSON or schema |
| `invalid_input` | false | 2 | Invalid response/plan encoding or JSON, malformed content, bounds, CLI arguments, or I/O |

Every result has schema `ephy.plan-completion-result.v1`, the assurance fields
described above, and `contract_sha256`, `response_sha256`, and
`plan_sha256`. The first two hash the supplied raw bytes. The plan digest hashes
the exact concatenated text re-encoded as UTF-8, including whitespace.
Hashes are null when unavailable, not yet extracted, or oversized; an oversized
CLI prefix is never reported as the full file's digest. An invalid but bounded
byte input can still have a hash. Sentinel text also has a plan hash.

CLI argument and input errors emit JSON to stdout with exit 2. `--help` prints
normal usage text. The module does not create output files; a caller can capture
stdout as a separate, hash-bound observation.

## Complete working example

Run this Python example in an installed environment, or with the repository
`src` directory on `PYTHONPATH`. It creates temporary demonstration input files
outside the repository and calls the read-only checker. In production the pin
must come from the externally frozen trusted contract, not from untrusted plan
output.

```python
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

contract = {
    "schema": "ephy.plan-completion-contract.v1",
    "requirements": [{"id": "R1", "text": "Reject missing fields."}],
    "allowed_files": ["src/checker.py"],
    "semantic_scope": "Check plan structure.",
    "prohibitions": [{"id": "P1", "text": "Do not execute checks."}],
    "checks": [{"id": "C1", "argv": ["python", "-m", "pytest", "-q"]}],
}
contract_bytes = encode(contract)
trusted_pin = hashlib.sha256(contract_bytes).hexdigest()
plan = {
    "schema": "ephy.plan.v1",
    "contract_sha256": trusted_pin,
    "hypothesis": "Checking fields detects omissions.",
    "scope": {"files": ["src/checker.py"], "semantic_scope": "Check plan structure."},
    "prohibitions": ["P1"],
    "acceptance": [{"id": "A1", "requirement_ids": ["R1"], "criterion": "Reject omissions."}],
    "verification": [{"check_id": "C1", "acceptance_ids": ["A1"]}],
}
message = {
    "role": "assistant",
    "stopReason": "stop",
    "content": [{"type": "text", "text": encode(plan).decode("utf-8")}],
}
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "contract.json").write_bytes(contract_bytes)
    (root / "response.json").write_bytes(encode(message))
    result = subprocess.run([
        sys.executable, "-m", "ephy_worker.plan_completion",
        "--contract", str(root / "contract.json"),
        "--contract-sha256", trusted_pin,
        "--response", str(root / "response.json"),
    ], check=False)
    assert result.returncode == 0
```

With already frozen files, use:

```text
python -m ephy_worker.plan_completion --contract /private/contract.json --contract-sha256 <trusted-64-lowercase-hex> --response /private/final-message.json
```

The targeted offline controls run with:

```text
python -m pytest -q tests/test_plan_completion.py
ruff check src/ephy_worker/plan_completion.py tests/test_plan_completion.py
```

The controls cover concise English and Japanese plans, each required field,
ack/read/tool-only messages, ordinary thinking compatibility, stop reasons,
all prefix truncations, forged/mismatched hashes, graph coverage, unsafe paths,
duplicates, nonfinite numbers, malformed types, UTF-8, controls, bounds,
nonexecution, and CLI exit behavior. These are implementation controls, not
evidence of real-model improvement or completed independent audit.

## Offline adapter for saved planner-only output

`python -m ephy_worker.plan_completion_adapter` adds a read-only adapter for the
current saved planner-only format. It does not connect a new gate to the runner,
change legacy success semantics, or rewrite historical evidence. Its separate
report is `ephy.planner-completion-evidence.v1`. Save the report outside the
job and capture directories.

The caller supplies the completion contract and these four fixed source files
inside `--job-dir`:

| File | Purpose |
|---|---|
| `job.json` | Job ID, stopped status, outcome, and full embedded runtime contract |
| `planner-session.jsonl` | Original Pi session records |
| `lead-plan.txt` | Original saved plan bytes |
| `planner-stop.json` | Stopped-boundary declarations and bindings |

The adapter requires three independent caller pins: the raw completion-contract
SHA-256, expected job ID, and canonical runtime-contract SHA-256.
It never derives trust pins automatically from the files it is checking.

```text
python -m ephy_worker.plan_completion_adapter --job-dir /private/job --contract /private/completion-contract.json --contract-sha256 <trusted-completion-hash> --job-id <trusted-job-id> --job-contract-sha256 <trusted-runtime-contract-hash>
```

The pure API accepts five raw byte strings and performs no filesystem, process,
or network actions:

```python
from ephy_worker.plan_completion_adapter import check_planner_only_output

report = check_planner_only_output(
    contract_bytes,
    job_bytes,
    session_bytes,
    plan_bytes,
    stop_bytes,
    expected_contract_sha256=trusted_completion_hash,
    expected_job_id=trusted_job_id,
    expected_job_contract_sha256=trusted_runtime_contract_hash,
)
```

### Source binding

The complete embedded `job["contract"]` is encoded using the current runtime's
canonical convention, independently of the raw formatting of `job.json`:

```python
(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
```

The adapter hashes those bytes and compares them with the caller pin. Production
code reproduces this convention without importing or executing the runtime.
Job ID must exactly match the caller's nonblank, single-line string of at most
256 characters, without surrounding whitespace. The contract must explicitly
set `planner_only` to Boolean true and `max_repairs` to integer zero.
Job status must be `planner_stopped`.

Completion-contract `allowed_files` must equal the runtime file set, with no
duplicates or unsafe lexical paths. `semantic_scope` must match exactly.
Check IDs and their argv arrays must match as an ID-to-argv mapping; argv token
boundaries, spelling, and order are significant. File order and check-record
order are not significant. Other runtime contract fields, including baseline
exit codes and the original task prose, remain bound by the full canonical
hash. This is a projection check, not a replacement for formal runtime contract
validation. The adapter does not infer or invent completion requirements or
prohibitions from legacy task text. Trusted preparation and review must provide
those in the separately frozen completion contract.

All nonblank physical JSONL lines are parsed with the core strict parser.
Every parsed record must be an object. Earlier malformed records, duplicate
keys, invalid UTF-8, nonfinite numbers, unsafe strings, and depth/node limit
violations cause rejection even if a later record looks valid.
LF separates physical lines; CRLF also works because CR is valid trailing JSON
whitespace. Only ASCII space, tab, and CR may make an otherwise empty line blank.
A complete final record without a final LF is accepted.

The last nonblank record itself must have `type: "message"` and contain an
assistant `message`. The adapter never searches backward for an older successful
assistant response. A trailing tool result, user message, or non-message record
causes rejection. All fields and content blocks of the selected message are
reserialized together and passed to the core checker. Neither thinking nor tool
blocks are removed from that input. Prefix records are syntax-checked; their
model declarations and transcript workflow are not authenticated.

`planner-stop.json` must have exactly these seven fields:

```json
{
  "job_id": "<same trusted job ID>",
  "contract_sha256": "<same canonical runtime-contract SHA-256>",
  "plan_sha256": "<SHA-256 of the original concatenated text encoded as UTF-8>",
  "snapshot_sha256": "<64 lowercase hex characters>",
  "implementation_started": false,
  "formal_audit_executed": false,
  "automatic_adoption": false
}
```

The three flags must be Boolean false, not numeric zero or a string. The
snapshot digest is format-checked and reported only: these four input files
contain no independent snapshot to verify against it. Its correctness and the
truth of the control declarations are outside this adapter's assurance.

The text blocks are concatenated exactly as in the runtime's text extraction,
without inserting separators. The core still checks the full original message
and rejects unsupported content. Empty text cannot establish a saved plan.
The original text hash must equal the stop file's plan hash.

There are exactly two acceptable `lead-plan.txt` byte representations for
original text `t`:

1. `t.encode("utf-8")`: reported as `exact_utf8`.
2. `t.replace("\n", "\r\n").encode("utf-8")`: reported as
   `windows_write_text`, reflecting Windows `Path.write_text` newline expansion.

The first representation wins if they are identical. The second replaces every
LF, even an LF already preceded by CR: original CRLF becomes CR-CR-LF, as with
the writer. No BOM removal, trimming, universal-newline decoding, partial
conversion, or other normalization is allowed. The checker always receives the
original text; its hash remains unchanged by the saved file representation.

Job outcome must be `no_hypothesis` exactly when original text, after surrounding
whitespace trimming, equals `NO_HYPOTHESIS`; otherwise it must be
`planned_only`. Trimming is used only for the sentinel test, never for hashes
or saved-byte matching.

### Adapter hashes, statuses, and limits

The report keeps these observations separate:

- `source_files`: raw byte hashes and lengths of the completion contract and
  all four saved files.
- `job_contract_sha256`: canonical hash of the full embedded runtime contract.
- `terminal_record_sha256`: hash of the original last nonblank line excluding
  its LF separator. For CRLF, its trailing CR remains included.
- `terminal_line`: one-based physical line ordinal, counting blank lines.
- `checker_input_sha256`: hash of the entire selected message reserialized
  with `ensure_ascii=False`, sorted keys, compact comma/colon separators, UTF-8,
  and no appended newline. This equals the nested checker's response hash when that hash is available.
- `original_text_sha256`: hash of the exact concatenated text, independent
  of JSON escaping and the saved Windows representation.

No raw plan, reasoning, session, or original task prose is echoed in the report.
Unavailable or oversized source hashes are null. The API reports the length of
the supplied bytes; CLI read failures return a report without a partial snapshot.

`source_bindings_valid` becomes true only after all specified local bindings
pass. `completed` becomes true only if those bindings and the core checker both
pass. The nested `checker` report is kept separately; its success alone never
makes the adapter report complete.

Core statuses retain their meanings and exit codes. Adapter source-format or
binding failures use `invalid_source` with exit 2. Bad arguments, I/O, or an
unstable read use `invalid_input` with exit 2. A bad completion contract uses
`invalid_contract`. Legacy plain prose or ack can have valid source bindings
but still fail the core JSON-plan format. `NO_HYPOTHESIS` remains exit 1 and
`completed: false`.

| Additional adapter input | Maximum |
|---|---:|
| Raw job JSON | 1 MiB |
| Raw session JSONL | 16 MiB |
| Physical session line | 2 MiB |
| Physical session lines, including blanks | 10,000 |
| Raw stop JSON | 64 KiB |
| Saved plan bytes, including possible Windows expansion | 512 KiB |

Core limits still apply to the completion contract, each parsed JSON record,
the reserialized checker input, and original plan text.

The CLI only opens the five caller-designated inputs. It does not follow
`jobDir`, worktree, checker, resource-lock, or other paths stored inside them.
It rejects nonregular files and file reparse points, uses bounded binary reads,
compares file identities and metadata before/after each read, reads all inputs
a second time with exact byte comparison, and rechecks their named identities.
A discrepancy fails closed without retrying for a more favorable snapshot.

These observations detect changes during the read window; they are not an atomic
multi-file snapshot or a lock against a writer deliberately changing and restoring
data between observations. They cannot attest future immutability or authenticate
the source. Use an independently frozen source snapshot when stronger guarantees
are required.

Every report states `structural_only`, `checks_executed: false`, and
`not_assessed` for semantic correctness, workflow validity, provenance, and formal
adoption. The adapter performs local consistency checks on declarations; it does
not establish model quality, a formal Pi workflow, audit attestation, authority
to adopt, or retroactive success of an old trial. Shared runtime, capture,
bootstrap pins, and the standalone checker remain unchanged.
On Python 3.12 Windows, path-stat and handle-stat ctime can have different
semantics. The adapter compares file ID, mode, size, and mtime across those APIs,
and retains ctime in before/after comparisons within each API and between path
observations across both passes. This avoids a false change diagnosis without
removing the ctime change controls.