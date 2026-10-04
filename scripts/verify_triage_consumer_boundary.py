"""Existing Pi + scripted HTTP controls; no real model or held-out data."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ephy_worker.strata_runtime import process_identity
from ephy_worker.verifier_triage_consumer import (
    build_contract,
    json_bytes,
    payload_strings,
    run_contract,
    run_session,
)
from ephy_worker.verifier_triage_evaluation import BATCH_PATH, SKILL_PATH, sha256
from ephy_worker.verifier_triage_fixtures import development_fixture, development_skill

MODEL = "synthetic-triage-consumer-fixture"
ROOT = Path(__file__).resolve().parents[1]
CASES = ("good", "forbidden_tool", "request9", "session5", "truncated",
         "baseline_leak", "partial_skill", "false_hash", "context_oversize", "wrong_counter",
         "crlf_batch", "bom_skill", "invalid_utf8_skill")
EXPECTED = {
    "forbidden_tool": ("Consumer exited 78", 2, 1),
    "request9": ("Consumer exited 78", 8, 1),
    "session5": ("Session5 refused", 0, 0),
    "truncated": ("Consumer exited 78", 3, 1),
    "baseline_leak": ("Consumer exited", 2, 1),
    "partial_skill": ("Consumer exited 78", 8, 3),
    "false_hash": ("False skill identity", 9, 3),
    "context_oversize": ("Consumer exited", 0, 0),
    "wrong_counter": ("Consumer exited", 0, 0),
    "crlf_batch": ("canonical UTF-8/LF", 0, 0),
    "bom_skill": ("canonical UTF-8/LF", 0, 0),
    "invalid_utf8_skill": ("not valid UTF-8", 0, 0),
}


def run_case(pi: Path, directory: Path, case: str) -> dict:
    if case not in CASES or directory.exists():
        raise ValueError("Known case and fresh artifact path required")
    directory.mkdir(parents=True)
    batch, gold = development_fixture()
    expected = json.loads(gold)
    skill = development_skill()
    if case == "crlf_batch":
        batch += b"\r\n"
    elif case == "bom_skill":
        skill = b"\xef\xbb\xbf" + skill
    elif case == "invalid_utf8_skill":
        skill = b"\xff" + skill
    if case == "baseline_leak":
        exposed = json.loads(batch)
        exposed[0]["input"] += "\n" + skill.decode()
        batch = json_bytes(exposed)
    for name, body in (("batch.json", batch), ("gold.json", gold), ("skill.md", skill),
                       ("fake-deployment.json", b'{"purpose":"synthetic-only"}')):
        (directory / name).write_bytes(body)
    counter = directory / "synthetic_counter.py"
    count = 130000 if case == "context_oversize" else 4000
    digest = "'0'*64" if case == "wrong_counter" else "hashlib.sha256(raw).hexdigest()"
    counter.write_text(
        "import hashlib,json,sys\nraw=sys.stdin.buffer.read()\n"
        f"print(json.dumps({{'payload_sha256':{digest},"
        f"'model_id':{MODEL!r},'input_tokens':{count},'synthetic_counter':True}}))\n",
        encoding="utf-8",
    )
    state = {"posts": [], "sessions": set()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            payload = (
                {"object": "list", "data": [{"id": MODEL, "status": {"value": "loaded"},
                                           "meta": {"n_ctx": 131072}}]}
                if self.path == "/v1/models" else
                {"service": "strata", "loaded": True, "api_key": False,
                 "model": MODEL, "max_context": 131072}
            )
            data = json_bytes(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            strings = "\n".join(payload_strings(payload))
            nonce = re.search(r"Acknowledgement-Nonce: ([^\s]+)", strings).group(1)
            state["sessions"].add(nonce)
            state["posts"].append({"sha256": sha256(raw), "max_tokens": payload.get("max_tokens"),
                                   "alias": payload.get("max_completion_tokens")})
            treatment = "Treatment:" in strings
            calls, text = [], None
            if "Acknowledgement-State: required" in strings:
                arguments = {
                    "policyId": "ephy.system-development-governance.v1",
                    "policySha256": re.search(r"Policy-SHA256: ([a-f0-9]{64})", strings).group(1),
                    "endMarker": "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1",
                    "nonce": nonce, "role": "planner",
                }
                calls = [("governance_ack", arguments)]
            elif case == "forbidden_tool":
                calls = [("write", {"path": "escape.txt", "content": "must not be written"})]
            elif expected[0]["id"] not in strings or case == "request9":
                calls = [("read", {"path": BATCH_PATH})]
                if treatment:
                    args = {"path": SKILL_PATH}
                    if case == "partial_skill":
                        args["limit"] = 1
                    calls.append(("read", args))
            else:
                answers = [dict(row) for row in expected]
                if not treatment:
                    for index in (0, 2, 4):
                        answers[index]["diagnosis"] = "UNATTRIBUTED"
                text = json.dumps({
                    "skill_sha256": (
                        "0" * 64 if case == "false_hash" else sha256(skill)
                    ) if treatment else None, "answers": answers,
                }, separators=(",", ":"))
                if case == "truncated":
                    text = text[:-2]
            delta = {"role": "assistant"}
            if text is not None:
                delta["content"] = text
            if calls:
                delta["tool_calls"] = [
                    {"index": index, "id": f"call-{len(state['posts'])}-{index}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(arguments)}}
                    for index, (name, arguments) in enumerate(calls)
                ]
            finish = "tool_calls" if calls else ("length" if case == "truncated" else "stop")
            chunks = [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                {"choices": [], "usage": {"prompt_tokens": 4000, "completion_tokens": 100,
                                         "total_tokens": 4100}},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                chunk.update(id="chatcmpl-synthetic", object="chat.completion.chunk", created=1, model=MODEL)
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    identity = {
        "schema": "ephy.existing-strata.v1", "base_url": f"http://127.0.0.1:{server.server_port}",
        "model_id": MODEL, "context": 131072, "listener": process_identity(os.getpid()),
        "engine": process_identity(os.getpid()), "configuration": {
            "path": str(directory / "fake-deployment.json"),
            "sha256": sha256((directory / "fake-deployment.json").read_bytes()),
        },
    }
    result, error = None, None
    try:
        try:
            contract = build_contract(
                ROOT, pi, identity, directory / "batch.json", directory / "gold.json",
                directory / "skill.md", directory / "run", [sys.executable, str(counter)],
                purpose="native_controls",
            )
            if case == "session5":
                run_session(json.loads(contract.read_bytes()), 4)
            else:
                result = run_contract(contract, sha256(contract.read_bytes()))
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as problem:
            error = type(problem).__name__ + ": " + str(problem)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    traces = [
        json.loads(line) for p in (directory / "run").glob("consumer-*/trace.jsonl")
        for line in p.read_bytes().splitlines() if line.strip()
    ]
    violations = [e.get("reason") for e in traces if e.get("kind") == "violation"]
    wire = [json.loads(p.read_bytes()) for p in (directory / "run").glob("consumer-*/http-*.receipt.json")]
    full_reads = [e for e in traces if e.get("kind") == "evidence_read"
                  and e.get("full_content_delivered") is True]
    full_batch = [e for e in full_reads if e.get("path") == BATCH_PATH and e.get("sha256") == sha256(batch)]
    full_skill = [e for e in full_reads if e.get("path") == SKILL_PATH and e.get("sha256") == sha256(skill)]
    if case == "good":
        receipts = [json.loads(p.read_bytes()) for p in (directory / "run").glob("consumer-*/receipt.json")]
        passed = (
            error is None and result["outcome"] == "descriptive_improvement"
            and result["strict_pass"] and result["paired_diagnosis_improvements"] >= 3
            and len(state["posts"]) == 12 and len(state["sessions"]) == 4
            and len(full_batch) == 4 and len(full_skill) == 2
            and len(wire) == 12 and all(r.get("forwarded") and not r["baseline_skill_leak"] for r in wire)
            and len(receipts) == 4 and all(r["exit_code"] == 0 for r in receipts)
        )
    else:
        reason, posts, sessions = EXPECTED[case]
        passed = error is not None and reason in error and len(state["posts"]) == posts \
            and len(state["sessions"]) == sessions
        if case == "forbidden_tool":
            passed = passed and bool(violations) and not list((directory / "run").rglob("escape.txt"))
        elif case == "request9":
            passed = passed and any("request" in v.lower() for v in violations)
        elif case == "truncated":
            passed = passed and bool(violations) and any(
                b'"finish_reason": "length"' in p.read_bytes()
                for p in (directory / "run").glob("consumer-*/http-*.response")
            )
        elif case in {"baseline_leak", "context_oversize", "wrong_counter"}:
            needle = {"baseline_leak": "Baseline contains candidate",
                      "context_oversize": "exceeds context",
                      "wrong_counter": "Tokenizer measured different"}[case]
            passed = passed and any(needle in r.get("error", "") for r in wire)
    summary = {
        "case": case, "passed": bool(passed), "result": result, "error": error,
        "raw_generation_posts": len(state["posts"]), "fresh_sessions": len(state["sessions"]),
        "cap_fields": state["posts"], "violations": violations,
        "full_byte_batch_reads": len(full_batch), "full_byte_skill_reads": len(full_skill),
        "synthetic_only": True, "real_model_contacted": False,
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    args = parser.parse_args()
    if args.artifacts.exists():
        parser.error("Fresh artifact path required")
    pi = args.pi.resolve(strict=True)
    if args.case == "all":
        results = [run_case(pi, args.artifacts / case, case) for case in CASES]
        result = {"passed": all(r["passed"] for r in results), "cases": results,
                  "real_model_generation_requests": 0}
        (args.artifacts / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    else:
        result = run_case(pi, args.artifacts, args.case)
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
