"""Actual HTTP boundary controls; model and native Pi are never used in CI."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from ephy_worker import verifier_triage_consumer as consumer
from ephy_worker.verifier_triage_evaluation import InvalidEvaluation, sha256
from ephy_worker.verifier_triage_fixtures import development_fixture, development_skill

ROOT = Path(__file__).resolve().parents[1]
MODEL = "synthetic-triage-consumer-fixture"


@pytest.fixture
def contract(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "verify_identity", lambda _: None)
    monkeypatch.setattr(consumer, "resource_gate", lambda *_: None)
    batch, gold = development_fixture()
    for name, data in (("batch", batch), ("gold", gold), ("skill", development_skill())):
        (tmp_path / name).write_bytes(data)
    counter = tmp_path / "counter.py"
    counter.write_text(
        "import hashlib,json,sys\nb=sys.stdin.buffer.read()\n"
        f"print(json.dumps({{'payload_sha256':hashlib.sha256(b).hexdigest(),"
        f"'model_id':{MODEL!r},'input_tokens':4000,'synthetic_counter':True}}))\n",
        encoding="utf-8",
    )
    path = consumer.build_contract(
        ROOT, Path(sys.executable),
        {"model_id": MODEL, "context": 131072, "base_url": "http://127.0.0.1:1",
         "listener": {"pid": os.getpid()}, "engine": {"pid": os.getpid()}},
        tmp_path / "batch", tmp_path / "gold", tmp_path / "skill",
        tmp_path / "run", [sys.executable, str(counter)], purpose="native_controls",
    )
    return json.loads(path.read_bytes())


@pytest.fixture
def upstream():
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            posts.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", posts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def payload():
    return {"model": MODEL, "max_tokens": 1024, "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "public development probe"}],
            "tools": [{"type": "function", "function": {"name": "governance_ack"}}],
            "tool_choice": {"type": "function", "function": {"name": "governance_ack"}},
            "stream": True}


def save_preview(directory, body, number):
    raw = consumer.json_bytes(body)
    consumer.write_new(directory / f"previews/{number}.json", raw)
    with (directory / "trace.jsonl").open("ab") as out:
        out.write(consumer.json_bytes({"kind": "triage_preview", "number": number,
                                      "sha256": sha256(raw)}) + b"\n")


def gateway(contract, tmp_path, upstream, arm="baseline"):
    contract["identity"]["base_url"] = upstream[0]
    directory = tmp_path / "gateway"
    (directory / "previews").mkdir(parents=True)
    save_preview(directory, payload(), 1)
    return consumer.Gateway(contract, directory, arm)


def test_actual_body_is_counted_saved_and_forwarded_without_rewriting(contract, tmp_path, upstream):
    gate = gateway(contract, tmp_path, upstream)
    raw = consumer.json_bytes(payload())
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", content=raw).status_code == 200
    assert upstream[1] == [raw]
    record = gate.records[0]
    assert record["forwarded"] and record["accepted"]
    assert record["actual_payload_fit"]["payload_sha256"] == sha256(raw)
    assert not record["baseline_skill_leak"]
    assert (tmp_path / "gateway/http-1.json").read_bytes() == raw


def test_ninth_post_is_refused_and_failure_latches(contract, tmp_path, upstream):
    gate = gateway(contract, tmp_path, upstream)
    with gate as origin, httpx.Client(trust_env=False) as client:
        for number in range(1, 9):
            if number > 1:
                save_preview(gate.directory, payload(), number)
            assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 200
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 503
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 503
    assert len(upstream[1]) == 8
    assert "N+1" in gate.records[8]["error"]
    assert not gate.records[8]["forwarded"]
    assert "already failed" in gate.records[9]["error"]


def test_candidate_body_in_baseline_payload_is_refused_before_upstream(contract, tmp_path, upstream):
    gate = gateway(contract, tmp_path, upstream)
    body = payload()
    body["messages"][0]["content"] = development_skill().decode().replace("\n", "\r\n")
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=body).status_code == 503
    assert not upstream[1]
    assert gate.records[0]["baseline_skill_leak"]
    assert "Baseline contains candidate" in gate.failure


@pytest.mark.parametrize("change", [
    {"model": "other"}, {"max_tokens": 1025}, {"max_tokens": True},
    {"max_completion_tokens": 1}, {"strata_mcp": True}, {"response_format": {"type": "json_object"}},
])
def test_malformed_invocations_never_reach_provider(contract, tmp_path, upstream, change):
    gate = gateway(contract, tmp_path, upstream)
    body = payload()
    body.update(change)
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=body).status_code == 503
    assert not upstream[1]
    assert not gate.records[0]["accepted"]


def test_session5_is_rejected_before_process_or_http(contract, monkeypatch):
    monkeypatch.setattr(consumer.subprocess, "Popen", lambda *a, **k: pytest.fail("Pi launched"))
    with pytest.raises(InvalidEvaluation, match="Session5"):
        consumer.run_session(contract, 4)
    assert not list(Path(contract["directory"]).glob("consumer-*"))


@pytest.mark.parametrize("key,value", [
    ("max_requests", 9), ("stage_seconds", 301), ("max_log_bytes", 8388609),
    ("max_process_rss_bytes", 4294967297), ("minimum_free_ram_bytes", 1),
    ("minimum_free_disk_bytes", 1),
])
def test_loosened_contract_rejected_before_launch(contract, key, value):
    contract[key] = value
    with pytest.raises(InvalidEvaluation):
        consumer.validate_contract(contract)


def test_frozen_source_change_rejected_before_launch(contract):
    source = Path(contract["private_root"]) / "skill.md"
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(InvalidEvaluation, match="changed"):
        consumer.validate_contract(contract)


def test_budget_keeps_observations_and_requests_separate():
    budgets = consumer.budget_reservations()
    assert budgets["consumers"]["max_requests"] == 32
    assert budgets["consumers"]["case_observations"] == 48
    assert budgets["generation"]["max_requests"] == 16
    assert budgets["generation"]["attempts"] == 1 and budgets["generation"]["repairs"] == 0
    assert budgets["preflight"]["model_requests"] == 8


def test_counter_failure_and_context_overflow_are_not_fit(contract, tmp_path, monkeypatch):
    class Result:
        returncode = 0
        stderr = b""
        stdout = b""
    result = Result()
    monkeypatch.setattr(consumer.subprocess, "run", lambda *a, **k: result)
    raw = consumer.json_bytes(payload())
    for index, measurement in enumerate((
        {"payload_sha256": "0" * 64, "model_id": MODEL, "input_tokens": 1},
        {"payload_sha256": sha256(raw), "model_id": MODEL, "input_tokens": 130000},
    )):
        result.stdout = consumer.json_bytes(measurement)
        with pytest.raises(InvalidEvaluation):
            consumer.count_payload(contract, raw, tmp_path, f"failed-{index}")


def test_model_input_snapshot_contains_no_gold_and_disallows_hardlinks(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "batch.json").write_bytes(b"[]")
    assert list(consumer.snapshot(input_root)) == ["batch.json"]
    os.link(input_root / "batch.json", input_root / "alias.json")
    with pytest.raises(InvalidEvaluation, match="Hard-linked"):
        consumer.snapshot(input_root)


def test_closed_or_expired_gateway_never_forwards(contract, tmp_path, upstream):
    gate = gateway(contract, tmp_path, upstream)
    gate.deadline = time.monotonic() - 1
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 503
    assert not upstream[1]
    assert "deadline/closure" in gate.failure


def test_final_pin_recheck_blocks_result_publication(contract, monkeypatch):
    monkeypatch.setattr(consumer, "run_session", lambda *_: object())
    def changed_during_scoring(*_):
        skill = Path(contract["private_root"]) / "skill.md"
        skill.write_bytes(skill.read_bytes() + b"changed")
        return {"outcome": "descriptive_improvement"}
    monkeypatch.setattr(consumer, "evaluate", changed_during_scoring)
    path = Path(contract["directory"]) / "contract.json"
    with pytest.raises(InvalidEvaluation, match="changed"):
        consumer.run_contract(path, sha256(path.read_bytes()))
    assert not (Path(contract["directory"]) / "result.json").exists()


@pytest.mark.parametrize("body", [b"file\r\n", b"\xef\xbb\xbffile\n", b"\xfffile\n", b""])
def test_crlf_bom_invalid_utf8_are_rejected_without_normalization(body):
    with pytest.raises(InvalidEvaluation):
        consumer.canonical_consumer_input(body)
    consumer.canonical_consumer_input(b"valid UTF-8\n")


@pytest.mark.parametrize("key,cap", [("max_requests", 8), ("output_token_budget", 2200),
                                    ("max_response_tokens", 1024), ("stage_seconds", 300),
                                    ("timeout_seconds", 900)])
def test_oversize_generation_proposal_cannot_use_smaller_reservation(key, cap):
    from ephy_worker.verifier_triage_evaluation import BASE_REVISION, SKILL_PATH
    job = {"baseRevision": BASE_REVISION, "contract": {
        "allowed_files": [SKILL_PATH], "max_repairs": 0, "max_requests": 8,
        "output_token_budget": 2200, "max_response_tokens": 1024,
        "stage_seconds": 300, "timeout_seconds": 900,
    }}
    consumer.validate_generation_budget(job)
    job["contract"][key] = cap + 1
    with pytest.raises(InvalidEvaluation, match="reservation exceeded"):
        consumer.validate_generation_budget(job)


def test_live_contract_cannot_launch_without_pre_authoring_isolation_capture(contract, monkeypatch):
    contract["purpose"] = "fixed_trial"
    monkeypatch.setattr(consumer.subprocess, "Popen", lambda *a, **k: pytest.fail("Pi launched"))
    monkeypatch.setattr(consumer, "verify_external_proposal", lambda *_: pytest.fail("Incomplete proof used"))
    with pytest.raises(InvalidEvaluation, match="pre-authoring held-out/gold freeze"):
        consumer.validate_contract(contract)


@pytest.mark.parametrize("delivery", ["exact", "crlf_to_lf", "omitted"])
def test_actual_provider_payload_proves_exact_read_bytes(tmp_path, delivery):
    raw = b"first\r\nsecond\r\n" if delivery == "crlf_to_lf" else b"first\nsecond\n"
    event = {"kind": "triage_exact_read", "toolCallId": "read-1", "path": "inputs/triage-batch.json",
             "raw_sha256": sha256(raw), "delivered_sha256": sha256(raw), "raw_bytes": len(raw),
             "full_content_delivered": True}
    messages = [] if delivery == "omitted" else [{"role": "tool", "tool_call_id": "read-1",
                                                   "content": raw.decode().replace("\r\n", "\n")}]
    body = consumer.json_bytes({"messages": messages})
    (tmp_path / "http-1.json").write_bytes(body)
    records = [{"payload_sha256": sha256(body)}]
    if delivery == "exact":
        assert consumer.wire_read_delivery(tmp_path, records, [event])[0]["sha256"] == sha256(raw)
    else:
        with pytest.raises(InvalidEvaluation):
            consumer.wire_read_delivery(tmp_path, records, [event])


@pytest.mark.parametrize("change", ["required", "extra_property", "strict", "strict_integer", "message",
                                   "response_cap", "preview_rewrite", "missing_preview"])
def test_serialized_wire_must_match_hash_bound_preview_before_forwarding(contract, tmp_path, upstream, change):
    gate = gateway(contract, tmp_path, upstream)
    preview = payload()
    preview["tools"] = [{"type": "function", "function": {"name": "read", "strict": True,
        "parameters": {"type": "object", "required": ["path"], "additionalProperties": False,
                       "properties": {"path": {"type": "string", "enum": [consumer.BATCH_PATH]}}}}}]
    preview.pop("tool_choice")
    body = json.loads(consumer.json_bytes(preview))
    if change == "required":
        body["tools"][0]["function"]["parameters"]["required"].append("offset")
    elif change == "extra_property":
        body["tools"][0]["function"]["parameters"]["properties"]["offset"] = {"type": "integer"}
    elif change == "strict":
        body["tools"][0]["function"]["strict"] = False
    elif change == "strict_integer":
        body["tools"][0]["function"]["strict"] = 1
    elif change == "message":
        body["messages"].append({"role": "user", "content": "unbound addition"})
    elif change == "response_cap":
        body["max_tokens"] = 512
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 200
        save_preview(gate.directory, preview, 2)
        if change == "preview_rewrite":
            preview["tools"][0]["function"]["strict"] = False
            body = preview
            (gate.directory / "previews/2.json").write_bytes(consumer.json_bytes(preview))
        elif change == "missing_preview":
            (gate.directory / "previews/2.json").unlink()
        assert client.post(origin + "/v1/chat/completions", content=consumer.json_bytes(body)).status_code == 503
    assert len(upstream[1]) == 1
    assert not gate.records[1]["accepted"] and not gate.records[1]["forwarded"]


def test_only_exact_first_governance_transform_is_allowed(contract, tmp_path, upstream):
    gate = gateway(contract, tmp_path, upstream)
    preview = payload()
    preview["tools"].append({"type": "function", "function": {"name": "read"}})
    (gate.directory / "previews/1.json").write_bytes(consumer.json_bytes(preview))
    (gate.directory / "trace.jsonl").write_bytes(consumer.json_bytes({
        "kind": "triage_preview", "number": 1, "sha256": sha256(consumer.json_bytes(preview))}) + b"\n")
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 200
        save_preview(gate.directory, preview, 2)
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 503
    assert len(upstream[1]) == 1


@pytest.mark.parametrize("extra", [{"type": "image", "data": "AA==", "mimeType": "image/png"},
                                  {"type": "input_audio", "data": "AA=="},
                                  {"type": "resource", "uri": "fixture://unbound"},
                                  {"type": "text", "text": None}, None, "untyped text"])
def test_exact_text_plus_nontext_wire_block_is_rejected(tmp_path, extra):
    raw = b"complete file\n"
    event = {"kind": "triage_exact_read", "toolCallId": "read-1", "path": consumer.BATCH_PATH,
             "raw_sha256": sha256(raw), "delivered_sha256": sha256(raw), "raw_bytes": len(raw),
             "full_content_delivered": True}
    body = consumer.json_bytes({"messages": [{"role": "tool", "tool_call_id": "read-1",
        "content": [{"type": "text", "text": raw.decode()}, extra]}]})
    (tmp_path / "http-1.json").write_bytes(body)
    with pytest.raises(InvalidEvaluation, match="Nontext transmitted read"):
        consumer.wire_read_delivery(tmp_path, [{"payload_sha256": sha256(body)}], [event])


@pytest.mark.parametrize("content", ["complete file\n", [{"type": "text", "text": "complete "},
                                                        {"type": "text", "text": "file\n"}]])
def test_exact_string_or_all_text_blocks_are_accepted(tmp_path, content):
    raw = b"complete file\n"
    event = {"kind": "triage_exact_read", "toolCallId": "read-1", "path": consumer.BATCH_PATH,
             "raw_sha256": sha256(raw), "delivered_sha256": sha256(raw), "raw_bytes": len(raw),
             "full_content_delivered": True}
    body = consumer.json_bytes({"messages": [{"role": "tool", "tool_call_id": "read-1", "content": content}]})
    (tmp_path / "http-1.json").write_bytes(body)
    assert consumer.wire_read_delivery(tmp_path, [{"payload_sha256": sha256(body)}], [event])[0]["sha256"] == sha256(raw)


@pytest.mark.parametrize("change", ["extra_tool", "ack_schema", "message", "response_cap"])
def test_first_governance_exception_cannot_change_any_other_body_field(contract, tmp_path, upstream, change):
    gate = gateway(contract, tmp_path, upstream)
    body = payload()
    if change == "extra_tool":
        body["tools"].append({"type": "function", "function": {"name": "read"}})
    elif change == "ack_schema":
        body["tools"][0]["function"]["parameters"] = {"type": "object"}
    elif change == "message":
        body["messages"][0]["content"] = "unbound bootstrap text"
    else:
        body["max_tokens"] = 512
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=body).status_code == 503
    assert not upstream[1] and not gate.records[0]["accepted"] and not gate.records[0]["forwarded"]


@pytest.mark.parametrize("arm", ["baseline", "treatment"])
@pytest.mark.parametrize("shape", ["exact", "split_text", "mixed_image", "mixed_resource", "array_text",
                                  "null_block", "null_content", "nonarray_content", "string_content",
                                  "number_text", "extra_field"])
def test_production_read_callback_hard_stops_malformed_content(tmp_path, arm, shape):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for actual TypeScript guard callback controls")
    text = "complete file\n"
    content = {
        "exact": [{"type": "text", "text": text}],
        "split_text": [{"type": "text", "text": "complete "}, {"type": "text", "text": "file\n"}],
        "mixed_image": [{"type": "text", "text": text}, {"type": "image", "data": "AA==", "mimeType": "image/png"}],
        "mixed_resource": [{"type": "text", "text": text}, {"type": "resource", "uri": "fixture://unbound"}],
        "array_text": [{"type": "text", "text": [text]}],
        "null_block": [{"type": "text", "text": text}, None],
        "null_content": None,
        "nonarray_content": {"type": "text", "text": text},
        "string_content": text,
        "number_text": [{"type": "text", "text": 1}],
        "extra_field": [{"type": "text", "text": text, "unbound": "extra"}],
    }[shape]
    batch = tmp_path / consumer.BATCH_PATH
    batch.parent.mkdir()
    batch.write_bytes(text.encode())
    config = tmp_path / "config.json"
    config.write_bytes(consumer.json_bytes({"root": str(tmp_path), "arm": arm}))
    result = tmp_path / "result.json"
    result.write_bytes(consumer.json_bytes({"toolName": "read", "toolCallId": "read-1", "content": content}))
    script = tmp_path / "callback.mjs"
    script.write_text(
        "import {readFileSync} from 'node:fs';\n"
        "const {default:guard}=await import(process.argv[2]);const handlers=new Map();\n"
        "guard({on:(name,callback)=>handlers.set(name,callback)});\n"
        "handlers.get('tool_call')({toolName:'read',toolCallId:'read-1',input:{path:'inputs/triage-batch.json'}});\n"
        "handlers.get('tool_result')(JSON.parse(readFileSync(process.argv[3],'utf8')));\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.jsonl"
    completed = subprocess.run([node, "--experimental-strip-types", str(script),
                                (ROOT / "tools/pi-local/triage-consumer-guard.ts").as_uri(), str(result)],
                               env={**os.environ, "EPHY_TRIAGE_CONFIG": str(config), "EPHY_FORMAL_TRACE": str(trace)},
                               capture_output=True, timeout=10, check=False)
    assert completed.returncode == (0 if shape in {"exact", "split_text"} else 78), completed.stderr.decode(errors="replace")
    events = [json.loads(line) for line in trace.read_bytes().splitlines()]
    if shape in {"exact", "split_text"}:
        assert events[-1]["full_content_delivered"] is True
    else:
        assert events[-1]["kind"] == "hard_stop" and events[-1]["exit_code"] == 78
        assert not any(event.get("kind") == "triage_exact_read" for event in events)


def attested_read_payload(raw, content):
    body = payload()
    body.pop("tool_choice")
    body["tools"] = [{"type": "function", "function": {"name": "read", "strict": True,
        "parameters": {"type": "object", "required": ["path"], "additionalProperties": False,
                       "properties": {"path": {"type": "string", "enum": [consumer.BATCH_PATH]}}}}}]
    body["messages"] += [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "read-1", "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"path": consumer.BATCH_PATH})}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": content},
    ]
    event = {"kind": "triage_exact_read", "toolCallId": "read-1", "path": consumer.BATCH_PATH,
             "raw_sha256": sha256(raw), "delivered_sha256": sha256(raw), "raw_bytes": len(raw),
             "full_content_delivered": True}
    return body, event


@pytest.mark.parametrize("extra", [{"type": "image", "data": "AA==", "mimeType": "image/png"},
                                  {"type": "resource", "uri": "fixture://unbound"}])
def test_matching_preview_mixed_read_never_reaches_accepting_collector(contract, tmp_path, upstream, extra):
    gate = gateway(contract, tmp_path, upstream)
    raw = b"complete file\n"
    body, event = attested_read_payload(raw, [{"type": "text", "text": raw.decode()}, extra])
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 200
        with (gate.directory / "trace.jsonl").open("ab") as out:
            out.write(consumer.json_bytes(event) + b"\n")
        save_preview(gate.directory, body, 2)
        assert client.post(origin + "/v1/chat/completions", content=consumer.json_bytes(body)).status_code == 503
    assert len(upstream[1]) == 1
    assert not gate.records[1]["accepted"] and not gate.records[1]["forwarded"]


@pytest.mark.parametrize("shape", ["string", "text_blocks"])
@pytest.mark.parametrize("other_multimodal", [False, True])
def test_matching_preview_exact_read_forwards_without_scanning_other_content(
        contract, tmp_path, upstream, shape, other_multimodal):
    gate = gateway(contract, tmp_path, upstream)
    raw = b"complete file\n"
    content = raw.decode() if shape == "string" else [
        {"type": "text", "text": "complete "}, {"type": "text", "text": "file\n"}]
    body, event = attested_read_payload(raw, content)
    if other_multimodal:
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        body["messages"][0]["content"] = [image]
        body["messages"][1]["content"] = [image]
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 200
        with (gate.directory / "trace.jsonl").open("ab") as out:
            out.write(consumer.json_bytes(event) + b"\n")
        save_preview(gate.directory, body, 2)
        assert client.post(origin + "/v1/chat/completions", content=consumer.json_bytes(body)).status_code == 200
    assert len(upstream[1]) == 2 and upstream[1][-1] == consumer.json_bytes(body)
    assert gate.records[1]["consumer_preview_binding"]["read_results"][0]["sha256"] == sha256(raw)


@pytest.mark.parametrize("change", ["array_text", "null_block", "nonarray_content", "changed_text",
                                   "wrong_path", "missing_evidence", "missing_result", "orphan_result",
                                   "duplicate_result", "duplicate_call", "duplicate_evidence"])
def test_matching_preview_unbound_or_malformed_read_is_refused_before_forwarding(
        contract, tmp_path, upstream, change):
    gate = gateway(contract, tmp_path, upstream)
    raw = b"complete file\n"
    body, event = attested_read_payload(raw, raw.decode())
    if change == "array_text":
        body["messages"][-1]["content"] = [{"type": "text", "text": [raw.decode()]}]
    elif change == "null_block":
        body["messages"][-1]["content"] = [{"type": "text", "text": raw.decode()}, None]
    elif change == "nonarray_content":
        body["messages"][-1]["content"] = {"type": "text", "text": raw.decode()}
    elif change == "changed_text":
        body["messages"][-1]["content"] = raw.decode() + "unbound addition"
    elif change == "wrong_path":
        body["messages"][1]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": consumer.SKILL_PATH})
    elif change == "missing_result":
        body["messages"].pop()
    elif change == "orphan_result":
        body["messages"][-1]["tool_call_id"] = "unbound-call"
    elif change == "duplicate_result":
        body["messages"].append(dict(body["messages"][-1]))
    elif change == "duplicate_call":
        body["messages"][1]["tool_calls"].append(dict(body["messages"][1]["tool_calls"][0]))
    with gate as origin, httpx.Client(trust_env=False) as client:
        assert client.post(origin + "/v1/chat/completions", json=payload()).status_code == 200
        with (gate.directory / "trace.jsonl").open("ab") as out:
            if change != "missing_evidence":
                out.write(consumer.json_bytes(event) + b"\n")
            if change == "duplicate_evidence":
                out.write(consumer.json_bytes(event) + b"\n")
        save_preview(gate.directory, body, 2)
        assert client.post(origin + "/v1/chat/completions", content=consumer.json_bytes(body)).status_code == 503
    assert len(upstream[1]) == 1
    assert not gate.records[1]["accepted"] and not gate.records[1]["forwarded"]
