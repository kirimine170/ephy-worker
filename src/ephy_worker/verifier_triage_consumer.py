"""Bounded read-only Pi consumers; no Strata lifecycle operation or adoption.

Contracts, gold and logs are controller-owned and outside model-visible roots.
Only explicit --execute-authorized launches consumers. Synthetic controls use a
separately named fake service and cannot be labeled a live-model experiment.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

import httpx
import psutil

from .formal_runtime import (
    GateFailure,
    command_environment,
    exclusive_lock,
    final_assistant_text,
    stage_evidence,
    stop_tree,
)
from .strata_runtime import verify_external_proposal, verify_identity
from .verifier_triage_evaluation import (
    BASE_REVISION,
    BATCH_PATH,
    MAX_REQUESTS,
    OUTPUT_TOKENS,
    RESPONSE_TOKENS,
    SKILL_PATH,
    STAGE_SECONDS,
    FrozenTrial,
    InvalidEvaluation,
    SuppliedSession,
    canonical_consumer_input,
    check_context_fit,
    evaluate,
    evaluator_sha256,
    sha256,
    validate_session,
)

CANONICAL = (
    "docs/system-development-governance.md",
    "docs/self-improvement-mvp.md",
    ".agents/skills/ephy-worker-self-improvement/SKILL.md",
    ".agents/skills/ephy-worker-self-improvement/references/eval-contract.md",
    ".agents/skills/ephy-worker-self-improvement/references/audit-contract.md",
    ".pi/prompts/audit-ephy-worker.md",
)
PROMPT = (
    "This is a read-only diagnostic consumer, not a proposal implementation or formal audit. "
    "Acknowledge canonical governance first. Read inputs/triage-batch.json completely. "
    "Use only exact read paths; no search, shell, writes, network or delegation. "
    "For each of all twelve cases return decision PASS or STOP and one diagnostic code. "
    "Name component:NAME only when expected and observed component hashes both exist and differ. "
    "Aggregate mismatch alone is UNATTRIBUTED; identical evidence is MATCH; mismatched CI revisions "
    "are STALE_CI. Evidence IDs refer only to expected/observed records present in that case. "
    "Return a single complete JSON object, no prose, markdown, omissions or truncation: "
    '{"skill_sha256":null_or_supplied_hash,"answers":'
    '[{"id":"exact case id","decision":"PASS or STOP","diagnosis":"code",'
    '"evidence_ids":["expected record id","observed record id"]}]}. '
)


def require(value, reason):
    if not value:
        raise InvalidEvaluation(reason)


def write_new(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def validate_generation_budget(job):
    require(job["baseRevision"] == BASE_REVISION, "Wrong generated candidate base")
    contract = job["contract"]
    require(contract["allowed_files"] == [SKILL_PATH] and contract["max_repairs"] == 0,
            "Wrong generation scope/repair budget")
    for key, cap in (("max_requests", 8), ("output_token_budget", 2200),
                     ("max_response_tokens", 1024), ("stage_seconds", 300), ("timeout_seconds", 900)):
        require(type(contract.get(key)) is int and 0 < contract[key] <= cap,
                "Generation reservation exceeded: " + key)


def live_generation_isolation_gate():
    # Existing external-review bundles prove generation/verification provenance,
    # but contain no pre-authoring held-out freeze or closed actual HTTP input
    # capture. Do not substitute a final skill ID scan for that missing evidence.
    raise InvalidEvaluation(
        "Live trial blocked: pre-authoring held-out/gold freeze and actual generation input isolation "
        "capture must be integrated and independently verified before enabling fixed_trial"
    )


def snapshot(root):
    require(not root.is_symlink() and not root.is_junction(), "Linked consumer input root")
    result = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink() and not path.is_junction(), "Linked consumer input")
        if path.is_file():
            require(path.stat().st_nlink == 1, "Hard-linked consumer input")
            data = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {"bytes": len(data), "sha256": sha256(data)}
    return result


def intact(contract):
    for name, expected in contract["pins"].items():
        path = Path(name)
        require(not path.is_symlink() and not path.is_junction() and path.is_file(), "Missing/linked frozen artifact")
        require(sha256(path.read_bytes()) == expected, "Frozen artifact changed")
    require(contract["dependency_versions"] == {
        name: version(name) for name in ("httpx", "psutil", "jsonschema")
    }, "Controller dependency versions changed")


def resource_gate(contract, directory):
    require(psutil.virtual_memory().available >= contract["minimum_free_ram_bytes"],
            "Consumer RAM floor")
    require(psutil.disk_usage(str(directory)).free >= contract["minimum_free_disk_bytes"],
            "Consumer disk floor")


def validate_contract(contract):
    intact(contract)
    require(contract["schema"] == "ephy.triage-consumer.v1", "Wrong schema")
    require(contract["purpose"] in {"fixed_trial", "native_controls"}, "Wrong purpose")
    require(type(contract["stage_seconds"]) in (int, float)
            and 0 < contract["stage_seconds"] <= STAGE_SECONDS, "Wrong stage deadline")
    require(contract["max_requests"] == MAX_REQUESTS
            and contract["budgets"] == budget_reservations(), "Wrong session/request budgets")
    for key, maximum in (("max_log_bytes", 8388608), ("max_process_rss_bytes", 4294967296)):
        require(type(contract[key]) is int and 0 < contract[key] <= maximum, "Invalid resource cap")
    require(contract["minimum_free_ram_bytes"] >= 4294967296
            and contract["minimum_free_disk_bytes"] >= 2147483648, "Resource floors weakened")
    frozen = FrozenTrial(**contract["frozen"])
    require(contract["controller"] == {"executable": str(Path(sys.executable).resolve()),
                                       "python_version": sys.version}, "Controller Python changed")
    require(frozen.base_revision == BASE_REVISION and frozen.evaluator_sha256 == evaluator_sha256()
            and frozen.prompt_sha256 == sha256(PROMPT.encode()), "Stale source/prompt")
    private = Path(contract["private_root"])
    canonical_consumer_input((private / "batch.json").read_bytes())
    canonical_consumer_input((private / "skill.md").read_bytes())
    require((sha256((private / "batch.json").read_bytes()),
             sha256((private / "gold.json").read_bytes()),
             sha256((private / "skill.md").read_bytes()))
            == (frozen.batch_sha256, frozen.gold_sha256, frozen.skill_sha256), "Wrong frozen inputs")
    if contract["purpose"] == "native_controls":
        require(contract["identity"]["model_id"] == "synthetic-triage-consumer-fixture"
                and contract["identity"]["listener"]["pid"]
                == contract["identity"]["engine"]["pid"] == os.getpid(), "Not an owned fake provider")
    else:
        live_generation_isolation_gate()
        job = json.loads(Path(contract["generation_job"]).read_bytes())
        verify_external_proposal(Path(contract["generation_job"]))
        require(contract["resource_lock"] == job["runtime"]["resource_lock"], "Wrong shared resource lock")
        require(contract["identity"] == json.loads(Path(job["runtime"]["models_ini"]).read_bytes()),
                "Consumer uses a different deployment")
        require(sha256((Path(job["worktreePath"]) / SKILL_PATH).read_bytes()) == frozen.skill_sha256,
                "Generated candidate changed")
    intact(contract)


def budget_reservations():
    stage = {
        "max_requests": MAX_REQUESTS, "output_token_budget": OUTPUT_TOKENS,
        "max_response_tokens": RESPONSE_TOKENS, "stage_seconds": STAGE_SECONDS,
    }
    return {
        "preflight": {"model_requests": 8, "seconds": 300, "stage": stage,
                      "description": "one separate development-only ceiling session; not run here"},
        "generation": {"attempts": 1, "repairs": 0, "job_seconds": 900,
                       "planner": stage, "implementer": stage, "max_requests": 16,
                       "description": "existing external Markdown proposal; not launched by this driver"},
        "consumers": {"sessions": 4, "max_requests": 32, "case_observations": 48,
                      "stage": stage, "max_stage_seconds_sum": 1200},
    }


def build_contract(
    repository: Path, pi: Path, identity: dict, batch: Path, gold: Path, skill: Path,
    directory: Path, token_argv: list[str], *, purpose="fixed_trial",
    generation_job: Path | None = None, stage_seconds=STAGE_SECONDS,
) -> Path:
    """Freeze an already generated candidate; never read gold into a model root."""
    require(purpose in {"fixed_trial", "native_controls"}, "Unknown purpose")
    token_argv = [str(Path(arg).resolve()) if Path(arg).is_file() else arg for arg in token_argv]
    require(not directory.exists(), "Evidence directory must be fresh")
    require(type(stage_seconds) in (int, float) and 0 < stage_seconds <= STAGE_SECONDS,
            "Invalid stage deadline")
    if purpose == "native_controls":
        require(identity["model_id"] == "synthetic-triage-consumer-fixture", "Not a fake model")
        require(identity["listener"]["pid"] == identity["engine"]["pid"] == os.getpid(),
                "Synthetic server must belong to this controller")
    else:
        require(generation_job is not None, "Live trial requires bound external proposal")
        job = json.loads(generation_job.read_bytes())
        validate_generation_budget(job)
        live_generation_isolation_gate()
        require(skill.resolve() == (Path(job["worktreePath"]) / SKILL_PATH).resolve(),
                "Skill is not the generated candidate")
        verify_external_proposal(generation_job)
        require(identity == json.loads(Path(job["runtime"]["models_ini"]).read_bytes()),
                "Consumer uses a different deployment")
        require(all((repository / name).read_bytes()
                    == (Path(job["runtime"]["governance_root"]) / name).read_bytes()
                    for name in CANONICAL), "Generation and consumer governance differ")
        require(len(token_argv) == 4 and token_argv[2] == "--spec"
                and Path(token_argv[1]).resolve()
                == (repository / "scripts/count_strata_consumer_tokens.py").resolve(),
                "Live consumers require the pinned Strata tokenizer adapter")
        token_spec = json.loads(Path(token_argv[3]).read_bytes())
        require(token_spec["model_id"] == identity["model_id"]
                and Path(token_spec["configuration"]).resolve()
                == Path(identity["configuration"]["path"]).resolve()
                and token_spec["pins"][token_spec["configuration"]]
                == identity["configuration"]["sha256"], "Tokenizer deployment mismatch")
    verify_identity(identity)
    canonical_consumer_input(batch.read_bytes())
    canonical_consumer_input(skill.read_bytes())
    directory.mkdir(parents=True)
    canonical = directory / "canonical"
    canonical_manifest = {}
    for name in CANONICAL:
        data = (repository / name).read_bytes()
        write_new(canonical / name, data)
        canonical_manifest[name] = {"sha256": sha256(data), "bytes": len(data)}
    policy = canonical / CANONICAL[0]
    private = directory / "private"
    for name, path in (("batch.json", batch), ("gold.json", gold), ("skill.md", skill)):
        write_new(private / name, path.read_bytes())
    required_runtime = {
        "pi": str(pi.resolve()),
        "provider": str(repository / "tools/pi-local/strata-provider.ts"),
        "stage_guard": str(repository / "tools/pi-local/formal-stage-guard.ts"),
        "stage_stop": str(repository / "tools/pi-local/formal-stage-stop.ts"),
        "extra_guard": str(repository / "tools/pi-local/triage-consumer-guard.ts"),
        "governance_gate": str(repository / ".pi/extensions/governance-gate.ts"),
        "runner": str(repository / "tools/pi-local/windows/run-background-job.ps1"),
        "runner_test": str(repository / "tools/pi-local/windows/test-background-job-runner.ps1"),
    }
    paths = list(required_runtime.values()) + [str(canonical / name) for name in CANONICAL]
    paths += [str(private / name) for name in ("batch.json", "gold.json", "skill.md")]
    paths += [str(Path(__file__)), str(repository / "src/ephy_worker/verifier_triage_evaluation.py")]
    paths += [str(p) for p in (repository / "src/ephy_worker").rglob("*.py")]
    paths += [str(Path(arg)) for arg in token_argv if Path(arg).is_file()]
    paths += [str(Path(sys.executable).resolve()), str(batch), str(gold), str(skill)]
    if generation_job:
        paths.append(str(generation_job))
        paths += list(job["contract"]["runtime_hashes"])
    pins = {name: sha256(Path(name).read_bytes()) for name in paths}
    frozen = FrozenTrial(
        BASE_REVISION, evaluator_sha256(), pins[str(private / "batch.json")],
        pins[str(private / "gold.json")], pins[str(private / "skill.md")],
        pins[str(policy)], sha256(json_bytes({"runtime": required_runtime, "identity": identity})),
        sha256(PROMPT.encode()), identity["model_id"], "planner",
    )
    contract = {
        "schema": "ephy.triage-consumer.v1", "purpose": purpose, "frozen": asdict(frozen),
        "identity": identity, "runtime": required_runtime, "canonical_root": str(canonical),
        "canonical_manifest": canonical_manifest, "private_root": str(private),
        "directory": str(directory), "resource_lock": (
            job["runtime"]["resource_lock"] if purpose == "fixed_trial"
            else str(directory / "resource.lock")),
        "token_argv": token_argv, "pins": pins, "stage_seconds": stage_seconds,
        "max_requests": 8, "max_log_bytes": 8388608, "max_process_rss_bytes": 4294967296,
        "minimum_free_ram_bytes": 4294967296, "minimum_free_disk_bytes": 2147483648,
        "budgets": budget_reservations(), "generation_job": str(generation_job) if generation_job else None,
        "dependency_versions": {name: version(name) for name in ("httpx", "psutil", "jsonschema")},
        "controller": {"executable": str(Path(sys.executable).resolve()), "python_version": sys.version},
    }
    path = directory / "contract.json"
    write_new(path, json_bytes(contract))
    return path


def count_payload(contract, raw, directory, label):
    """Counter receives the complete request including tools/system/context."""
    intact(contract)
    result = subprocess.run(
        contract["token_argv"], input=raw, capture_output=True,
        check=False,
        env=command_environment(Path(contract["directory"]), directory),
        timeout=min(20, contract["stage_seconds"]),
        **({"creationflags": 0x08000000} if os.name == "nt" else {}),
    )
    write_new(directory / (label + ".token-counter.stdout"), result.stdout)
    write_new(directory / (label + ".token-counter.stderr"), result.stderr)
    require(result.returncode == 0, "Frozen tokenizer failed")
    measured = json.loads(result.stdout)
    require(measured.get("payload_sha256") == sha256(raw), "Tokenizer measured different payload")
    require(measured.get("model_id") == contract["identity"]["model_id"], "Wrong tokenizer model")
    if contract["purpose"] == "fixed_trial":
        require(measured.get("model_generation_requests") == 0
                and measured.get("spec_sha256") == contract["pins"][contract["token_argv"][3]]
                and isinstance(measured.get("rendered_sha256"), str)
                and len(measured["rendered_sha256"]) == 64
                and not measured.get("synthetic_counter"), "Missing actual tokenizer evidence")
    check_context_fit(measured["input_tokens"], OUTPUT_TOKENS + 8, contract["identity"]["context"])
    return measured


def payload_strings(value, depth=0):
    if isinstance(value, str):
        yield value.replace("\r\n", "\n")
        if depth < 4:
            try:
                decoded = json.loads(value)
            except ValueError:
                return
            if decoded != value:
                yield from payload_strings(decoded, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from payload_strings(item, depth)
    elif isinstance(value, dict):
        for item in value.values():
            yield from payload_strings(item, depth)


class Gateway:
    """An owned loopback capture boundary; fixed upstream, no arbitrary routes."""
    def __init__(self, contract, directory, arm):
        self.contract, self.directory, self.arm = contract, directory, arm
        self.records = []
        self.failure = None
        self.closed = threading.Event()
        self.deadline = time.monotonic() + contract["stage_seconds"]
        self.lock = threading.Lock()
        self.skill = (Path(contract["private_root"]) / "skill.md").read_bytes().decode("utf-8")
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, status, data, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                try:
                    require(self.path == "/health", "Unexpected gateway read")
                    intact(contract)
                    verify_identity(contract["identity"])
                    self.respond(200, json_bytes({
                        "service": "strata", "api_key": False, "loaded": True,
                        "model": contract["identity"]["model_id"],
                        "max_context": contract["identity"]["context"],
                    }))
                except (GateFailure, ValueError, OSError, KeyError, TypeError, httpx.HTTPError,
                        psutil.Error, subprocess.SubprocessError) as error:
                    gateway.failure = type(error).__name__ + ": " + str(error)
                    self.respond(503, json_bytes({"error": gateway.failure}))

            def do_POST(self):
                if not gateway.lock.acquire(blocking=False):
                    gateway.failure = "Concurrent generation prohibited"
                    self.respond(409, b'{"error":"concurrent generation prohibited"}')
                    return
                record = {"accepted": False, "forwarded": False}
                answer = None
                number = len(gateway.records) + 1
                gateway.records.append(record)
                try:
                    require(not gateway.closed.is_set() and time.monotonic() < gateway.deadline,
                            "Gateway deadline/closure")
                    require(not gateway.failure, "Gateway already failed")
                    require(self.path == "/v1/chat/completions", "Unexpected gateway POST")
                    size = int(self.headers["Content-Length"])
                    require(0 < size <= contract["max_log_bytes"], "Invalid request size")
                    self.connection.settimeout(max(0.01, gateway.deadline - time.monotonic()))
                    raw = self.rfile.read(size)
                    require(len(raw) == size, "Truncated HTTP request")
                    write_new(directory / f"http-{number}.json", raw)
                    payload = json.loads(raw)
                    intact(contract)
                    resource_gate(contract, directory)
                    verify_identity(contract["identity"])
                    require(number <= contract["max_requests"], "N+1 request refused")
                    require(payload.get("model") == contract["identity"]["model_id"], "Wrong model")
                    require(type(payload.get("max_tokens")) is int
                            and 0 < payload["max_tokens"] <= RESPONSE_TOKENS
                            and "max_completion_tokens" not in payload, "Wrong response cap")
                    require(not payload.get("strata_mcp") and not payload.get("response_format"),
                            "Server augmentation prohibited")
                    leak = any(gateway.skill.replace("\r\n", "\n") in s for s in payload_strings(payload))
                    record.update(payload_sha256=sha256(raw), baseline_skill_leak=leak if arm == "baseline" else False)
                    require(arm != "baseline" or not leak, "Baseline contains candidate skill bytes")
                    if number == 1:
                        preview = json.loads((directory / "previews/1.json").read_bytes())
                        projection = dict(payload)
                        projection["tools"] = preview.get("tools", payload.get("tools"))
                        documents = [
                            (Path(contract["canonical_root"]) / name).read_text(encoding="utf-8")
                            for name in CANONICAL[1:]
                        ]
                        documents.append((Path(contract["private_root"]) / "batch.json").read_text(encoding="utf-8"))
                        if arm == "treatment":
                            documents.append(gateway.skill)
                        projection["messages"] = list(payload["messages"]) + [
                            {"role": "user", "content": "\n".join(documents)}
                        ]
                        projected = json_bytes(projection)
                        write_new(directory / "complete-batch-projection.json", projected)
                        record["complete_batch_fit"] = count_payload(
                            contract, projected, directory, "complete-batch-projection")
                    record["actual_payload_fit"] = count_payload(contract, raw, directory, f"http-{number}")
                    intact(contract)
                    verify_identity(contract["identity"])
                    upstream = contract["identity"]["base_url"] + "/v1/chat/completions"
                    require(not gateway.closed.is_set() and time.monotonic() < gateway.deadline,
                            "Gateway deadline/closure before forwarding")
                    record["accepted"] = True
                    with httpx.Client(trust_env=False, follow_redirects=False,
                                      timeout=max(0.01, gateway.deadline - time.monotonic())) as client:
                        record["forwarded"] = True
                        with client.stream("POST", upstream, content=raw,
                                           headers={"Content-Type": "application/json"}) as response:
                            require(response.is_success, "Upstream rejected request")
                            chunks, total = [], 0
                            for chunk in response.iter_raw():
                                require(not gateway.closed.is_set() and time.monotonic() < gateway.deadline,
                                        "Gateway deadline/closure while reading response")
                                total += len(chunk)
                                require(total <= contract["max_log_bytes"], "Response log cap exceeded")
                                chunks.append(chunk)
                            body = b"".join(chunks)
                            write_new(directory / f"http-{number}.response", body)
                            answer = (response.status_code, body, response.headers.get(
                                "Content-Type", "application/json"))
                    intact(contract)
                except (GateFailure, ValueError, OSError, KeyError, TypeError, httpx.HTTPError,
                        psutil.Error, subprocess.SubprocessError) as error:
                    gateway.failure = type(error).__name__ + ": " + str(error)
                    record["error"] = gateway.failure
                    answer = (503, json_bytes({"error": gateway.failure}), "application/json")
                finally:
                    try:
                        write_new(directory / f"http-{number}.receipt.json", json_bytes(record))
                    finally:
                        gateway.lock.release()
                # A successful response is visible only after the immutable
                # receipt and pin recheck complete, so the next turn is serial.
                try:
                    self.respond(*answer)
                except (BrokenPipeError, ConnectionResetError):
                    gateway.failure = "Consumer disconnected before delivery"

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_):
        self.closed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def canonical_delivery(contract, events):
    ack = next(e for e in events if e.get("kind") == "governance_result")
    details = ack.get("details", {})
    require(details.get("policySha256") == contract["frozen"]["policy_sha256"], "Policy mismatch")
    delivered = "".join(c.get("text", "") for c in ack.get("content", []) if c.get("type") == "text")
    actual = {d["path"]: d for d in details.get("requiredContext", [])}
    for name in CANONICAL[1:]:
        expected = contract["canonical_manifest"][name]
        require(actual.get(name, {}).get("sha256") == expected["sha256"]
                and actual.get(name, {}).get("bytes") == expected["bytes"], "Canonical context mismatch")
        require((Path(contract["canonical_root"]) / name).read_text(encoding="utf-8") in delivered,
                "Canonical context was truncated or omitted")


def wire_read_delivery(directory, records, events):
    """Bind exact tool-result bytes to the actual transmitted request body."""
    expected = {e["toolCallId"]: e for e in events if e.get("kind") == "triage_exact_read"}
    delivered = {}
    for number, record in enumerate(records, 1):
        raw = (directory / f"http-{number}.json").read_bytes()
        require(sha256(raw) == record["payload_sha256"], "Captured HTTP payload changed")
        for message in json.loads(raw)["messages"]:
            call = message.get("tool_call_id")
            if message.get("role") != "tool" or call not in expected:
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(c["text"] for c in content if c.get("type") == "text")
            require(isinstance(content, str), "Missing transmitted read text")
            body = content.encode("utf-8")
            event = expected[call]
            require(event["full_content_delivered"] is True
                    and len(body) == event["raw_bytes"]
                    and sha256(body) == event["raw_sha256"] == event["delivered_sha256"],
                    "Transmitted read bytes differ from raw artifact")
            delivered[call] = {"toolCallId": call, "path": event["path"],
                               "bytes": len(body), "sha256": sha256(body)}
    require(expected and delivered.keys() == expected.keys(), "Exact read never reached provider payload")
    return list(delivered.values())


def run_session(contract, index):
    require(type(index) is int and 0 <= index < 4, "Session5 refused before launch")
    intact(contract)
    arm = "baseline" if index < 2 else "treatment"
    directory = Path(contract["directory"]) / f"consumer-{index}"
    require(not directory.exists(), "Session reuse prohibited")
    directory.mkdir()
    resource_gate(contract, directory)
    guest = directory / "input"
    private = Path(contract["private_root"])
    write_new(guest / BATCH_PATH, (private / "batch.json").read_bytes())
    if arm == "treatment":
        write_new(guest / SKILL_PATH, (private / "skill.md").read_bytes())
    before = snapshot(guest)
    require(sum(entry["bytes"] for entry in before.values()) <= contract["max_log_bytes"],
            "Consumer input exceeds log cap")
    managed = directory / "managed"
    write_new(managed / "settings.json", json_bytes({
        "retry": {"enabled": False}, "compaction": {"enabled": False},
    }))
    previews = directory / "previews"
    previews.mkdir()
    temp = directory / "temp"
    temp.mkdir()
    frozen = FrozenTrial(**contract["frozen"])
    config = {
        "root": str(guest), "role": "planner", "model_id": frozen.model_id,
        "provider_id": "strata-local", "allowed_files": [], "arm": arm,
        "previews": str(previews), "max_requests": contract["max_requests"],
        "output_token_budget": OUTPUT_TOKENS, "max_response_tokens": RESPONSE_TOKENS,
    }
    write_new(directory / "config.json", json_bytes(config))
    trace = directory / "trace.jsonl"
    runtime = contract["runtime"]
    prompt = PROMPT + (
        "Baseline: no candidate skill is available; skill_sha256 must be null."
        if arm == "baseline" else
        "Treatment: explicitly read " + SKILL_PATH + " completely; skill_sha256 is "
        + frozen.skill_sha256 + ". This is explicit loading, not automatic skill discovery."
    )
    write_new(directory / "prompt.txt", prompt.encode())
    gateway = Gateway(contract, directory, arm)
    with gateway as origin:
        write_new(directory / "provider-identity.json", json_bytes({
            "base_url": origin, "model_id": frozen.model_id, "context": contract["identity"]["context"],
        }))
        env = command_environment(guest, temp)
        env.update(
            PI_CODING_AGENT_DIR=str(managed), PI_OFFLINE="1",
            DUAL_GOVERNANCE_ROLE="planner", DUAL_GOVERNANCE_POLICY=str(
                Path(contract["canonical_root"]) / CANONICAL[0]),
            DUAL_GOVERNANCE_CONTEXT_ROOT=contract["canonical_root"],
            DUAL_JOB_RUNNER=runtime["runner"], DUAL_RUNNER_SMOKE_TEST=runtime["runner_test"],
            EPHY_STRATA_IDENTITY=str(directory / "provider-identity.json"),
            EPHY_FORMAL_STAGE_CONFIG=str(directory / "config.json"), EPHY_FORMAL_TRACE=str(trace),
            EPHY_TRIAGE_CONFIG=str(directory / "config.json"),
        )
        args = [
            runtime["pi"], "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-themes", "--no-context-files", "--offline", "--provider", "strata-local",
            "--model", frozen.model_id, "--thinking", "off",
        ]
        for name in ("provider", "stage_guard", "extra_guard", "governance_gate"):
            args += ["--extension", runtime[name]]
        args += ["--session", str(directory / "session.jsonl"), "--mode", "json", "--print",
                 "--approve", "--", "@" + str(directory / "prompt.txt")]
        start = time.monotonic()
        with (directory / "stdout.jsonl").open("xb") as out, (directory / "stderr.log").open("xb") as err:
            process = subprocess.Popen(
                args, cwd=guest, env=env, stdout=out, stderr=err,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            try:
                owned = psutil.Process(process.pid)
                actual_executable = str(Path(owned.exe()).resolve())
                require(actual_executable == str(Path(runtime["pi"]).resolve()), "Wrong launched Pi executable")
                identity = {
                    "pid": process.pid, "created_at": owned.create_time(),
                    "executable": actual_executable,
                    "executable_sha256": contract["pins"][runtime["pi"]],
                }
                write_new(directory / "process.json", json_bytes({"argv": args, **identity}))
                while process.poll() is None:
                    require(time.monotonic() - start <= contract["stage_seconds"], "Consumer timeout")
                    resource_gate(contract, directory)
                    try:
                        rss = owned.memory_info().rss
                    except psutil.NoSuchProcess:
                        # A finished child may disappear between poll and measurement.
                        # Its exit code and complete saved traces are still required below.
                        if process.poll() is None:
                            raise
                        break
                    require(rss <= contract["max_process_rss_bytes"], "Consumer process RSS cap")
                    size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
                    require(size <= contract["max_log_bytes"], "Consumer log cap")
                    time.sleep(0.05)
            finally:
                if process.poll() is None:
                    stop_tree(process)  # exact owned child only; never stops Strata
    require(process.returncode == 0, f"Consumer exited {process.returncode}")
    require(not gateway.failure, "Gateway rejected session: " + str(gateway.failure))
    require(snapshot(guest) == before, "Consumer changed input snapshot")
    intact(contract)
    events = [json.loads(line) for line in trace.read_bytes().splitlines() if line.strip()]
    observed = stage_evidence(events, frozen.model_id, "planner")
    require(len(gateway.records) == observed["requests"]
            and all(r["accepted"] and r["forwarded"] and not r.get("error")
                    for r in gateway.records), "HTTP/managed-stage request evidence mismatch")
    canonical_delivery(contract, events)
    delivered_reads = wire_read_delivery(directory, gateway.records, events)
    output = final_assistant_text(directory / "stdout.jsonl")
    require(output == final_assistant_text(directory / "session.jsonl", session=True),
            "Final stdout/session output mismatch")
    write_new(directory / "output.json", output.encode())
    supplied = SuppliedSession(
        arm, str(directory / "session.jsonl"), process.pid, identity["created_at"],
        time.monotonic() - start, contract["frozen"], output.encode(), trace.read_bytes(),
    )
    validate_session(supplied, frozen, (private / "batch.json").read_bytes())
    write_new(directory / "receipt.json", json_bytes({
        "arm": arm, "observed": observed, "process": identity,
        "exit_code": process.returncode,
        "wire_read_delivery": delivered_reads,
        "elapsed_seconds": time.monotonic() - start, "input_before": before,
        "input_after": snapshot(guest), "canonical_manifest": contract["canonical_manifest"],
        "wire_records": gateway.records, "output_sha256": sha256(output.encode()),
        "trace_sha256": sha256(trace.read_bytes()), "real_model_contacted": contract["purpose"] == "fixed_trial",
    }))
    return supplied


def run_contract(path: Path, expected_sha256: str):
    require(sha256(path.read_bytes()) == expected_sha256, "Frozen contract changed")
    contract = json.loads(path.read_bytes())
    validate_contract(contract)
    sessions = []
    with exclusive_lock(Path(contract["resource_lock"])):
        for index in range(4):
            require(sha256(path.read_bytes()) == expected_sha256, "Contract changed during evaluation")
            sessions.append(run_session(contract, index))
        private = Path(contract["private_root"])
        result = evaluate(
            FrozenTrial(**contract["frozen"]), (private / "batch.json").read_bytes(),
            (private / "gold.json").read_bytes(), (private / "skill.md").read_bytes(), sessions,
        )
        intact(contract)
        verify_identity(contract["identity"])
        require(sha256(path.read_bytes()) == expected_sha256, "Contract changed before result")
        result["synthetic_controls"] = contract["purpose"] == "native_controls"
        result["live_execution_attested"] = contract["purpose"] == "fixed_trial"
        result["adoption_authorized"] = False
        result["live_trial_enabled"] = False
        write_new(Path(contract["directory"]) / "result.json", json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--execute-authorized", action="store_true")
    args = parser.parse_args()
    if not args.execute_authorized:
        parser.error("Explicit consumer execution authorization required")
    print(json.dumps(run_contract(args.contract, args.sha256)))


if __name__ == "__main__":
    main()
