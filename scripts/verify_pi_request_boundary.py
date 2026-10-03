"""Real Pi + synthetic loopback HTTP: no model, global settings, or real service.

Supply an existing Pi binary and a fresh private artifact directory. This is a
transport control, not model/workflow evidence. The server counts raw HTTP POSTs,
including retries and in-flight calls; the managed hook counts admitted calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
MODEL = "synthetic-boundary-fixture"


def run_case(pi: Path, directory: Path, case: str, limit: int) -> dict:
    directory.mkdir()
    (directory / "docs").mkdir()
    (directory / "docs/readable.md").write_text("Synthetic fixture only.\n", encoding="utf-8")
    agent = directory / "agent"
    agent.mkdir()
    (agent / "settings.json").write_text(
        json.dumps({"retry": {"enabled": False}, "compaction": {"enabled": False}}), encoding="utf-8"
    )
    state = {"posts": [], "active": 0, "maximum_active": 0, "delayed_in_flight": False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = json.dumps(
                {
                    "service": "strata",
                    "api_key": False,
                    "loaded": case != "identity",
                    "model": MODEL,
                    "max_context": 131072,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["posts"].append(
                    {
                        "model": payload.get("model"),
                        "max_tokens": payload.get("max_tokens"),
                        "max_completion_tokens": payload.get("max_completion_tokens"),
                    }
                )
                number = len(state["posts"])
                state["active"] += 1
                state["maximum_active"] = max(state["maximum_active"], state["active"])
            try:
                if case == "http_error":
                    body = b'{"error":{"message":"Synthetic service error","type":"server_error"}}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if case == "in_flight":
                    time.sleep(0.4)
                    with lock:
                        state["delayed_in_flight"] = state["active"] == 1 and len(state["posts"]) == 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                path = "../outside.md" if case == "scope" else "docs/readable.md"
                chunks = [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": f"call-{number}",
                                            "type": "function",
                                            "function": {
                                                "name": "read",
                                                "arguments": json.dumps({"path": path}),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 2
                            if case == "tokens"
                            else 7
                            if case == "response_cap"
                            else (5 if number == 1 else 2)
                            if case == "remaining"
                            else 1,
                            "total_tokens": 3
                            if case == "tokens"
                            else 8
                            if case == "response_cap"
                            else (6 if number == 1 else 3)
                            if case == "remaining"
                            else 2,
                        },
                    },
                ]
                for chunk in chunks:
                    chunk.update(
                        id=f"chatcmpl-fixture-{number}",
                        object="chat.completion.chunk",
                        created=1,
                        model=MODEL,
                    )
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            finally:
                with lock:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    identity = directory / "identity.json"
    identity.write_text(
        json.dumps({"base_url": origin, "model_id": MODEL, "context": 131072}), encoding="utf-8"
    )
    config = directory / "config.json"
    config.write_text(
        json.dumps(
            {
                "root": str(directory),
                "role": "planner",
                "model_id": "wrong-expected-model" if case == "model" else MODEL,
                "provider_id": "strata-local",
                "allowed_files": [],
                "max_requests": limit,
                "output_token_budget": 2 if case == "tokens" else 7 if case == "remaining" else 200,
                "max_response_tokens": 6,
            }
        ),
        encoding="utf-8",
    )
    trace = directory / ("missing/trace.jsonl" if case == "trace_io" else "trace.jsonl")
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "HOME",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "APPDATA",
            "LOCALAPPDATA",
            "LANG",
            "LC_ALL",
            "TZ",
        )
        if key in os.environ
    }
    (directory / "tmp").mkdir()
    environment.update(TEMP=str(directory / "tmp"), TMP=str(directory / "tmp"), TMPDIR=str(directory / "tmp"))
    environment.update(
        PI_CODING_AGENT_DIR=str(agent),
        PI_OFFLINE="1",
        EPHY_STRATA_IDENTITY=str(identity),
        EPHY_FORMAL_STAGE_CONFIG=str(config),
        EPHY_FORMAL_TRACE=str(trace),
    )
    argv = [
        str(pi),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--offline",
        "--provider",
        "strata-local",
        "--model",
        MODEL,
        "--thinking",
        "off",
        "--extension",
        str(REPOSITORY / "tools/pi-local/strata-provider.ts"),
        "--extension",
        str(REPOSITORY / "tools/pi-local/formal-stage-guard.ts"),
        "--session",
        str(directory / "session.jsonl"),
        "--mode",
        "json",
        "--print",
        "--approve",
        "--",
        "Read docs/readable.md repeatedly. This is a synthetic transport fixture; no other tools.",
    ]
    if case == "later_hook":
        later = directory / "later-payload-hook.ts"
        later.write_text(
            'export default function(pi) { pi.on("before_provider_request", '
            "(event) => ({ ...event.payload, fixture_tail: true })); }",
            encoding="utf-8",
        )
        argv[argv.index("--session") : argv.index("--session")] = ["--extension", str(later)]
    started = time.monotonic()
    try:
        with (directory / "stdout.jsonl").open("wb") as out, (directory / "stderr.log").open("wb") as err:
            process = subprocess.Popen(
                argv,
                cwd=directory,
                env=environment,
                stdout=out,
                stderr=err,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            try:
                code = process.wait(timeout=20)
            finally:
                if process.poll() is None:
                    # The exact Popen object is owned by this test; no process-name kill.
                    process.kill()
                    process.wait(timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    events = (
        [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        if trace.exists()
        else []
    )
    expected = (
        0
        if case in ("identity", "model", "trace_io")
        else 1
        if case in ("tokens", "scope", "http_error", "in_flight", "response_cap", "later_hook")
        else 2
        if case == "remaining"
        else limit
    )
    admitted = [event for event in events if event["kind"] == "provider_request"]
    hard_stops = [event for event in events if event["kind"] == "hard_stop"]
    passed = (
        len(state["posts"]) == expected
        and len(admitted) == expected
        and state["maximum_active"] <= 1
        and all(
            p["max_tokens"] == (2 if case == "tokens" or (case == "remaining" and i == 1) else 6)
            and p["max_completion_tokens"] is None
            for i, p in enumerate(state["posts"])
        )
    )
    if case == "trace_io":
        passed = passed and code == 78 and not trace.exists()
    elif case == "scope":
        # Pi honors tool_call termination in this pinned version; the controller
        # must still reject its exit-zero stage using the latched trace evidence.
        passed = passed and any(e["kind"] == "violation" for e in events)
        passed = passed and any(e["kind"] == "stage_end" and e["failed"] for e in events)
    else:
        passed = passed and code == 78 and len(hard_stops) == 1
    if case == "in_flight":
        passed = passed and state["delayed_in_flight"]
    result = {
        "case": case,
        "limit": limit,
        "expected_posts": expected,
        "actual_posts": len(state["posts"]),
        "admitted": len(admitted),
        "maximum_in_flight": state["maximum_active"],
        "exit_code": code,
        "hard_stop_count": len(hard_stops),
        "elapsed_seconds": time.monotonic() - started,
        "passed": passed,
        "real_model_contacted": False,
        "captured_cap_fields": state["posts"],
    }
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True)  # fresh evidence allocation, never overwrite
    pi = args.pi.resolve(strict=True)
    results = []
    for case, limit in (
        ("requests-1", 1),
        ("requests-2", 2),
        ("requests-8", 8),
        ("tokens", 8),
        ("in_flight", 1),
        ("scope", 8),
        ("identity", 8),
        ("model", 8),
        ("http_error", 1),
        ("trace_io", 1),
        ("response_cap", 8),
        ("remaining", 8),
        ("later_hook", 1),
    ):
        result = run_case(pi, args.artifacts / case, case, limit)
        results.append(result)
        print(json.dumps(result), flush=True)
        if not result["passed"]:
            break
    files = [
        pi,
        REPOSITORY / "tools/pi-local/formal-stage-guard.ts",
        REPOSITORY / "tools/pi-local/formal-stage-stop.ts",
        REPOSITORY / "tools/pi-local/strata-provider.ts",
    ]
    summary = {
        "passed": len(results) == 13 and all(r["passed"] for r in results),
        "results": results,
        "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
        "synthetic_fixture_only": True,
        "real_model_contacted": False,
    }
    (args.artifacts / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
