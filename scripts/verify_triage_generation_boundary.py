"""Real Pi and owned scripted provider controls for closed generation capture."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from ephy_worker.strata_runtime import process_identity
from ephy_worker.verifier_triage_consumer import json_bytes, payload_strings
from ephy_worker.verifier_triage_evaluation import BASE_REVISION, SKILL_PATH, sha256
from ephy_worker.verifier_triage_fixtures import development_fixture, development_skill
from ephy_worker.verifier_triage_isolation import (
    FAKE_MODEL,
    TASK,
    freeze_generation,
    run_isolated_external_job,
    run_native_generation,
    verify_live_binding,
    verify_native_binding,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = ("good", "missing", "batch_leak", "gold_leak", "duplicate", "session", "hash", "tool_substitution",
         "freeze_change")
REASONS = {"missing": "FileNotFoundError", "batch_leak": "Held-out/gold leakage",
           "gold_leak": "Held-out/gold leakage", "duplicate": "already failed",
           "session": "Wrong generation session", "hash": "Earlier captured file changed",
           "tool_substitution": "Tool-result bytes substituted", "freeze_change": "Frozen artifact changed"}
ADAPTER_CASES = ("adapter_good", "adapter_lock", "adapter_capture_cap", "adapter_thinking",
                 "invalid_duplicate", "invalid_answer", "invalid_evidence", "invalid_diagnosis", "invalid_allstop",
                 "invalid_unattributed_component", "invalid_unattributed_stale_ci",
                 "invalid_match_stale_ci", "invalid_component_stale_ci", "invalid_ci_schema",
                 "adapter_missing", "adapter_batch_leak", "adapter_gold_leak", "adapter_runtime_change", "adapter_rebound_lock")


def run_adapter_case(pi, directory, case):
    """Unmocked external runner, independent checks, proposal and consumer admission."""
    from ephy_worker import formal_runtime as formal
    from ephy_worker.strata_runtime import verify_external_proposal
    from ephy_worker.verifier_triage_consumer import BATCH_PATH, PROMPT, build_contract, run_contract

    if directory.exists():
        raise ValueError("Fresh adapter artifacts required")
    directory.mkdir(parents=True)
    # Process-local resolution only; reuse already installed Windows executables.
    if os.name == "nt":
        git = Path("C:/Program Files/Git/cmd/git.exe")
        os.environ["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), str(git.parent), os.environ["PATH"]))
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    repository = directory/"repository"
    cloned = subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(ROOT), str(repository)],
                            capture_output=True, check=True)
    (directory/"clone.stdout").write_bytes(cloned.stdout)
    (directory/"clone.stderr").write_bytes(cloned.stderr)
    batch, gold = development_fixture()
    rows, answers = json.loads(batch), json.loads(gold)
    if case == "invalid_duplicate":
        rows[-1] = rows[0]
    elif case == "invalid_answer":
        answers[0]["decision"] = "MAYBE"
    elif case == "invalid_evidence":
        answers[0]["evidence_ids"] = ["nonexistent", "another-missing"]
    elif case == "invalid_diagnosis":
        answers[0]["diagnosis"] = "component:invented"
    elif case == "invalid_allstop":
        for answer in answers:
            answer["decision"] = "STOP"
    elif case == "invalid_unattributed_component":
        answers[0]["diagnosis"] = "UNATTRIBUTED"
    elif case == "invalid_unattributed_stale_ci":
        data = json.loads(rows[8]["input"])
        data["observed"]["aggregate"] = "b"*64
        rows[8]["input"] = json.dumps(data, separators=(",", ":"))
        answers[8]["diagnosis"] = "UNATTRIBUTED"
        answers[8]["evidence_ids"] = ["e", "o"]
    elif case in {"invalid_match_stale_ci", "invalid_component_stale_ci", "invalid_ci_schema"}:
        data = json.loads(rows[8]["input"])
        if case == "invalid_ci_schema":
            data["ci"]["observed_revision"] = 42
        else:
            answers[8]["evidence_ids"] = ["e", "o"]
            if case == "invalid_match_stale_ci":
                answers[8]["decision"], answers[8]["diagnosis"] = "PASS", "MATCH"
            else:
                data["expected"]["components"] = {"PATH": "a"*64}
                data["observed"]["components"] = {"PATH": "b"*64}
                data["observed"]["aggregate"] = "b"*64
                answers[8]["diagnosis"] = "component:PATH"
        rows[8]["input"] = json.dumps(data, separators=(",", ":"))
    batch, gold = json_bytes(rows), json_bytes(answers)
    for name, data in (("batch.json", batch), ("gold.json", gold), ("deployment.json", b'{"synthetic":true}')):
        (directory/name).write_bytes(data)
    counter = directory/"counter.py"
    counter.write_text("import hashlib,json,sys\nb=sys.stdin.buffer.read()\n"
                       f"print(json.dumps({{'payload_sha256':hashlib.sha256(b).hexdigest(),"
                       f"'model_id':{FAKE_MODEL!r},'input_tokens':4000,'synthetic_counter':True}}))\n", encoding="utf-8")
    checker = directory/"checker.py"
    checker.write_text("import hashlib,sys\nfrom pathlib import Path\n"
                       f"p=Path(sys.argv[1])/{SKILL_PATH!r}\n"
                       f"ok=p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()=={sha256(development_skill())!r}\n"
                       "ok=ok and not (Path(sys.argv[1])/'weakening.flag').exists() and not (Path(sys.argv[1])/'escape.md').exists()\n"
                       "sys.exit(0 if ok else 1)\n", encoding="utf-8")
    controls = []
    for name in ("known_good", "unchanged", "wrong_answer", "test_weakening", "scope_escape"):
        control_root = directory/"controls"/name
        (control_root/SKILL_PATH).parent.mkdir(parents=True)
        if name != "unchanged":
            (control_root/SKILL_PATH).write_bytes(b"wrong answer\n" if name == "wrong_answer" else development_skill())
        if name in ("test_weakening", "scope_escape"):
            (control_root/("weakening.flag" if name == "test_weakening" else "escape.md")).write_bytes(b"forbidden\n")
        result = subprocess.run([sys.executable, str(checker), str(control_root)], capture_output=True, check=False)
        controls.append({"name": name, "actual": result.returncode, "expected": 0 if name == "known_good" else 1})
    controls_path = directory/"checker-controls.json"
    controls_path.write_bytes(json_bytes(controls))
    state = {"posts": 0, "nonces": set()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            data = ({"data": [{"id": FAKE_MODEL, "status": {"value": "loaded"}, "meta": {"n_ctx": 131072}}]}
                    if self.path == "/v1/models" else {"service": "strata", "loaded": True, "api_key": False,
                                                       "model": FAKE_MODEL, "max_context": 131072})
            body = json_bytes(data)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"] += 1
            if state["posts"] == 2:
                if case == "adapter_missing":
                    (directory/"run/planner/http-1.complete.json").unlink()
                elif case in {"adapter_batch_leak", "adapter_gold_leak"}:
                    (directory/"candidate/README.md").write_bytes(batch if case == "adapter_batch_leak" else gold)
            strings = "\n".join(payload_strings(payload["messages"]))
            nonce = re.search(r"Acknowledgement-Nonce: ([^\s]+)", strings).group(1)
            state["nonces"].add(nonce)
            role = re.search(r"Role: ([^\s]+)", strings).group(1)
            calls, text = [], None
            results = [m for m in payload["messages"] if m.get("role") == "tool"]
            if "Acknowledgement-State: required" in strings:
                calls = [("governance_ack", {"policyId": "ephy.system-development-governance.v1",
                          "policySha256": re.search(r"Policy-SHA256: ([a-f0-9]{64})", strings).group(1),
                          "endMarker": "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1", "nonce": nonce, "role": role})]
            elif PROMPT in strings:
                treatment = "Treatment: explicitly read " in strings
                if any(batch.decode() in "\n".join(payload_strings(m.get("content", []))) for m in results):
                    output = json.loads(gold)
                    if not treatment:
                        for answer in [a for a in output if a["diagnosis"].startswith("component:")][:3]:
                            answer["diagnosis"] = "UNATTRIBUTED"
                    text = json.dumps({"skill_sha256": sha256(development_skill()) if treatment else None,
                                       "answers": output})
                else:
                    calls = [("read", {"path": BATCH_PATH})]
                    if treatment:
                        calls.append(("read", {"path": SKILL_PATH}))
            elif not any(m.get("tool_call_id", "").startswith("adapter-read-") for m in results):
                calls = [("read", {"path": "README.md"})]
            elif role == "planner":
                text = "Write the scoped verifier triage skill using measured paired evidence and STOP for unresolved evidence."
            elif not (directory/"candidate"/SKILL_PATH).exists():
                calls = [("write", {"path": SKILL_PATH, "content": development_skill().decode()})]
            else:
                text = "DONE"
            delta = {"role": "assistant"}
            if calls:
                delta["tool_calls"] = [{"index": index, "id": f"adapter-{name}-{state['posts']}-{index}",
                                       "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                                      for index, (name, args) in enumerate(calls)]
            else:
                delta["content"] = text
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 4000, "completion_tokens": 100, "total_tokens": 4100}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                chunk.update(id="chatcmpl-adapter-control", object="chat.completion.chunk", created=1, model=FAKE_MODEL)
                self.wfile.write(("data: "+json.dumps(chunk)+"\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    result, consumer_result, error, live_rejection = None, None, None, None
    try:
        identity = {"schema": "ephy.existing-strata.v1", "base_url": f"http://127.0.0.1:{server.server_port}",
                    "model_id": FAKE_MODEL, "context": 131072, "listener": process_identity(os.getpid()),
                    "engine": process_identity(os.getpid()), "configuration": {
                        "path": str(directory/"deployment.json"), "sha256": sha256((directory/"deployment.json").read_bytes())}}
        identity_path = directory/"identity.json"
        identity_path.write_bytes(json_bytes(identity))
        managed = directory/"managed-pi"
        managed.mkdir()
        (managed/"settings.json").write_bytes(json_bytes({"retry": {"enabled": False}, "compaction": {"enabled": False}}))
        gate = directory/"runtime/.pi/extensions/governance-gate.ts"
        gate.parent.mkdir(parents=True)
        gate.write_bytes((ROOT/".pi/extensions/governance-gate.ts").read_bytes())
        for destination, source in {
            "policies/independent-audit.md": "audit_contract", "prompts/audit-ephy-worker.md": "audit_prompt",
            "policies/audit-input.schema.json": "audit_input_schema",
            "policies/evidence-manifest.schema.json": "evidence_manifest_schema",
            "policies/audit-result.schema.json": "audit_result_schema",
        }.items():
            target = gate.parent.parent/destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT/formal.CONTROL_PATHS[source]).read_bytes())
        runtime = {"backend": "external_strata", "provider_id": "strata-local", "review_mode": "external_codex",
                   "base_url": identity["base_url"], "models_ini": str(identity_path), "managed_dir": str(managed),
                   "python": str(Path(sys.executable).resolve()), "pi": str(pi), "server": str(Path(sys.executable).resolve()),
                   "provider": str(ROOT/"tools/pi-local/strata-provider.ts"),
                   "stage_guard": str(ROOT/"tools/pi-local/formal-stage-guard.ts"),
                   "governance_gate": str(gate),
                   "worker_agent": str(ROOT/"tools/pi-local/strata-worker.md"),
                   "runner": str(ROOT/"tools/pi-local/windows/run-background-job.ps1"),
                   "runner_test": str(ROOT/"tools/pi-local/windows/test-background-job-runner.ps1"),
                   "policy": str(ROOT/"docs/system-development-governance.md"), "governance_root": str(ROOT),
                   "resource_lock": str(directory/"shared-resource.lock"),
                   "model_roles": {r: FAKE_MODEL for r in ("planner", "implementer", "auditor")},
                   "model_manifests": {FAKE_MODEL: identity},
                   "thinking": {r: "off" for r in ("planner", "implementer", "auditor")}}
        if case == "adapter_thinking":
            runtime["thinking"]["planner"] = "medium"
        pins = [Path(v) for v in runtime.values() if isinstance(v, str) and Path(v).is_file()]
        pins += list((ROOT/"src/ephy_worker").rglob("*.py"))
        pins += [ROOT/name for name in formal.CONTROL_PATHS.values()]
        pins += [Path(p) for p in formal.injected_context_pins(runtime, {n: sha256((ROOT/p).read_bytes())
                                                                      for n, p in formal.CONTROL_PATHS.items()})]
        pins += [Path(shutil.which("git")), managed/"settings.json", ROOT/"tools/pi-local/formal-stage-stop.ts",
                 ROOT/"tools/pi-local/triage-generation-guard.ts", Path(identity["listener"]["executable"]),
                 Path(identity["engine"]["executable"]), Path(identity["configuration"]["path"]),
                 ROOT/"docs/self-improvement-mvp.md"]
        checks = [{"id": name, "argv": argv, "baseline_exit_code": baseline} for name, argv, baseline in (
            ("target", ["{python}", str(checker), "."], 1),
            ("regression", ["{python}", "-m", "pytest", "-q", "tests/test_strata_runtime.py", "--basetemp", str(directory/"check-temp")], 0),
            ("lint", ["{python}", "-m", "ruff", "check", "src/ephy_worker/strata_runtime.py"], 0),
            ("repository", ["{python}", "scripts/validate_repository.py"], 0),
            ("fixed", ["{python}", str(checker), "."], 1))]
        contract = {"allowed_files": [SKILL_PATH], "semantic_scope": "Public synthetic triage fixture skill only",
                    "task": "Read README.md completely. Author the scoped general evidence-triage Markdown skill; preserve STOP for unresolved evidence.",
                    "checks": checks, "checker": str(checker), "checker_sha256": sha256(checker.read_bytes()),
                    "checker_controls": str(controls_path), "checker_controls_sha256": sha256(controls_path.read_bytes()),
                    "max_repairs": 0, "timeout_seconds": 900, "stage_seconds": 300, "max_requests": 8,
                    "output_token_budget": 2200, "max_response_tokens": 1024, "minimum_free_ram_bytes": 4294967296,
                    "minimum_free_disk_bytes": 2147483648, "max_process_rss_bytes": 4294967296, "max_log_bytes": 8388608,
                    "runtime_hashes": {str(p.resolve()): sha256(p.read_bytes()) for p in pins}}
        model_ids = {r: {"model_id": FAKE_MODEL, "runtime_sha256": sha256(Path(runtime["server"]).read_bytes()),
                         "model_artifact_manifest_sha256": sha256(formal.encode(identity)),
                         "invocation_config_sha256": formal.invocation_identity(runtime, contract, r)}
                     for r in ("planner", "implementer", "auditor")}
        job_directory = directory/"job"
        job_directory.mkdir()
        job = {"schemaVersion": 2, "id": directory.name, "humanAuthorization": "explicit-execute-proposal-only",
               "baseRevision": BASE_REVISION, "repoRoot": str(repository),
               "jobDir": str(job_directory), "worktreePath": str(directory/"candidate"), "runtime": runtime,
               "contract": contract, "controls": {n: sha256((ROOT/p).read_bytes()) for n, p in formal.CONTROL_PATHS.items()},
               "model_identities": model_ids, "verifier_identity": formal.observed_verifier_identity(runtime, contract)}
        job_path = directory/"job.json"
        job_path.write_bytes(json_bytes(job))
        try:
            freeze = freeze_generation(ROOT, pi, identity, directory/"batch.json", directory/"gold.json",
                                       directory/"run", [sys.executable, str(counter)], generation_job=job_path)
            expected = sha256(freeze.read_bytes())
            if case == "adapter_runtime_change":
                job["runtime"]["resource_lock"] = str(directory/"different-resource.lock")
                job_path.write_bytes(json_bytes(job))
            if case == "adapter_capture_cap":
                size = sum(p.stat().st_size for p in freeze.parent.rglob("*") if p.is_file())
                (freeze.parent/"padding.bin").write_bytes(b"0"*(8388608-size-1024))
            if case == "adapter_lock":
                with formal.exclusive_lock(Path(runtime["resource_lock"])):
                    run_isolated_external_job(job_path, freeze, expected)
            else:
                result = run_isolated_external_job(job_path, freeze, expected)
                verify_external_proposal(job_path)
                if case == "adapter_rebound_lock":
                    changed_job = json.loads(job_path.read_bytes())
                    changed_job["runtime"]["resource_lock"] = str(directory/"different-resource.lock")
                    job_path.write_bytes(json_bytes(changed_job))
                skill = directory/"candidate"/SKILL_PATH
                verify_native_binding(job_path, freeze, expected, sha256(batch), sha256(gold), sha256(skill.read_bytes()), identity)
                try:
                    verify_live_binding(job_path, freeze, expected, sha256(batch), sha256(gold), sha256(skill.read_bytes()))
                except ValueError as exc:
                    live_rejection = str(exc)
                consumer = build_contract(ROOT, pi, identity, directory/"batch.json", directory/"gold.json", skill,
                                          directory/"consumer", [sys.executable, str(counter)], purpose="native_controls",
                                          generation_job=job_path, isolation_freeze=freeze, isolation_sha256=expected)
                consumer_result = run_contract(consumer, sha256(consumer.read_bytes()))
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            error = type(exc).__name__+": "+str(exc)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    reasons = {"adapter_lock": "owns the resource lock", "adapter_capture_cap": "cumulative log cap",
               "adapter_thinking": "thinking=off", "invalid_duplicate": "Duplicate case ID",
               "invalid_answer": "Unknown decision", "invalid_evidence": "nonexistent evidence",
               "invalid_diagnosis": "Unsupported gold diagnosis", "invalid_allstop": "Uninformative decision",
               "invalid_unattributed_component": "Unsupported unattributed answer",
               "invalid_unattributed_stale_ci": "Stale CI requires STOP/STALE_CI",
               "invalid_match_stale_ci": "Stale CI requires STOP/STALE_CI",
               "invalid_component_stale_ci": "Stale CI requires STOP/STALE_CI",
               "invalid_ci_schema": "Malformed CI evidence",
               "adapter_runtime_change": "Generation freeze belongs to another external job"}
    admission_errors = [json.loads(p.read_bytes()).get("error") for p in (directory/"run/planner").glob("http-*.receipt.json")]
    if case in {"adapter_missing", "adapter_batch_leak", "adapter_gold_leak"}:
        reason = "FileNotFoundError" if case == "adapter_missing" else "Held-out/gold leakage"
        passed = error is not None and state["posts"] == 2 and any(reason in (e or "") for e in admission_errors)
    elif case == "adapter_rebound_lock":
        passed = (error is not None and "Isolation capture belongs to another generation job" in error
                  and state["posts"] == 7 and len(state["nonces"]) == 2 and consumer_result is None)
    elif case == "adapter_good":
        passed = (error is None and result is not None and consumer_result is not None
                  and consumer_result["synthetic_generation_capture_verified"] and not consumer_result["live_execution_attested"]
                  and live_rejection and state["posts"] == 19 and len(state["nonces"]) == 6)
    else:
        passed = error is not None and reasons[case] in error and state["posts"] == 0
    summary = {"case": case, "passed": bool(passed), "fake_posts": state["posts"], "fresh_sessions": len(state["nonces"]),
               "error": error, "generation": result, "consumer": consumer_result, "live_domain_rejection": live_rejection,
               "admission_errors": admission_errors, "unmocked_adapter": True, "real_model_generations": 0}
    (directory/"summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    return summary


def run_case(pi, directory, case):
    if directory.exists():
        raise ValueError("Fresh native artifacts required")
    directory.mkdir(parents=True)
    batch, gold = development_fixture()
    for name, data in (("batch.json", batch), ("gold.json", gold), ("deployment.json", b'{"synthetic":true}')):
        (directory / name).write_bytes(data)
    counter = directory / "counter.py"
    counter.write_text("import hashlib,json,sys\nb=sys.stdin.buffer.read()\n"
                       f"print(json.dumps({{'payload_sha256':hashlib.sha256(b).hexdigest(),"
                       f"'model_id':{FAKE_MODEL!r},'input_tokens':4000,'synthetic_counter':True}}))\n",
                       encoding="utf-8")
    state = {"posts": 0, "nonces": set(), "duplicate_status": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            payload = ({"object": "list", "data": [{"id": FAKE_MODEL, "status": {"value": "loaded"},
                                                    "meta": {"n_ctx": 131072}}]} if self.path == "/v1/models"
                       else {"service": "strata", "loaded": True, "api_key": False, "model": FAKE_MODEL,
                             "max_context": 131072})
            body = json_bytes(payload)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            state["posts"] += 1
            strings = "\n".join(payload_strings(payload["messages"]))
            nonce = re.search(r"Acknowledgement-Nonce: ([^\s]+)", strings).group(1)
            state["nonces"].add(nonce)
            role = re.search(r"Role: ([^\s]+)", strings).group(1)
            stage = directory / "run" / role
            if state["posts"] == 2 and case != "good":
                if case == "missing":
                    (stage / "http-1.complete.json").unlink()
                elif case in ("batch_leak", "gold_leak"):
                    (stage / "input/authoring/task.md").write_bytes(batch if case == "batch_leak" else gold)
                elif case == "hash":
                    with (stage / "http-1.json").open("ab") as f:
                        f.write(b" ")
                elif case in ("session", "tool_substitution"):
                    path = stage / "trace.jsonl"
                    rows = [json.loads(l) for l in path.read_bytes().splitlines()]
                    for row in rows:
                        if case == "session" and row.get("kind") == "generation_start":
                            row["session_id"] = "other-session"
                        if case == "tool_substitution" and row.get("kind") == "generation_tool_result":
                            row["text"] += "substitution"
                            row["bytes"] = len(row["text"].encode())
                            row["sha256"] = sha256(row["text"].encode())
                    path.write_bytes(b"\n".join(json_bytes(r) for r in rows) + b"\n")
                elif case == "freeze_change":
                    with (directory / "run/private/gold.json").open("ab") as f:
                        f.write(b" ")
                elif case == "duplicate":
                    origin = json.loads((stage / "provider-identity.json").read_bytes())["base_url"]
                    with httpx.Client(trust_env=False, timeout=5) as client:
                        state["duplicate_status"] = client.post(origin+"/v1/chat/completions",content=raw).status_code
            calls, text = [], None
            if "Acknowledgement-State: required" in strings:
                calls = [("governance_ack", {
                    "policyId": "ephy.system-development-governance.v1",
                    "policySha256": re.search(r"Policy-SHA256: ([a-f0-9]{64})", strings).group(1),
                    "endMarker": "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1",
                    "nonce": nonce, "role": role})]
            elif not any(m.get("role") == "tool" and (m.get("content") == TASK
                         or isinstance(m.get("content"), list) and "".join(v.get("text", "") for v in m["content"]) == TASK)
                         for m in payload["messages"]):
                calls = [("read", {"path": "authoring/task.md"})]
                if role == "implementer":
                    calls.append(("read", {"path": "authoring/plan.md"}))
            elif role == "planner":
                text = "Use measured component evidence, preserve STOP on unverified evidence, cite both records."
            elif not (stage / "input" / SKILL_PATH).exists():
                calls = [("write", {"path": SKILL_PATH, "content": development_skill().decode()})]
            else:
                text = "DONE"
            delta = {"role": "assistant"}
            if calls:
                delta["tool_calls"] = [{"index": i, "id": f"generation-{state['posts']}-{i}", "type": "function",
                                      "function": {"name": n, "arguments": json.dumps(a)}}
                                     for i, (n, a) in enumerate(calls)]
            else:
                delta["content"] = text
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 4000, "completion_tokens": 100, "total_tokens": 4100}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                chunk.update(id="chatcmpl-generation-control", object="chat.completion.chunk", created=1, model=FAKE_MODEL)
                self.wfile.write(("data: "+json.dumps(chunk)+"\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    identity = {"schema": "ephy.existing-strata.v1", "base_url": f"http://127.0.0.1:{server.server_port}",
                "model_id": FAKE_MODEL, "context": 131072, "listener": process_identity(os.getpid()),
                "engine": process_identity(os.getpid()), "configuration": {
                    "path": str(directory / "deployment.json"),
                    "sha256": sha256((directory / "deployment.json").read_bytes())}}
    result, error, verifier = None, None, None
    try:
        freeze = freeze_generation(ROOT, pi, identity, directory/"batch.json", directory/"gold.json",
                                   directory/"run", [sys.executable, str(counter)])
        digest = sha256(freeze.read_bytes())
        try:
            result = run_native_generation(freeze, digest)
            env = {**os.environ, "PYTHONPATH": str(ROOT/"src"), "PYTHONIOENCODING": "utf-8"}
            import subprocess
            checked = subprocess.run([sys.executable, "-X", "utf8", "-m", "ephy_worker.verifier_triage_isolation",
                                      "--freeze", str(freeze), "--sha256", digest],
                                     env=env, capture_output=True, timeout=20, check=False)
            verifier = {"exit_code": checked.returncode, "stdout": checked.stdout.decode(),
                        "stderr": checked.stderr.decode()}
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            error = type(exc).__name__+": "+str(exc)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    passed = ((error is None and result is not None and state["posts"] == 7 and len(state["nonces"]) == 2
               and verifier["exit_code"] == 0) if case == "good"
              else error is not None and REASONS[case] in error and state["posts"] == 2)
    if case == "duplicate":
        passed = passed and state["duplicate_status"] == 409
    summary = {"case": case, "passed": passed, "fake_posts": state["posts"],
               "fresh_sessions": len(state["nonces"]), "error": error, "result": result,
               "independent_verifier": verifier, "real_model_generations": 0}
    (directory/"summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--case", choices=(*CASES, *ADAPTER_CASES, "all", "adapter_all"), default="all")
    args = parser.parse_args()
    if args.artifacts.exists():
        parser.error("Fresh artifacts required")
    pi = args.pi.resolve(strict=True)
    if args.case in {"all", "adapter_all"}:
        cases, runner = (CASES, run_case) if args.case == "all" else (ADAPTER_CASES, run_adapter_case)
        results = [runner(pi, args.artifacts/case, case) for case in cases]
        result = {"passed": all(r["passed"] for r in results), "cases": results, "real_model_generations": 0}
        (args.artifacts/"summary.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    else:
        result = (run_adapter_case if args.case in ADAPTER_CASES else run_case)(pi, args.artifacts, args.case)
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
