"""Actual HTTP boundary controls; model and native Pi are never used in CI."""
import json
import os
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
            "tools": [], "stream": True}


def gateway(contract, tmp_path, upstream, arm="baseline"):
    contract["identity"]["base_url"] = upstream[0]
    directory = tmp_path / "gateway"
    (directory / "previews").mkdir(parents=True)
    (directory / "previews/1.json").write_text('{"tools":[]}', encoding="utf-8")
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
        for _ in range(8):
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
