"""Real Pi and owned scripted provider controls for closed generation capture."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from ephy_worker.strata_runtime import process_identity
from ephy_worker.verifier_triage_consumer import json_bytes, payload_strings
from ephy_worker.verifier_triage_evaluation import SKILL_PATH, sha256
from ephy_worker.verifier_triage_fixtures import development_fixture, development_skill
from ephy_worker.verifier_triage_isolation import FAKE_MODEL, TASK, freeze_generation, run_native_generation

ROOT = Path(__file__).resolve().parents[1]
CASES = ("good", "missing", "batch_leak", "gold_leak", "duplicate", "session", "hash", "tool_substitution",
         "freeze_change")
REASONS = {"missing": "FileNotFoundError", "batch_leak": "Held-out/gold leakage",
           "gold_leak": "Held-out/gold leakage", "duplicate": "already failed",
           "session": "Wrong generation session", "hash": "Earlier captured file changed",
           "tool_substitution": "Tool-result bytes substituted", "freeze_change": "Frozen artifact changed"}


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
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    args = parser.parse_args()
    if args.artifacts.exists():
        parser.error("Fresh artifacts required")
    pi = args.pi.resolve(strict=True)
    if args.case == "all":
        results = [run_case(pi, args.artifacts/case, case) for case in CASES]
        result = {"passed": all(r["passed"] for r in results), "cases": results, "real_model_generations": 0}
        (args.artifacts/"summary.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    else:
        result = run_case(pi, args.artifacts, args.case)
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
