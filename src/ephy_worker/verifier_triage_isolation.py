"""Pre-authoring freeze and closed actual-HTTP capture for triage generation.

Controller-owned records are outside every Pi data root. Evidence is checked
from raw files, not model assertions. Native execution accepts an owned fake
provider only; live consumers require a separately bound external proposal.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import psutil

from .formal_artifacts import encode
from .formal_runtime import (
    command_environment,
    exclusive_lock,
    final_assistant_text,
    snapshot_hash,
    stage_evidence,
    stop_tree,
)
from .strata_runtime import StrataRunner, verify_identity
from .verifier_triage_evaluation import BASE_REVISION, SKILL_PATH, evaluator_sha256, sha256, validate_corpus

FAKE_MODEL = "synthetic-triage-generation-fixture"
TASK = (
    "Author only a general evidence-triage Markdown skill. No held-out examples or answers are "
    "available. Read authoring/task.md fully. Planner: return a short plan without writing. "
    "Implementer: read authoring/plan.md, then write only "
    + SKILL_PATH + ". Return DONE after writing. No shell, search, network, or delegation."
)
PLAN = {"schema": "ephy.triage-fixed-plan.v1", "base_revision": BASE_REVISION,
        "cases": 12, "repeats_per_arm": 2, "safety_correct_per_treatment": 12,
        "minimum_paired_improvements": 3, "paired_regressions": 0,
        "scope": [SKILL_PATH], "repair": 0}
CAPS = {"max_requests": 8, "output_token_budget": 2200, "max_response_tokens": 1024,
        "stage_seconds": 300, "job_seconds": 900}


def helpers():
    from . import verifier_triage_consumer as c
    return c


def load(path):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            helpers().require(key not in obj, "Duplicate JSON key")
            obj[key] = value
        return obj
    return json.loads(Path(path).read_bytes(), object_pairs_hook=unique)


def planner_only_mode(contract):
    value = contract.get("planner_only", False)
    helpers().require(type(value) is bool, "planner_only must be a frozen boolean")
    return value


def generation_submission_sha256(job):
    """Bind every immutable submission field; only controller state can evolve."""
    c = helpers()
    c.require({"schemaVersion", "id", "baseRevision", "contract", "runtime", "controls",
               "model_identities", "verifier_identity", "humanAuthorization",
               "repoRoot", "worktreePath", "jobDir"} <= set(job),
              "Planner-only submission identity is incomplete")
    # Preflight derives environment_sha256; it has a separate frozen binding.
    mutable = {"status", "message", "updatedAt", "runnerPid", "outcome", "environment_sha256"}
    return sha256(encode({key: value for key, value in job.items() if key not in mutable}))


def generation_environment_sha256(job):
    """Derive the exact environment that existing preflight records, before it runs."""
    return sha256(encode({
        "python": job["runtime"]["python"], "python_version": sys.version,
        "dependencies": {name: version(name) for name in ("pytest", "ruff", "jsonschema", "psutil")},
        "offline": True, "locale": "UTF-8", "pythonpath": "<measured-worktree>/src",
        "temp_root": str(Path(job["jobDir"]).resolve() / "temp"),
        "contract_sha256": sha256(encode(job["contract"])),
    }))


def freeze_generation(repository, pi, identity, batch, gold, directory, token_argv, *, generation_job=None):
    """Called before either fresh Pi process or candidate creation."""
    c = helpers()
    c.require(not directory.exists(), "Fresh pre-authoring freeze required")
    batch_bytes, gold_bytes = batch.read_bytes(), gold.read_bytes()
    validate_corpus(batch_bytes, gold_bytes)
    token_argv = [str(Path(arg).resolve()) if Path(arg).is_file() else arg for arg in token_argv]
    c.require(isinstance(identity["model_id"], str) and identity["model_id"], "Invalid model identity")
    if identity["model_id"] != FAKE_MODEL:
        c.require(generation_job is not None, "Real generation requires a pre-bound external job")
        c.require(len(token_argv) == 4 and token_argv[2] == "--spec"
                  and Path(token_argv[1]).resolve() == (repository/"scripts/count_strata_consumer_tokens.py").resolve(),
                  "Real generation requires pinned actual Strata tokenizer")
        spec = load(Path(token_argv[3]))
        c.require(spec["model_id"] == identity["model_id"]
                  and Path(spec["configuration"]).resolve() == Path(identity["configuration"]["path"]).resolve()
                  and spec["pins"][spec["configuration"]] == identity["configuration"]["sha256"],
                  "Generation tokenizer deployment mismatch")
    verify_identity(identity)
    directory.mkdir(parents=True)
    canonical = directory / "canonical"
    canonical_manifest = {}
    for name in c.CANONICAL:
        data = (repository / name).read_bytes()
        c.write_new(canonical / name, data)
        canonical_manifest[name] = {"sha256": sha256(data), "bytes": len(data)}
    for name, data in (("batch.json", batch_bytes), ("gold.json", gold_bytes)):
        c.write_new(directory / "private" / name, data)
    runtime = {
        "pi": str(pi.resolve()), "provider": str(repository / "tools/pi-local/strata-provider.ts"),
        "stage_guard": str(repository / "tools/pi-local/formal-stage-guard.ts"),
        "extra_guard": str(repository / "tools/pi-local/triage-generation-guard.ts"),
        "governance_gate": str(repository / ".pi/extensions/governance-gate.ts"),
        "runner": str(repository / "tools/pi-local/windows/run-background-job.ps1"),
        "runner_test": str(repository / "tools/pi-local/windows/test-background-job-runner.ps1"),
    }
    pins = list(runtime.values()) + [str(p) for p in (repository / "src/ephy_worker").rglob("*.py")]
    pins += [str(p) for p in canonical.rglob("*") if p.is_file()]
    pins += [str(directory / "private" / n) for n in ("batch.json", "gold.json")]
    pins += [str(Path(sys.executable).resolve()), *[a for a in token_argv if Path(a).is_file()]]
    binding = None
    log_root = None
    task = TASK
    planner_only = False
    if generation_job:
        job = load(generation_job)
        c.validate_resource_lock_pins(job["runtime"].get("resource_lock"), job["contract"]["runtime_hashes"])
        c.validate_generation_budget(job)
        planner_only = planner_only_mode(job["contract"])
        c.require(not directory.resolve().is_relative_to(Path(job["worktreePath"]).resolve()),
                  "Private freeze inside generation root")
        binding = {"job_id": job["id"], "base": job["baseRevision"],
                   "contract_sha256": sha256(encode(job["contract"])),
                   "runtime_sha256": sha256(encode(job["runtime"])), "job_dir": job["jobDir"],
                   "worktree": job["worktreePath"]}
        log_root = job["jobDir"]
        task = job["contract"]["task"]
        for name in ("pi", "provider", "stage_guard", "governance_gate", "runner", "runner_test"):
            runtime[name] = job["runtime"][name]
        c.require(Path(runtime["pi"]).resolve() == pi.resolve(), "Generation Pi differs from frozen job")
        for p, digest in job["contract"]["runtime_hashes"].items():
            c.require(sha256(Path(p).read_bytes()) == digest, "Generation job runtime pin changed")
        pins += list(job["contract"]["runtime_hashes"])
    freeze = {
        "schema": "ephy.triage-generation-freeze.v1", "job_id": str(uuid.uuid4()),
        "planner_only": planner_only,
        "created_at": time.time(), "plan": PLAN, "caps": CAPS,
        "evaluator_sha256": evaluator_sha256(), "task": task,
        "batch_sha256": sha256(batch_bytes), "gold_sha256": sha256(gold_bytes),
        "directory": str(directory.resolve()), "log_root": log_root,
        "private_root": str((directory / "private").resolve()),
        "canonical_root": str(canonical.resolve()), "identity": identity, "runtime": runtime,
        "canonical_manifest": canonical_manifest,
        "token_argv": token_argv, "stage_seconds": 300, "max_requests": 8,
        "max_log_bytes": 8388608, "max_process_rss_bytes": 4294967296,
        "minimum_free_ram_bytes": 4294967296, "minimum_free_disk_bytes": 2147483648,
        "purpose": "generation_capture", "generation_binding": binding,
        "generation_job_path": str(generation_job.resolve()) if generation_job else None,
        **({"generation_submission_sha256": generation_submission_sha256(job),
            "generation_environment_sha256": generation_environment_sha256(job)} if planner_only else {}),
        "dependency_versions": {n: version(n) for n in ("httpx", "psutil", "jsonschema")},
        "controller": {"executable": str(Path(sys.executable).resolve()), "python_version": sys.version},
        "pins": {p: sha256(Path(p).read_bytes()) for p in pins},
        "frozen": {"policy_sha256": sha256((canonical / c.CANONICAL[0]).read_bytes())},
    }
    path = directory / "freeze.json"
    write_capture(freeze, path, c.json_bytes(freeze))
    return path


def frozen(path, expected):
    c = helpers()
    c.require(sha256(Path(path).read_bytes()) == expected, "Pre-authoring freeze changed")
    value = load(path)
    c.require(value["schema"] == "ephy.triage-generation-freeze.v1"
              and value["plan"] == PLAN and value["caps"] == CAPS,
              "Unfrozen plan/scope/budgets")
    c.require(not planner_only_mode(value) or value["generation_binding"] is not None,
              "Planner-only capture requires a bound external job")
    c.require(not planner_only_mode(value)
              or isinstance(value.get("generation_submission_sha256"), str)
              and re.fullmatch(r"[0-9a-f]{64}", value["generation_submission_sha256"]),
              "Planner-only frozen submission identity missing")
    c.require(not planner_only_mode(value)
              or isinstance(value.get("generation_environment_sha256"), str)
              and re.fullmatch(r"[0-9a-f]{64}", value["generation_environment_sha256"]),
              "Planner-only frozen environment identity missing")
    c.intact(value)
    private = Path(value["private_root"])
    batch, gold = (private/"batch.json").read_bytes(), (private/"gold.json").read_bytes()
    c.require((sha256(batch), sha256(gold)) == (value["batch_sha256"], value["gold_sha256"]),
              "Frozen input hashes differ from private bytes")
    validate_corpus(batch, gold)
    capture_budget(value, 0)
    c.require(value["evaluator_sha256"] == evaluator_sha256(), "Evaluator changed")
    c.require(value["controller"] == {"executable": str(Path(sys.executable).resolve()),
                                    "python_version": sys.version}, "Controller changed")
    return value


def deny_leak(contract, payload):
    """Copy scan supplements the closed roots, prompt, and exact result binding."""
    c = helpers()
    private = Path(contract["private_root"])
    batch = load(private / "batch.json")
    needles = {v["id"] for v in batch} | {v["input"] for v in batch if len(v["input"]) >= 16}
    needles |= {(private / n).read_text(encoding="utf-8") for n in ("batch.json", "gold.json")}
    c.require(not any(needle in text for text in c.payload_strings(payload) for needle in needles),
              "Held-out/gold leakage")


def events_at(directory):
    path = directory / "trace.jsonl"
    if not path.exists():
        path = Path(load(directory / "config.json")["trace_path"])
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def wire_matches(contract, directory, number, raw, config, events):
    """Verify the final body, including every tool result, before forwarding."""
    c = helpers()
    deny_leak(contract, json.loads(raw))
    starts = [e for e in events if e.get("kind") == "generation_start"]
    c.require(len(starts) == 1 and starts[0]["session_id"] == config["session_id"]
              and starts[0]["role"] == config["role"]
              and starts[0]["config_sha256"] == sha256((directory / "config.json").read_bytes()),
              "Wrong generation session/config")
    previews = [e for e in events if e.get("kind") == "generation_preview"]
    c.require([e["number"] for e in previews] == list(range(1, number + 1))
              and all(e["session_id"] == config["session_id"] and e["role"] == config["role"]
                      for e in previews), "Missing/duplicate/session preview")
    preview_path = directory / f"previews/{number}.json"
    preview_raw = preview_path.read_bytes()
    c.require(sha256(preview_raw) == previews[-1]["sha256"], "Preview hash changed")
    expected = load(preview_path)
    if number == 1:
        acks = [t for t in expected["tools"] if t.get("function", {}).get("name") == "governance_ack"]
        c.require(len(acks) == 1, "Missing exact governance tool")
        expected["tools"] = acks
        expected["tool_choice"] = {"type": "function", "function": {"name": "governance_ack"}}
    c.require(json.loads(raw) == expected, "Final HTTP input differs from pinned governance transformation")
    messages = expected["messages"]
    context = "\n".join(c.payload_strings(messages[:1]))
    nonce = re.search(r"Acknowledgement-Nonce: ([^\s]+)", context)
    c.require(nonce and ("Role: " + config["role"]) in context, "Missing role/nonce")
    def text(content):
        if isinstance(content, str):
            return content
        c.require(isinstance(content, list)
                  and all(set(v) == {"type", "text"} and v["type"] == "text" for v in content),
                  "Nontext model-visible input")
        return "".join(v["text"] for v in content)
    users = [text(m["content"]) for m in messages if m.get("role") == "user"]
    if number == 1:
        c.require(len(users) == 1 and str(users[0]).startswith("MANDATORY GOVERNANCE BOOTSTRAP ONLY."),
                  "Unclosed initial user context")
    else:
        wire_prompt = '<file name="' + config.get("prompt_path", str(directory / "prompt.txt")) + '">\n' + config["prompt"] + '\n</file>\n'
        c.require(users == [wire_prompt], "Unclosed user context")
    calls, results = {}, {}
    for e in events:
        if e.get("kind") == "generation_tool_call":
            c.require(e["id"] not in calls and e["session_id"] == config["session_id"],
                      "Duplicate/session tool call")
            calls[e["id"]] = e
        elif e.get("kind") == "generation_tool_result":
            c.require(e["id"] in calls and e["id"] not in results
                      and e["name"] == calls[e["id"]]["name"] and e["input"] == calls[e["id"]]["input"]
                      and e["session_id"] == config["session_id"]
                      and e["bytes"] == len(e["text"].encode()) and e["sha256"] == sha256(e["text"].encode()),
                      "Missing/substituted tool result")
            results[e["id"]] = e
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    c.require(len(tool_messages) == len(results) and {m["tool_call_id"] for m in tool_messages} == set(results),
              "Model-visible tool-result capture incomplete")
    for m in tool_messages:
        c.require(text(m["content"]) == results[m["tool_call_id"]]["text"], "Tool-result bytes substituted")
    c.require(set(calls) == set(results), "Pending tool result")
    wire_calls = {}
    for m in messages:
        for call in m.get("tool_calls", []):
            c.require(call["id"] not in wire_calls, "Duplicate model-visible tool call")
            wire_calls[call["id"]] = call["function"]
    c.require(set(wire_calls) == set(calls), "Model-visible tool-call capture incomplete")
    for identifier, call in calls.items():
        c.require(wire_calls[identifier]["name"] == call["name"]
                  and json.loads(wire_calls[identifier]["arguments"]) == call["input"], "Tool call substituted")
        name, args = call["name"], call["input"]
        c.require(name == "governance_ack" or name == "read" and set(args) == {"path"}
                  and args["path"] in config["allowed_reads"] or name == "write"
                  and config["role"] == "implementer" and set(args) == {"path", "content"}
                  and args["path"] == SKILL_PATH, "Captured tool exceeds closed scope")
        if name == "read":
            entry = config["input_snapshot"][args["path"]]
            c.require(results[identifier]["sha256"] == entry["sha256"]
                      and results[identifier]["bytes"] == entry["bytes"], "Read source was not pre-frozen")
    return {"nonce": nonce.group(1), "tool_results": [
        {"id": k, "bytes": e["bytes"], "sha256": e["sha256"]} for k, e in results.items()
    ]}


def capture_budget(contract, pending_bytes, trace=None):
    roots = [Path(contract["directory"])]
    if contract.get("log_root"):
        roots.append(Path(contract["log_root"]))
    paths = {p.resolve() for root in roots for p in root.rglob("*") if p.is_file()}
    if trace and trace.exists():
        paths.add(trace.resolve())
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            # Git renames its transient index lock when it commits the index.
            if not path.name.endswith(".index.lock"):
                raise
    helpers().require(type(pending_bytes) is int and pending_bytes >= 0
                      and total+pending_bytes <= contract["max_log_bytes"],
                      "Generation capture cumulative log cap")


def write_capture(contract, path, data):
    capture_budget(contract, len(data))
    helpers().write_new(path, data)


class GenerationCapture:
    def __init__(self, freeze_path, freeze_sha256, directory):
        self.freeze_path, self.freeze_sha256, self.directory = freeze_path, freeze_sha256, directory
        self.contract = frozen(freeze_path, freeze_sha256)
        self.config_raw = (directory / "config.json").read_bytes()
        self.config = load(directory / "config.json")
        self.completed = []
        self.nonce = None

    def guard_write(self, pending_bytes):
        trace = Path(self.config.get("trace_path", str(self.directory/"trace.jsonl")))
        capture_budget(self.contract, pending_bytes, trace)

    def write_new(self, path, data):
        self.guard_write(len(data))
        helpers().write_new(path, data)

    def check_prefix(self):
        c = helpers()
        frozen(self.freeze_path, self.freeze_sha256)
        c.require((self.directory / "config.json").read_bytes() == self.config_raw, "Session config changed")
        for n, digest in enumerate(self.completed, 1):
            closure = self.directory / f"http-{n}.complete.json"
            c.require(sha256(closure.read_bytes()) == digest, "Missing/changed earlier capture")
            d = load(closure)
            for name, h in d["files"].items():
                c.require(sha256((self.directory / name).read_bytes()) == h, "Earlier captured file changed")
            receipt = load(self.directory / f"http-{n}.receipt.json")
            c.require(receipt["accepted"] and receipt["forwarded"] and not receipt.get("error"),
                      "Earlier forwarding incomplete")

    def admit(self, raw, number):
        c = helpers()
        self.check_prefix()
        c.require(number == len(self.completed) + 1, "Replay/duplicate request sequence")
        c.require(not any(sha256(raw) == load(self.directory / f"http-{i}.complete.json")["payload_sha256"]
                          for i in range(1, number)), "Replay/duplicate payload")
        events = events_at(self.directory)
        process_path = self.directory / "process.json"
        if not process_path.exists():
            starts = [e for e in events if e.get("kind") == "generation_start"]
            c.require(len(starts) == 1, "Missing observed generation process")
            child = psutil.Process(starts[0]["pid"])
            c.require(child.ppid() == os.getpid(), "Generation process is not controller-owned")
            self.write_new(process_path, c.json_bytes({"pid": child.pid, "created_at": child.create_time(),
                        "executable": str(Path(child.exe()).resolve()), "argv": self.config["argv"]}))
        process = load(process_path)
        owned = psutil.Process(process["pid"])
        c.require(owned.create_time() == process["created_at"]
                  and str(Path(owned.exe()).resolve()) == self.contract["runtime"]["pi"],
                  "Generation process changed")
        c.require(process["created_at"] >= self.contract["created_at"], "Freeze occurred after authoring")
        trace_path = Path(self.config.get("trace_path", str(self.directory / "trace.jsonl")))
        trace = trace_path.read_bytes()
        binding = wire_matches(self.contract, self.directory, number, raw, self.config, events)
        c.require(all(e.get("pid") == process["pid"] for e in events if e.get("kind") == "generation_start"),
                  "Other process/session")
        self.nonce = self.nonce or binding["nonce"]
        c.require(binding["nonce"] == self.nonce, "Other governance session")
        actual = c.snapshot(Path(self.config["root"]))
        before = self.config["input_snapshot"]
        c.require(all(actual.get(k) == v for k, v in before.items())
                  and set(actual) - set(before) <= ({SKILL_PATH} if self.config["role"] == "implementer" else set()),
                  "Generation inputs changed")
        # Immutable prefix retained before network forwarding.
        self.guard_write(len(trace))
        self.write_new(self.directory / f"http-{number}.trace-prefix", trace)
        admission = {"schema": "ephy.triage-generation-admission.v1", "number": number,
                     "session_id": self.config["session_id"], "role": self.config["role"],
                     "freeze_sha256": self.freeze_sha256, "payload_sha256": sha256(raw),
                     "preview_sha256": sha256((self.directory / f"previews/{number}.json").read_bytes()),
                     "trace_prefix_sha256": sha256(trace), "process": process, **binding}
        body = c.json_bytes(admission)
        self.guard_write(len(body))
        self.write_new(self.directory / f"http-{number}.admission.json", body)
        return {"generation_admission_sha256": sha256(c.json_bytes(admission))}

    def complete(self, number):
        c = helpers()
        names = [f"http-{number}.json", f"http-{number}.response",
                 f"http-{number}.trace-prefix", f"http-{number}.admission.json", f"previews/{number}.json"]
        closure = {"number": number, "previous_sha256": self.completed[-1] if self.completed else self.freeze_sha256,
                   "payload_sha256": sha256((self.directory / names[0]).read_bytes()),
                   "files": {n: sha256((self.directory / n).read_bytes()) for n in names}}
        data = c.json_bytes(closure)
        self.guard_write(len(data)+4096)
        self.write_new(self.directory / f"http-{number}.complete.json", data)
        self.completed.append(sha256(data))


def run_generation_session(freeze_path, expected, index):
    c = helpers()
    contract = frozen(freeze_path, expected)
    c.require(type(index) is int and index in (0, 1), "Only one planner and implementer")
    identity = contract["identity"]
    c.require(identity["model_id"] == FAKE_MODEL
              and identity["listener"]["pid"] == identity["engine"]["pid"] == os.getpid(),
              "Native generation controller requires owned fake provider; real generation prohibited")
    role = ("planner", "implementer")[index]
    directory = Path(contract["directory"]) / role
    c.require(not directory.exists(), "Generation session reuse")
    directory.mkdir()
    guest = directory / "input"
    write_capture(contract, guest / "authoring/task.md", TASK.encode())
    if index:
        previous = verify_session(freeze_path, expected, Path(contract["directory"]) / "planner")
        write_capture(contract, guest / "authoring/plan.md", (Path(contract["directory"]) / "planner/output.txt").read_bytes())
        c.require(previous["role"] == "planner", "Missing planner")
    before = c.snapshot(guest)
    for sub in ("previews", "managed", "temp"):
        (directory / sub).mkdir()
    write_capture(contract, directory / "managed/settings.json",
                c.json_bytes({"retry": {"enabled": False}, "compaction": {"enabled": False}}))
    prompt = TASK + "\nStage: " + role
    config = {"root": str(guest), "session_id": str(uuid.uuid4()), "role": role, "prompt": prompt,
              "model_id": identity["model_id"], "provider_id": "strata-local",
              "allowed_files": [SKILL_PATH] if index else [], "input_snapshot": before,
              "allowed_reads": list(before), "previews": str(directory / "previews"),
              "capture_root": contract["directory"], "max_log_bytes": contract["max_log_bytes"],
              "log_root": contract.get("log_root"),
              **{k: CAPS[k] for k in ("max_requests", "output_token_budget", "max_response_tokens")}}
    write_capture(contract, directory / "config.json", c.json_bytes(config))
    write_capture(contract, directory / "prompt.txt", prompt.encode())
    capture = GenerationCapture(freeze_path, expected, directory)
    runtime = contract["runtime"]
    gateway = c.Gateway(contract, directory, "generation", generation_capture=capture)
    with gateway as origin:
        write_capture(contract, directory / "provider-identity.json", c.json_bytes({
            "base_url": origin, "model_id": identity["model_id"], "context": identity["context"]}))
        env = command_environment(guest, directory / "temp")
        env.update(PI_CODING_AGENT_DIR=str(directory / "managed"), PI_OFFLINE="1",
                   DUAL_GOVERNANCE_ROLE=role, DUAL_GOVERNANCE_POLICY=str(Path(contract["canonical_root"]) / c.CANONICAL[0]),
                   DUAL_GOVERNANCE_CONTEXT_ROOT=contract["canonical_root"], DUAL_JOB_RUNNER=runtime["runner"],
                   DUAL_RUNNER_SMOKE_TEST=runtime["runner_test"], EPHY_STRATA_IDENTITY=str(directory / "provider-identity.json"),
                   EPHY_FORMAL_STAGE_CONFIG=str(directory / "config.json"), EPHY_FORMAL_TRACE=str(directory / "trace.jsonl"),
                   EPHY_TRIAGE_GENERATION_CONFIG=str(directory / "config.json"))
        args = [runtime["pi"], "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-themes", "--no-context-files", "--offline", "--provider", "strata-local",
                "--model", identity["model_id"], "--thinking", "off"]
        for key in ("provider", "stage_guard", "extra_guard", "governance_gate"):
            args += ["--extension", runtime[key]]
        args += ["--session", str(directory / "session.jsonl"), "--mode", "json", "--print",
                 "--approve", "--", "@" + str(directory / "prompt.txt")]
        started = time.monotonic()
        with (directory / "stdout.jsonl").open("xb") as out, (directory / "stderr.log").open("xb") as err:
            process = subprocess.Popen(args, cwd=guest, env=env, stdout=out, stderr=err,
                                       **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            try:
                owned = psutil.Process(process.pid)
                record = {"pid": process.pid, "created_at": owned.create_time(),
                          "executable": str(Path(owned.exe()).resolve()), "argv": args}
                c.require(record["executable"] == runtime["pi"], "Wrong launched Pi")
                write_capture(contract, directory / "process.json", c.json_bytes(record))
                while process.poll() is None:
                    c.require(time.monotonic() - started < CAPS["stage_seconds"], "Generation stage timeout")
                    c.resource_gate(contract, directory)
                    if process.poll() is not None:
                        break
                    try:
                        c.require(owned.memory_info().rss <= contract["max_process_rss_bytes"], "Generation RSS cap")
                    except psutil.NoSuchProcess:
                        c.require(process.poll() is not None, "Generation process vanished")
                    c.require(sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
                              <= contract["max_log_bytes"], "Generation log cap")
                    time.sleep(.05)
            finally:
                if process.poll() is None:
                    stop_tree(process)
    c.require(process.returncode == 0 and not gateway.failure,
              f"Generation rejected (exit {process.returncode}): {gateway.failure}")
    output = final_assistant_text(directory / "stdout.jsonl")
    c.require(output == final_assistant_text(directory / "session.jsonl", session=True), "Generation stdout/session mismatch")
    write_capture(contract, directory / "output.txt", output.encode())
    after = c.snapshot(guest)
    c.require(all(after.get(k) == v for k, v in before.items())
              and set(after) - set(before) == ({SKILL_PATH} if index else set()), "Generation input/scope changed")
    capture.check_prefix()
    receipt = {"schema": "ephy.triage-generation-session.v1", "role": role,
               "session_id": config["session_id"], "freeze_sha256": expected,
               "process": record, "exit_code": process.returncode, "elapsed_seconds": time.monotonic()-started,
               "input_before": before, "input_after": after, "captures": capture.completed,
               "files": {p.relative_to(directory).as_posix(): sha256(p.read_bytes())
                         for p in directory.rglob("*") if p.is_file()}}
    write_capture(contract, directory / "receipt.json", c.json_bytes(receipt))
    return verify_session(freeze_path, expected, directory)


def verify_session(freeze_path, expected, directory):
    """Pure saved-evidence verification; no network or model process."""
    c = helpers()
    contract = frozen(freeze_path, expected)
    receipt = load(directory / "receipt.json")
    c.require(receipt["schema"] == "ephy.triage-generation-session.v1"
              and receipt["freeze_sha256"] == expected and receipt["exit_code"] == 0
              and receipt["elapsed_seconds"] <= CAPS["stage_seconds"], "Incomplete session")
    config = load(directory / "config.json")
    c.require(config["role"] == receipt["role"] and config["session_id"] == receipt["session_id"],
              "Receipt session changed")
    manifest = c.snapshot(directory)
    manifest.pop("receipt.json")
    c.require(set(manifest) == set(receipt["files"]), "Session capture manifest incomplete")
    for name, h in receipt["files"].items():
        c.require(manifest[name]["sha256"] == h, "Session evidence hash changed")
    c.require(receipt["process"]["created_at"] >= contract["created_at"], "Not pre-authoring freeze")
    c.require(receipt["process"]["executable"] == contract["runtime"]["pi"], "Other Pi process")
    events = events_at(directory)
    observed = stage_evidence(events, contract["identity"]["model_id"], config["role"])
    c.require(1 <= observed["requests"] == len(receipt["captures"]) <= CAPS["max_requests"]
              and observed["output_tokens"] <= CAPS["output_token_budget"], "Missing/over-budget capture")
    requests = [e for e in events if e.get("kind") == "provider_request"]
    responses = [e for e in events if e.get("kind") == "assistant"]
    c.require(len(requests) == len(responses) == observed["requests"]
              and [e["requests"] for e in requests] == list(range(1, len(requests)+1)), "Incomplete request trace")
    output_total = 0
    for e in responses:
        cap, tokens = e["responseTokenCap"], e["responseTokens"]
        c.require(type(cap) is int and type(tokens) is int and 0 <= tokens <= cap <= CAPS["max_response_tokens"],
                  "Generation response cap exceeded")
        output_total += tokens
        c.require(e["outputTokens"] == output_total <= CAPS["output_token_budget"], "Generation cumulative usage changed")
    c.require(output_total == observed["output_tokens"], "Generation final usage differs")
    previous = expected
    for n, h in enumerate(receipt["captures"], 1):
        closure_path = directory / f"http-{n}.complete.json"
        c.require(sha256(closure_path.read_bytes()) == h, "Capture closure changed")
        closure = load(closure_path)
        c.require(closure["number"] == n and closure["previous_sha256"] == previous, "Capture chain gap/replay")
        previous = h
        for name, digest in closure["files"].items():
            c.require(sha256((directory / name).read_bytes()) == digest, "Capture member changed")
        admission = load(directory / f"http-{n}.admission.json")
        prefix = [json.loads(l) for l in (directory / f"http-{n}.trace-prefix").read_bytes().splitlines()]
        raw = (directory / f"http-{n}.json").read_bytes()
        c.require(admission["number"] == n and admission["freeze_sha256"] == expected
                  and admission["process"] == receipt["process"], "Admission identity changed")
        wire_matches(contract, directory, n, raw, config, prefix)
        http = load(directory / f"http-{n}.receipt.json")
        c.require(http["accepted"] and http["forwarded"] and not http.get("error")
                  and http["payload_sha256"] == sha256(raw)
                  and http["generation_admission_sha256"] == sha256((directory / f"http-{n}.admission.json").read_bytes()),
                  "HTTP capture unbound/incomplete")
    if config["role"] == "planner":
        c.canonical_delivery(contract, events)
    else:
        c.require(observed["governance_ack"]["details"]["policySha256"]
                  == contract["frozen"]["policy_sha256"], "Implementer policy changed")
    return receipt


def run_native_generation(freeze_path, expected):
    c = helpers()
    contract = frozen(freeze_path, expected)
    with exclusive_lock(Path(contract["directory"]) / "resource.lock"):
        started = time.monotonic()
        receipts = [run_generation_session(freeze_path, expected, i) for i in range(2)]
        c.require(time.monotonic()-started <= CAPS["job_seconds"], "Generation job timeout")
        c.require(len({(r["process"]["pid"], r["process"]["created_at"]) for r in receipts}) == 2
                  and len({r["session_id"] for r in receipts}) == 2, "Generation sessions reused")
        result = {"schema": "ephy.triage-generation-capture.v1", "freeze_sha256": expected,
                  "sessions": ["planner", "implementer"], "synthetic_only": True,
                  "real_model_generations": 0, "candidate_sha256": sha256(
                      (Path(contract["directory"]) / "implementer/input" / SKILL_PATH).read_bytes())}
        write_capture(contract, Path(contract["directory"]) / "capture.json", c.json_bytes(result))
    return result


def planner_completion_evidence(contract, expected, job, receipt, stop, planner):
    """Require the complete known workflow inside the bound whole-run window."""
    c = helpers()
    timing_path = Path(contract["directory"]) / "planner-completion.json"
    timing = load(timing_path)
    c.require(set(timing) == {"schema", "job_id", "freeze_sha256", "submission_sha256",
                              "started_at", "finished_at", "elapsed_seconds"}
              and timing["schema"] == "ephy.triage-planner-completion.v1"
              and timing["job_id"] == job["id"] and timing["freeze_sha256"] == expected
              and timing["submission_sha256"] == contract["generation_submission_sha256"],
              "Planner-only whole-job timing binding changed")
    c.require(all(type(timing.get(key)) in (int, float) and math.isfinite(timing[key])
                  for key in ("started_at", "finished_at", "elapsed_seconds")),
              "Planner-only whole-job timing unknown")
    start, finish, elapsed = (timing[key] for key in ("started_at", "finished_at", "elapsed_seconds"))
    c.require(type(contract["created_at"]) in (int, float) and math.isfinite(contract["created_at"])
              and 0 < contract["created_at"] <= start <= finish
              and 0 <= elapsed <= job["contract"]["timeout_seconds"]
              and finish - start <= job["contract"]["timeout_seconds"]
              and abs((finish - start) - elapsed) <= 0.5,
              "Planner-only whole-job deadline exceeded or clock evidence changed")
    workflow_path = Path(job["jobDir"]) / "workflow.jsonl"
    raw = workflow_path.read_bytes()
    c.require(raw.endswith(b"\n"), "Planner-only workflow incomplete")
    workflow = [json.loads(line) for line in raw.splitlines()]
    stages = ["preflight", "model loaded", "planner start", "planner end", "planner stop"]
    c.require(len(workflow) == len(stages)
              and all(isinstance(event, dict) and event.get("stage") == stage
                      and isinstance(event.get("at"), str) for event, stage in zip(workflow, stages)),
              "Planner-only workflow has missing, unknown or non-planner stages")
    dates = [datetime.fromisoformat(event["at"]) for event in workflow]
    c.require(all(date.utcoffset() is not None for date in dates), "Planner-only workflow time lacks timezone")
    times = [date.timestamp() for date in dates]
    c.require(times == sorted(times) and start <= times[0] <= times[-1] <= finish
              and times[2] <= receipt["process"]["created_at"] <= times[3]
              and times[3] - times[2] <= job["contract"]["stage_seconds"],
              "Planner-only workflow escapes the bound runner/stage window")
    model = contract["identity"]["model_id"]
    c.require(workflow[0].get("passed") is True
              and workflow[0].get("contract_sha256") == contract["generation_binding"]["contract_sha256"]
              and workflow[0].get("snapshot_sha256") == stop["snapshot_sha256"]
              and workflow[1].get("model") == model and workflow[2].get("label") == "planner"
              and workflow[2].get("expected_model") == model and workflow[3].get("label") == "planner"
              and type(workflow[3].get("pid")) is int and workflow[3]["pid"] == receipt["process"]["pid"]
              and workflow[3].get("trace_sha256") == sha256((planner / "trace.jsonl").read_bytes())
              and {key: value for key, value in workflow[4].items() if key not in {"stage", "at"}} == stop,
              "Planner-only workflow binding changed")
    return sha256(raw), sha256(timing_path.read_bytes())


def planner_capture_result(contract, expected):
    """Derive planner completion from bound saved evidence, never a report flag."""
    c = helpers()
    c.require(planner_only_mode(contract), "Planner-only capture requires its frozen mode")
    binding = contract["generation_binding"]
    c.require(binding is not None, "Planner-only capture requires a bound external job")
    job_dir = Path(binding["job_dir"])
    job = load(Path(contract.get("generation_job_path") or job_dir / "job.json"))
    c.require(generation_submission_sha256(job) == contract.get("generation_submission_sha256"),
              "Planner-only submission identity changed")
    c.require(job.get("environment_sha256") == contract.get("generation_environment_sha256"),
              "Planner-only submission identity environment changed")
    c.validate_generation_budget(job)
    c.require(planner_only_mode(job["contract"]) and binding == {
        "job_id": job["id"], "base": job["baseRevision"],
        "contract_sha256": sha256(encode(job["contract"])),
        "runtime_sha256": sha256(encode(job["runtime"])),
        "job_dir": job["jobDir"], "worktree": job["worktreePath"],
    }, "Planner-only frozen job/flag changed")
    directory = Path(contract["directory"])
    c.require(not (directory / "implementer").exists()
              and not any(path.name.casefold().startswith(("worker-", "implementer", "auditor", "audit-"))
                          for path in job_dir.iterdir())
              and not any((job_dir / name).exists() for name in
                          ("audit-bundle", "audit-result.json", "candidate.patch")),
              "Planner-only capture contains implementation or audit evidence")
    planner = directory / "planner"
    receipt = verify_session(Path(contract["directory"]) / "freeze.json", expected, planner)
    c.require(receipt["role"] == "planner"
              and type(receipt["elapsed_seconds"]) in (int, float)
              and 0 <= receipt["elapsed_seconds"] <= job["contract"]["stage_seconds"],
              "Planner-only session role/deadline changed")
    candidate = Path(binding["worktree"])
    c.require(receipt["input_before"] == receipt["input_after"] == c.snapshot(candidate),
              "Planner-only candidate changed")
    plan = (job_dir / "lead-plan.txt").read_text(encoding="utf-8")
    c.require(plan.strip()
              and (planner / "output.txt").read_bytes() == plan.encode("utf-8")
              and final_assistant_text(planner / "session.jsonl", session=True) == plan,
              "Planner-only final plan missing or substituted")
    stop_path = job_dir / "planner-stop.json"
    stop = load(stop_path)
    c.require(stop == {
        "job_id": job["id"], "contract_sha256": binding["contract_sha256"],
        "plan_sha256": sha256(plan.encode("utf-8")), "snapshot_sha256": snapshot_hash(candidate),
        "implementation_started": False, "formal_audit_executed": False, "automatic_adoption": False,
    } and all(stop[name] is False for name in
              ("implementation_started", "formal_audit_executed", "automatic_adoption")),
              "Planner-only stop binding changed")
    c.require(job["status"] == "planner_stopped"
              and job["outcome"] == ("no_hypothesis" if plan.strip() == "NO_HYPOTHESIS" else "planned_only"),
              "Planner-only job did not stop successfully")
    workflow_sha, completion_sha = planner_completion_evidence(contract, expected, job, receipt, stop, planner)
    events = events_at(planner)
    observed = stage_evidence(events, contract["identity"]["model_id"], "planner")
    c.require(type(observed["requests"]) is int
              and 1 <= observed["requests"] <= job["contract"]["max_requests"]
              and type(observed["output_tokens"]) is int
              and 0 <= observed["output_tokens"] <= job["contract"]["output_token_budget"],
              "Planner-only usage unknown or over budget")
    responses = [event for event in events if event.get("kind") == "assistant"]
    for number, response in enumerate(responses, 1):
        raw = load(planner / f"http-{number}.json")
        cap = raw.get("max_tokens")
        lines = [line[5:].strip() for line in (planner / f"http-{number}.response").read_bytes().splitlines()
                 if line.startswith(b"data:")]
        reports = [load_line["usage"] for line in lines if line != b"[DONE]"
                   and isinstance((load_line := json.loads(line)), dict) and load_line.get("usage")]
        c.require(lines and lines[-1] == b"[DONE]" and lines.count(b"[DONE]") == 1
                  and len(reports) == 1 and isinstance(reports[0], dict),
                  "Planner-only HTTP usage incomplete")
        usage = reports[0]
        c.require(type(cap) is int and 0 < cap <= job["contract"]["max_response_tokens"]
                  and cap == response["responseTokenCap"]
                  and all(type(usage.get(key)) is int for key in
                          ("prompt_tokens", "completion_tokens", "total_tokens"))
                  and 0 <= usage["completion_tokens"] == response["responseTokens"] <= cap
                  and usage["prompt_tokens"] >= 0
                  and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"],
                  "Planner-only HTTP/trace usage unknown or changed")
        if number == len(responses):
            choices = [choice for line in lines if line != b"[DONE]"
                       for choice in json.loads(line).get("choices", [])]
            contents = [choice.get("delta", {}).get("content") for choice in choices]
            c.require(all(content is None or isinstance(content, str) for content in contents)
                      and "".join(content for content in contents if content is not None) == plan
                      and [choice["finish_reason"] for choice in choices
                           if choice.get("finish_reason") is not None] == ["stop"],
                      "Planner-only HTTP final plan differs or did not stop")
    return {
        "schema": "ephy.triage-planner-capture.v1", "freeze_sha256": expected,
        "job_id": job["id"], "contract_sha256": binding["contract_sha256"], "sessions": ["planner"],
        "synthetic_only": contract["identity"]["model_id"] == FAKE_MODEL,
        "real_model_generations": (0 if contract["identity"]["model_id"] == FAKE_MODEL else observed["requests"]),
        "implementation_requests": 0, "requests": observed["requests"], "output_tokens": observed["output_tokens"],
        "plan_sha256": stop["plan_sha256"], "stop_sha256": sha256(stop_path.read_bytes()),
        "candidate_snapshot_sha256": stop["snapshot_sha256"],
        "submission_sha256": contract["generation_submission_sha256"],
        "workflow_sha256": workflow_sha, "completion_sha256": completion_sha,
    }


def verify_capture(freeze_path, expected):
    contract = frozen(freeze_path, expected)
    result = load(Path(contract["directory"]) / "capture.json")
    c = helpers()
    if contract["generation_binding"] is not None:
        binding = contract["generation_binding"]
        job = load(Path(contract.get("generation_job_path") or Path(binding["job_dir"]) / "job.json"))
        c.require(planner_only_mode(job["contract"]) == planner_only_mode(contract)
                  and sha256(encode(job["contract"])) == binding["contract_sha256"],
                  "Generation capture frozen job/flag changed")
    if planner_only_mode(contract):
        c.require(type(result.get("synthetic_only")) is bool
                  and all(type(result.get(name)) is int for name in
                          ("real_model_generations", "implementation_requests", "requests", "output_tokens"))
                  and result == planner_capture_result(contract, expected),
                  "Planner-only capture report differs from saved evidence")
        return result
    c.require(result["schema"] == "ephy.triage-generation-capture.v1"
              and result["freeze_sha256"] == expected and result["sessions"] == ["planner", "implementer"],
              "Incomplete generation capture")
    receipts = [verify_session(freeze_path, expected, Path(contract["directory"])/r) for r in result["sessions"]]
    c.require(len({r["session_id"] for r in receipts}) == 2
              and len({(r["process"]["pid"], r["process"]["created_at"]) for r in receipts}) == 2,
              "Capture session/process reused")
    candidate = (Path(contract["generation_binding"]["worktree"]) / SKILL_PATH
                 if contract["generation_binding"] else Path(contract["directory"]) / "implementer/input" / SKILL_PATH)
    c.require(result["candidate_sha256"] == sha256(candidate.read_bytes()), "Candidate capture changed")
    return result




# The adapter composes the existing external runner. It does not replace its
# baseline, independent checks, proposal freeze, stop, or external review gates.
class IsolatedStrataRunner(StrataRunner):
    def __init__(self, job_file, freeze_path, expected):
        self._isolation_started_at = time.time()
        self._isolation_started_monotonic = time.monotonic()
        self.isolation_path, self.isolation_expected = freeze_path, expected
        self.isolation = frozen(freeze_path, expected)
        super().__init__(job_file)
        c = helpers()
        c.validate_resource_lock_pins(self.runtime["resource_lock"], self.isolation["pins"])
        binding = self.isolation["generation_binding"]
        c.require(binding is not None and binding == {
            "job_id": self.job["id"], "base": self.job["baseRevision"],
            "contract_sha256": sha256(encode(self.contract)),
            "runtime_sha256": sha256(encode(self.runtime)), "job_dir": self.job["jobDir"],
            "worktree": self.job["worktreePath"]},
            "Generation freeze belongs to another external job")
        c.require(self.identity == self.isolation["identity"], "Generation deployment differs")
        c.validate_generation_budget(self.job)
        if self.isolation.get("generation_job_path") is not None:
            c.require(Path(self.isolation["generation_job_path"]).resolve() == self.job_file,
                      "Generation freeze belongs to another job file")
        self._planner_only = planner_only_mode(self.contract)
        c.require(self._planner_only == planner_only_mode(self.isolation),
                  "Generation freeze planner_only differs from job")
        c.require(not self._planner_only or generation_submission_sha256(self.job)
                  == self.isolation.get("generation_submission_sha256"),
                  "Planner-only submission identity differs from freeze")
        c.require(all(self.runtime.get("thinking", {}).get(role) == "off" for role in ("planner", "implementer")),
                  "Isolated generation requires planner/implementer thinking=off")
        required = [str(Path(__file__).resolve()), self.isolation["runtime"]["extra_guard"],
                    str(Path(__file__).with_name("verifier_triage_consumer.py").resolve())]
        c.require(all(self.contract["runtime_hashes"].get(p) == sha256(Path(p).read_bytes()) for p in required),
                  "Isolation adapter/guard must be pinned before submission")
        self.isolation_stages = []
        self.active_isolation = None

    def resources(self):
        super().resources()
        capture_budget(self.isolation, 0)

    def stage(self, role, prompt, label, root, envelope=None):
        c = helpers()
        c.require(not self._planner_only or role == "planner", "Planner-only forbids non-planner capture")
        c.require(len(self.isolation_stages) < 2
                  and role == ("planner", "implementer")[len(self.isolation_stages)], "Generation stage replay/order")
        directory = Path(self.isolation["directory"]) / role
        c.require(not directory.exists(), "Generation capture session reused")
        directory.mkdir()
        (directory/"previews").mkdir()
        self.active_isolation = (directory, role, prompt)
        try:
            text = super().stage(role, prompt, label, root, envelope)
        finally:
            self.active_isolation = None
        gateway = self._generation_gateway
        c.require(not gateway.failure, "External generation capture rejected")
        for name, source in (("trace.jsonl", self.directory/(label+"-trace.jsonl")),
                             ("stdout.jsonl", self.directory/(label+".stdout.log")),
                             ("session.jsonl", self.directory/(label+"-session.jsonl"))):
            body = source.read_bytes()
            self._generation_capture.guard_write(len(body))
            write_capture(self.isolation, directory/name, body)
        c.require(text == final_assistant_text(directory/"session.jsonl", session=True),
                  "External stdout/session differs")
        write_capture(self.isolation, directory/"output.txt", text.encode())
        config = load(directory/"config.json")
        after = c.snapshot(root)
        c.require(all(after.get(k) == v for k, v in config["input_snapshot"].items()
                      if k != SKILL_PATH)
                  and set(after)-set(config["input_snapshot"]) <= ({SKILL_PATH} if role == "implementer" else set()),
                  "External generation changed closed input scope")
        self._generation_capture.check_prefix()
        process = load(directory/"process.json")
        transcript = self.transcripts[-1]
        c.require(transcript["pid"] == process["pid"] and transcript["exit_code"] == 0,
                  "External generation process/exit differs")
        receipt = {"schema": "ephy.triage-generation-session.v1", "role": role,
                   "session_id": config["session_id"], "freeze_sha256": self.isolation_expected,
                   "process": process, "exit_code": 0,
                   "elapsed_seconds": self._generation_elapsed,
                   "input_before": config["input_snapshot"], "input_after": after,
                   "captures": self._generation_capture.completed,
                   "files": {p.relative_to(directory).as_posix(): sha256(p.read_bytes())
                             for p in directory.rglob("*") if p.is_file()}}
        write_capture(self.isolation, directory/"receipt.json", c.json_bytes(receipt))
        verify_session(self.isolation_path, self.isolation_expected, directory)
        self.isolation_stages.append(role)
        return text

    def command(self, argv, cwd, label, seconds, stage_environment=None):
        if self.active_isolation is None:
            return super().command(argv, cwd, label, seconds, stage_environment)
        c = helpers()
        directory, _role, prompt = self.active_isolation
        config = load(Path(stage_environment["EPHY_FORMAL_STAGE_CONFIG"]))
        prompt_path = self.directory/(label+"-prompt.txt")
        raw_prompt = prompt_path.read_bytes()
        c.require(raw_prompt == prompt.replace("\n", os.linesep).encode("utf-8"),
                  "External authoring prompt differs from the controlled task")
        write_capture(self.isolation, directory/"prompt.txt", raw_prompt)
        inputs = c.snapshot(cwd)
        allowed_reads = [name for name in inputs if Path(name).suffix in {".md", ".json", ".toml", ".yaml", ".yml"}
                         and not name.startswith(".git/")]
        for name in allowed_reads:
            deny_leak(self.isolation, (cwd/name).read_text(encoding="utf-8", errors="strict"))
        config.update(session_id=str(uuid.uuid4()), prompt=raw_prompt.decode("utf-8"), input_snapshot=inputs,
                      capture_root=self.isolation["directory"], max_log_bytes=self.isolation["max_log_bytes"],
                      log_root=self.isolation.get("log_root"),
                      allowed_reads=allowed_reads, previews=str(directory/"previews"),
                      prompt_path=str(prompt_path),
                      trace_path=stage_environment["EPHY_FORMAL_TRACE"])
        marker = argv.index(self.runtime["governance_gate"])
        c.require(argv[marker-1] == "--extension", "Missing final governance extension")
        argv = [*argv[:marker-1], "--extension", self.isolation["runtime"]["extra_guard"], *argv[marker-1:]]
        config["argv"] = argv
        config_raw = c.json_bytes(config)
        capture_budget(self.isolation, len(config_raw))
        write_capture(self.isolation, directory/"config.json", config_raw)
        capture = GenerationCapture(self.isolation_path, self.isolation_expected, directory)
        gateway = c.Gateway(self.isolation, directory, "generation", generation_capture=capture)
        with gateway as origin:
            identity_path = directory/"provider-identity.json"
            write_capture(self.isolation, identity_path, c.json_bytes({"base_url": origin, "model_id": self.identity["model_id"],
                                                   "context": self.identity["context"]}))
            env = {**stage_environment, "EPHY_TRIAGE_GENERATION_CONFIG": str(directory/"config.json"),
                   "EPHY_STRATA_IDENTITY": str(identity_path)}
            started = time.monotonic()
            result = super().command(argv, cwd, label, seconds, env)
            self._generation_elapsed = time.monotonic()-started
        self._generation_gateway, self._generation_capture = gateway, capture
        return result

    def run(self):
        # Direct callers and the supported entrypoint hold the same existing
        # lock as consumers and other external jobs for the complete workflow.
        with exclusive_lock(Path(self.runtime["resource_lock"])):
            return self._run_isolated()

    def _run_isolated(self):
        super().run()
        c = helpers()
        if self._planner_only:
            c.require(self.isolation_stages == ["planner"], "Planner-only external capture incomplete")
            elapsed = time.monotonic() - self._isolation_started_monotonic
            c.require(0 <= elapsed <= self.contract["timeout_seconds"], "Planner-only whole-job deadline exceeded")
            completion = {"schema": "ephy.triage-planner-completion.v1", "job_id": self.job["id"],
                          "freeze_sha256": self.isolation_expected,
                          "submission_sha256": self.isolation["generation_submission_sha256"],
                          "started_at": self._isolation_started_at, "finished_at": time.time(),
                          "elapsed_seconds": elapsed}
            write_capture(self.isolation, Path(self.isolation["directory"]) / "planner-completion.json",
                          c.json_bytes(completion))
            result = planner_capture_result(frozen(self.isolation_path, self.isolation_expected),
                                            self.isolation_expected)
            write_capture(self.isolation, Path(self.isolation["directory"]) / "capture.json", c.json_bytes(result))
            return verify_capture(self.isolation_path, self.isolation_expected)
        c.require(self.isolation_stages == ["planner", "implementer"]
                  and self.job["status"] == "external_review_pending", "External proposal incomplete")
        result = {"schema": "ephy.triage-generation-capture.v1",
                  "freeze_sha256": self.isolation_expected, "sessions": self.isolation_stages,
                  "synthetic_only": self.identity["model_id"] == FAKE_MODEL,
                  "real_model_generations": (0 if self.identity["model_id"] == FAKE_MODEL
                                             else sum(self.provenance[i]["requests"] for i in range(2))),
                  "candidate_sha256": sha256((self.candidate/SKILL_PATH).read_bytes())}
        write_capture(self.isolation, Path(self.isolation["directory"])/"capture.json", c.json_bytes(result))
        verify_capture(self.isolation_path, self.isolation_expected)


def run_isolated_external_job(job_file, freeze_path, expected):
    runner = IsolatedStrataRunner(job_file, freeze_path, expected)
    runner.run()
    return verify_capture(freeze_path, expected)


def _verify_job_binding(job_path, freeze_path, expected, batch_sha, gold_sha, skill_sha):
    """Shared complete binding; caller separately enforces live/native domain."""
    c = helpers()
    contract = frozen(freeze_path, expected)
    c.require(not planner_only_mode(contract), "Planner-only capture cannot authorize consumer evaluation")
    result = verify_capture(freeze_path, expected)
    job = load(job_path)
    binding = contract["generation_binding"]
    c.require(contract["task"] == job["contract"]["task"], "Generation task differs from frozen job")
    c.require(binding == {"job_id": job["id"], "base": job["baseRevision"],
                          "contract_sha256": sha256(encode(job["contract"])),
                          "runtime_sha256": sha256(encode(job["runtime"])), "job_dir": job["jobDir"],
                          "worktree": job["worktreePath"]},
              "Isolation capture belongs to another generation job")
    c.require((contract["batch_sha256"], contract["gold_sha256"], result["candidate_sha256"])
              == (batch_sha, gold_sha, skill_sha), "Isolation capture covers other evaluation inputs")
    c.require(contract["identity"] == load(Path(job["runtime"]["models_ini"])), "Capture model differs")
    # Bind captured sessions to the existing, independently checked proposal's
    # actual generation processes. A separate clean native run cannot stand in.
    provenance = load(Path(job["jobDir"])/"audit-bundle/model_provenance.txt")
    c.require([p["role"] for p in provenance] == ["planner", "implementer"], "Incomplete generation provenance")
    for p in provenance:
        directory = Path(contract["directory"])/p["role"]
        r = verify_session(freeze_path, expected, directory)
        c.require(p["pid"] == r["process"]["pid"]
                  and p["trace_sha256"] == sha256((directory/"trace.jsonl").read_bytes())
                  and p["session_sha256"] == sha256((directory/"session.jsonl").read_bytes()),
                  "Capture belongs to another actual generation session")
    return result


def verify_live_binding(job_path, freeze_path, expected, batch_sha, gold_sha, skill_sha):
    contract = frozen(freeze_path, expected)
    result = verify_capture(freeze_path, expected)
    helpers().require(contract["identity"]["model_id"] != FAKE_MODEL
                      and contract["identity"]["listener"]["pid"] != contract["identity"]["engine"]["pid"]
                      and result["synthetic_only"] is False, "Native capture cannot authorize a live trial")
    return _verify_job_binding(job_path, freeze_path, expected, batch_sha, gold_sha, skill_sha)


def verify_native_binding(job_path, freeze_path, expected, batch_sha, gold_sha, skill_sha, identity):
    helpers().require(identity["model_id"] == FAKE_MODEL
                      and identity["listener"]["pid"] == identity["engine"]["pid"] == os.getpid(),
                      "Native binding requires controller-owned scripted provider")
    contract = frozen(freeze_path, expected)
    helpers().require(contract["identity"] == identity, "Native capture uses other provider")
    return _verify_job_binding(job_path, freeze_path, expected, batch_sha, gold_sha, skill_sha)


def main():
    parser = argparse.ArgumentParser(description="Independent saved generation-capture verification; no generation")
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(verify_capture(args.freeze, args.sha256)))


if __name__ == "__main__":
    main()
