"""Real typed-model/SSE path：progress，stalls，loops and autonomous delivery．"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from test_discord_answer import fixture_report

from ephy_worker import discord_answer as answer
from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import ModelProfile, SearchConfig, WorkerConfig
from ephy_worker.discord_adapter import Answer, DiscordAdapter, DiscordSettings, Ledger, Request
from ephy_worker.discord_cli import make_executor
from ephy_worker.models import ModelError, ModelRunner, _BudgetTransport, _GenerationMonitor
from ephy_worker.schema import Limits
from ephy_worker.store import JobStore


class TypedAnswer(BaseModel):
    answer: str


def profile(**updates):
    return ModelProfile(
        family="qwen", base_url="http://127.0.0.1:9/v1", model_id="fixture",
        output_mode=updates.pop("output_mode", "prompted_json"), timeout_seconds=updates.pop("timeout_seconds", .01),
        context_tokens=32768, max_output_tokens=2048, **updates,
    )


def frame(content=None, *, reasoning=None, finish=None, usage=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    result = {
        "id": "fixture-completion", "object": "chat.completion.chunk", "created": 1, "model": "actual-fixture",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        result["usage"] = usage
    return ("data: " + json.dumps(result, ensure_ascii=False) + "\n\n").encode()


DONE = b"data: [DONE]\n\n"


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, error=None, hang=False):
        self.chunks, self.delay, self.error, self.hang = chunks, delay, error, hang
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Future()

    async def aclose(self):
        self.closed = True


def response(stream):
    return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream)


def free_budget(**updates):
    budget = Budget(Limits(job_seconds=.001, request_seconds=.001, model_requests=1, **updates), enforce_time_limit=False)
    budget.started -= 1200
    return budget


async def test_real_typed_model_progress_ignores_elapsed_caps_preserves_payload_usage_and_utf8():
    chunks = [frame('{"answer":"'), frame("日本語", reasoning="private reasoning progresses"), frame('です．"}', finish="stop")]
    chunks.insert(-1, frame("", reasoning="more private reasoning"))
    usage = {"prompt_tokens": 31, "completion_tokens": 17, "total_tokens": 48}
    final_usage = json.loads(frame("").decode()[6:])
    final_usage.update(choices=[], usage=usage)
    chunks += [("data: " + json.dumps(final_usage) + "\n\n").encode(), DONE]
    # Split every UTF-8 byte；decoder must preserve multibyte Japanese and JSON framing．
    split = [bytes([value]) for value in b"".join(chunks)]
    stream = Stream(split)
    observed, events = [], []

    async def handler(request):
        payload = json.loads(await request.aread())
        observed.append(payload)
        assert payload["model"] == "fixture" and payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["max_tokens"] == 2048 and "tools" not in payload
        assert "公開質問" in json.dumps(payload, ensure_ascii=False)
        assert request.extensions["timeout"] == {"connect": 10, "read": None, "write": 30, "pool": 10}
        # Initial connection exceeds all old profile/request/aggregate limits．
        await asyncio.sleep(.025)
        return response(stream)

    budget = free_budget()
    runner = ModelRunner(profile(max_tokens_parameter="max_tokens", temperature=.2, enable_thinking=True), budget,
                         transport=httpx.MockTransport(handler), wait_for_completion=True,
                         generation_idle_seconds=.3, on_generation_event=events.append)
    try:
        result = await runner.run(TypedAnswer, "公開質問です．", stage="fixture")
        assert result.answer == "日本語です．" and len(observed) == budget.counts["model_requests"] == 1
        assert observed[0]["temperature"] == .2 and observed[0]["chat_template_kwargs"] == {"enable_thinking": True}
        assert runner.metadata["actual_model_ids"] == ["actual-fixture"]
        assert (budget.input_tokens, budget.output_tokens) == (31, 17)
        assert runner.metadata["generation_wait"]["elapsed_limit_seconds"] is None
        assert [e["status"] for e in events] == ["started", "progress", "completed"]
        assert events[-1]["content_chars"] == len('{"answer":"日本語です．"}')
        assert events[-1]["reasoning_chars"] > 0 and stream.closed
        assert "private reasoning" not in json.dumps(events)
    finally:
        await runner.aclose()


async def test_same_slow_stream_bounded_control_times_out_while_progressing_wait_completes():
    streams = []

    async def handler(request):
        stream = Stream([frame('{"answer":"'), frame("one "), frame("two "), frame('three"}', finish="stop"), DONE], delay=.025)
        streams.append(stream)
        return response(stream)

    bounded = ModelRunner(profile(), Budget(Limits()), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError, match="model_timeout"):
            await bounded.run(TypedAnswer, "same slow stream", stage="bounded-control")
        assert streams[0].closed
    finally:
        await bounded.aclose()
    monitored = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(handler),
                            wait_for_completion=True, generation_idle_seconds=.09)
    try:
        assert (await monitored.run(TypedAnswer, "same slow stream", stage="progressing")).answer == "one two three"
        assert streams[-1].closed
    finally:
        await monitored.aclose()


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
async def test_nullable_empty_delta_fields_line_endings_and_done_without_tcp_eof(ending):
    chunk = json.loads(frame('{"answer":"日本語"}', finish="stop").decode()[6:])
    chunk["choices"][0]["delta"].update(tool_calls=None, function_call=None, refusal=None)
    raw = (("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode() + DONE).replace(b"\n", ending)
    stream = Stream([bytes([byte]) for byte in raw], hang=True)
    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(lambda req: response(stream)),
                         wait_for_completion=True, generation_idle_seconds=.2)
    try:
        assert (await runner.run(TypedAnswer, "fixture", stage="valid-empty-fields")).answer == "日本語"
        assert stream.closed
    finally:
        await runner.aclose()


@pytest.mark.parametrize("malformation", ["surrogate-content", "surrogate-id", "boolean-index", "tool-output"])
async def test_malformed_scalars_and_real_tool_delta_fail_closed(malformation):
    chunk = json.loads(frame('{"answer":"ok"}', finish="stop").decode()[6:])
    expected = "model_generation_malformed"
    if malformation == "surrogate-content":
        chunk["choices"][0]["delta"]["content"] = "\ud800"
    elif malformation == "surrogate-id":
        chunk["id"] = "\udfff"
    elif malformation == "boolean-index":
        chunk["choices"][0]["index"] = False
    else:
        chunk["choices"][0]["delta"]["tool_calls"] = [{"index": 0, "function": {"arguments": "{}"}}]
        expected = "model_generation_mode_unsupported"
    stream = Stream([("data: " + json.dumps(chunk) + "\n\n").encode(), DONE])
    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(lambda req: response(stream)),
                         wait_for_completion=True)
    try:
        with pytest.raises(ModelError) as caught:
            await runner.run(TypedAnswer, "fixture", stage="bad-scalar")
        assert caught.value.code == expected and stream.closed
    finally:
        await runner.aclose()


async def test_unexpected_transport_error_propagates_original_and_records_no_private_body():
    failure, events = RuntimeError("private transport bug"), []

    async def handler(request):
        raise failure

    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(handler), wait_for_completion=True,
                         on_generation_event=events.append)
    try:
        with pytest.raises(RuntimeError) as caught:
            await runner.run(TypedAnswer, "fixture", stage="unexpected")
        assert caught.value is failure
        assert events[-1]["cause"] == "model_generation_internal_error"
        assert "private transport bug" not in json.dumps(events)
    finally:
        await runner.aclose()


@pytest.mark.parametrize("kind", ["initial", "silent", "comments", "whitespace"])
async def test_no_actual_generation_progress_stalls_with_stable_cause_and_closes(kind):
    stream = Stream([] if kind in {"initial", "silent"} else
                    [b": keepalive\n\n"] * 30 if kind == "comments" else [frame(" \n")] * 30,
                    delay=.01, hang=True)
    events = []

    async def handler(request):
        if kind == "initial":
            await asyncio.Future()
        return response(stream)

    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(handler), wait_for_completion=True,
                         generation_idle_seconds=.04, on_generation_event=events.append)
    try:
        with pytest.raises(ModelError) as caught:
            await runner.run(TypedAnswer, "fixture", stage="stall")
        assert caught.value.code == "model_generation_stalled"
        assert events[-1]["status"] == "failed" and events[-1]["cause"] == caught.value.code
        if kind != "initial":
            assert stream.closed
    finally:
        await runner.aclose()


@pytest.mark.parametrize("field", ["content", "reasoning"])
@pytest.mark.parametrize("period", [32, 97, 256])
async def test_repeated_suffix_loop_is_detected_independently_without_publishing_text(field, period):
    pattern = ("0123456789abcdefghijklmnopqrstuvwxyz" * 8)[:period]
    value = pattern * 4
    stream = Stream([frame(value) if field == "content" else frame("", reasoning=value), DONE])
    events = []
    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(lambda req: response(stream)),
                         wait_for_completion=True, generation_idle_seconds=1, on_generation_event=events.append)
    try:
        with pytest.raises(ModelError, match="model_generation_loop"):
            await runner.run(TypedAnswer, "fixture", stage="loop")
        assert events[-1]["cause"] == "model_generation_loop" and pattern not in json.dumps(events)
        assert stream.closed
    finally:
        await runner.aclose()


@pytest.mark.parametrize("chunks,code", [
    ([frame('{"answer":"ok"}', finish="stop")], "model_generation_incomplete"),
    ([frame('{"answer":"ok"}'), DONE], "model_generation_incomplete"),
    ([b"data: broken\n\n", DONE], "model_generation_malformed"),
    ([b"data: []\n\n", DONE], "model_generation_malformed"),
    ([b"data: \xff\n\n", DONE], "model_generation_malformed"),
    ([frame("ok", finish="stop"), DONE + b"data: {}\n\n"], "model_generation_malformed"),
    ([b"data: " + b"x" * (8 * 1024**2 + 1)], "model_response_size_limit"),
])
async def test_incomplete_malformed_and_oversized_streams_never_succeed(chunks, code):
    stream = Stream(chunks)
    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(lambda req: response(stream)),
                         wait_for_completion=True, generation_idle_seconds=1)
    try:
        with pytest.raises(ModelError) as caught:
            await runner.run(TypedAnswer, "fixture", stage="bad-stream")
        assert caught.value.code == code and stream.closed
    finally:
        await runner.aclose()


async def test_stream_disconnect_and_cancellation_keep_distinct_causes_and_cleanup():
    stream = Stream([frame('{"answer":"')], error=httpx.ReadError("private upstream failure"))
    events = []
    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(lambda req: response(stream)),
                         wait_for_completion=True, generation_idle_seconds=1, on_generation_event=events.append)
    try:
        with pytest.raises(ModelError) as caught:
            await runner.run(TypedAnswer, "fixture", stage="disconnect")
        assert caught.value.code == "model_generation_disconnected" and stream.closed
        assert "private upstream" not in json.dumps(events)
    finally:
        await runner.aclose()
    stream = Stream([frame('{"answer":"')], hang=True)
    runner = ModelRunner(profile(), free_budget(), transport=httpx.MockTransport(lambda req: response(stream)),
                         wait_for_completion=True, generation_idle_seconds=1, on_generation_event=events.append)
    task = asyncio.create_task(runner.run(TypedAnswer, "fixture", stage="cancel"))
    await asyncio.wait_for(stream.entered.wait(), 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed and events[-1]["status"] == "cancelled"
    finally:
        await runner.aclose()


@pytest.mark.parametrize("mode", ["tool", "native_json"])
def test_unsupported_streamed_output_modes_fail_closed_before_network(mode):
    with pytest.raises(ModelError, match="model_generation_mode_unsupported"):
        ModelRunner(profile(output_mode=mode), free_budget(), wait_for_completion=True)


async def test_actual_stream_payload_context_checked_before_request_count_and_network():
    budget = free_budget()
    transport = _BudgetTransport(profile().model_copy(update={"context_tokens": 2100, "max_output_tokens": 1024}).resolve(),
                                 budget, httpx.MockTransport(pytest.fail), wait_for_completion=True)
    try:
        with pytest.raises(ModelError, match="model_context_limit"):
            await transport.handle_async_request(httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions",
                json={"model": "fixture", "messages": [{"role": "user", "content": "public " * 1000}]}))
        assert budget.counts["model_requests"] == 0
    finally:
        await transport.aclose()


async def test_wait_mode_keeps_physical_request_budget_and_no_schema_retry():
    observed = []
    stream = Stream([frame("not valid JSON", finish="stop"), DONE])
    runner = ModelRunner(profile(schema_retries=1), free_budget(),
        transport=httpx.MockTransport(lambda req: observed.append(req) or response(stream)), wait_for_completion=True)
    try:
        with pytest.raises(ModelError, match="model_schema_invalid"):
            await runner.run(TypedAnswer, "fixture", stage="invalid-schema")
        assert len(observed) == runner.budget.counts["model_requests"] == 1
        with pytest.raises(BudgetExceeded, match="model_requests"):
            await runner.run(TypedAnswer, "fixture", stage="second-call")
        assert len(observed) == 1
    finally:
        await runner.aclose()


def test_heartbeat_first_progress_rate_limit_and_bounded_loop_tail(monkeypatch):
    tick, events = [100.], []
    monkeypatch.setattr("ephy_worker.models.time.monotonic", lambda: tick[0])
    monitor = _GenerationMonitor(600, events.append)
    assert monitor.delta("first", "")
    for index in range(1, 10):
        tick[0] += 1
        monitor.delta(str(index), "different reason " + str(index))
    assert [e["status"] for e in events] == ["started", "progress"]
    tick[0] += 1
    monitor.delta("next", "")
    assert len(events) == 3 and all(event["phase"] == "stream" for event in events)
    assert monitor.delta("", " ") is False
    assert all(len(value) <= 4096 for value in monitor.tails.values())


@pytest.mark.parametrize("no_facts", [False, True])
@pytest.mark.parametrize("failure", [None, "stall", "loop", "disconnect", "schema"])
async def test_composer_real_transport_records_safe_heartbeat_typed_outcome_and_cautious_opinion(tmp_path, monkeypatch, no_facts, failure):
    store = JobStore(tmp_path / "jobs")
    report = fixture_report().model_copy(update={"job_id": store.job_id})
    if no_facts:
        report.claims.clear()
    original = report.model_dump_json()
    opinion = "条件を比べて，小さく試すのがよいと思います．"
    typed = {"opinion": opinion} if no_facts else {
        "text": "機能Aを利用できます．", "claim_ids": ["c1"], "opinion": opinion, "opinion_claim_ids": ["c1"],
    }
    chunks = [frame(json.dumps(typed, ensure_ascii=False), reasoning="private chain of thought", finish="stop"), DONE]
    if failure == "loop":
        chunks = [frame("", reasoning="a private repetitive reasoning pattern. " * 4)]
    elif failure in {"stall", "disconnect"}:
        chunks = [frame("", reasoning="initial private thought")]
    elif failure == "schema":
        chunks = [frame("{}", finish="stop"), DONE]
    stream = Stream(chunks, hang=failure == "stall", error=httpx.ReadError("secret-disconnect") if failure == "disconnect" else None)
    requests = []
    real_runner = ModelRunner

    def runner(p, budget, **kwargs):
        return real_runner(p, budget, transport=httpx.MockTransport(lambda req: requests.append(req) or response(stream)), **kwargs)

    monkeypatch.setattr(answer, "ModelRunner", runner)
    text = await answer.compose_answer(report, profile(), free_budget(), store, require_opinion=True,
        wait_for_completion=True, generation_idle_seconds=.04, previous_answer="過去の未検証の回答です．")
    saved = json.loads((store.directory / "discord-answer.json").read_bytes())
    heartbeat = json.loads((store.directory / "discord-generation.json").read_bytes())
    assert len(requests) == 1 and "わたしとしては，" in text and report.model_dump_json() == original
    assert saved["generation_wait"]["elapsed_limit_seconds"] is None
    assert heartbeat["phase"] == "answer_validation" and heartbeat["status"] == ("completed" if failure is None else "failed")
    assert saved["opinion"]["status"] == ("generated" if failure is None else "fallback")
    assert heartbeat["answer_status"] == saved["status"]
    if failure in {"stall", "loop", "disconnect"}:
        cause = "model_generation_" + {"stall": "stalled", "loop": "loop", "disconnect": "disconnected"}[failure]
        assert saved["generation_wait"]["monitor_cause"] == cause and heartbeat["cause"] == cause.removeprefix("model_")
        assert "取得できた内容をまとめて" in text
    dumped = json.dumps(heartbeat)
    assert all(value not in dumped for value in ["private", "secret-disconnect", "http://", "過去", "opinion"])


async def test_heartbeat_write_failure_propagates_original_error_through_real_sdk(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    report = fixture_report().model_copy(update={"job_id": store.job_id})
    failure = OSError("private write failure")
    monkeypatch.setattr(answer, "_save_generation", lambda *args: (_ for _ in ()).throw(failure))
    real_runner = ModelRunner
    monkeypatch.setattr(answer, "ModelRunner", lambda p, b, **kw: real_runner(p, b,
        transport=httpx.MockTransport(pytest.fail), **kw))
    with pytest.raises(OSError) as caught:
        await answer.compose_answer(report, profile(), free_budget(), store, require_opinion=True, wait_for_completion=True)
    assert caught.value is failure
    assert not (store.directory / "discord-generation.json").exists()


def settings(tmp_path, **kwargs):
    return DiscordSettings(application_id=100, guild_id=200, allowed_channel_ids=[300], allowed_user_ids=[400],
        worker_config=tmp_path / "worker.yaml", profile="fixture", output_dir=tmp_path / "results",
        state_dir=tmp_path / "state", **kwargs)


def test_wait_settings_strict_and_default_bounded_budget_unchanged(tmp_path):
    configured = settings(tmp_path)
    assert configured.wait_for_answer is False and configured.generation_idle_seconds == 600
    for value in (0, 1, "true", None):
        with pytest.raises(ValidationError):
            settings(tmp_path, wait_for_answer=value)
    bounded = Budget(Limits(job_seconds=.001))
    bounded.started -= 1200
    with pytest.raises(BudgetExceeded, match="job_timeout"):
        bounded.take("model_requests")
    free = free_budget()
    free.take("model_requests")
    with pytest.raises(BudgetExceeded, match="model_requests"):
        free.take("model_requests")
    assert "time_limit_enforced" not in bounded.snapshot() and free.snapshot()["time_limit_enforced"] is False
    json.dumps(free.snapshot(), allow_nan=False)


async def test_executor_keeps_research_resource_deadline_without_reserve_and_final_counts(tmp_path, monkeypatch):
    from ephy_worker import research

    configured = settings(tmp_path, timeout_seconds=600, require_opinion=True, wait_for_answer=True)
    config = WorkerConfig(model_profiles={"fixture": profile()}, search=SearchConfig(base_url="http://127.0.0.1:9"),
                          limits=Limits(job_seconds=600, model_requests=5))
    seen = {}

    class Research:
        def __init__(self, config, profile, store):
            seen["research_limits"] = config.limits
            self.budget = Budget(config.limits)
            self.budget.counts["model_requests"] = 3
            self.store = store

        async def run(self, question):
            return fixture_report().model_copy(update={"job_id": self.store.job_id})

    async def compose(report, p, budget, store, **kwargs):
        seen.update(kwargs)
        budget.started -= 1200
        budget.check()
        assert budget.counts["model_requests"] == 3
        assert budget.enforce_time_limit is False and budget.limits.model_requests == 5
        return "わたしとしては，まず試すのがよいと思います．"

    monkeypatch.setattr(research, "ResearchExecutor", Research)
    monkeypatch.setattr(answer, "compose_answer", compose)
    result = await make_executor(configured, config)("公開情報の比較")
    assert "わたし" in result.text
    assert seen["research_limits"].job_seconds == 600 and seen["research_limits"].model_requests == 4
    assert seen["require_opinion"] and seen["wait_for_completion"] and seen["generation_idle_seconds"] == 600
    assert "timeout_seconds" not in seen


async def test_adapter_wait_mode_delivers_final_after_old_deadline_without_user_followup(tmp_path, monkeypatch):
    configured = settings(tmp_path, wait_for_answer=True, timeout_seconds=1)
    ledger = Ledger(configured)
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(question):
        started.set()
        await release.wait()
        return Answer("completed", "わたしとしては，条件を比べるのがよいと思います．", "fixture-job")

    adapter = DiscordAdapter(configured, ledger, execute)
    reply = type("Reply", (), {"defer": AsyncMock(), "send": AsyncMock(), "reject": AsyncMock()})()
    task = asyncio.create_task(adapter.ask(Request(1, 100, 200, 300, 400, False), reply, "公開情報の比較"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(1.02)
        assert not task.done() and adapter._deadline.when() is None
        release.set()
        await asyncio.wait_for(task, 1)
        assert reply.send.await_count == 2
        assert reply.send.await_args_list[0].args == ("分かりました，調べてみますね．",)
        assert "わたしとしては" in reply.send.await_args_list[-1].args[0]
    finally:
        release.set()
        await adapter.close()



async def test_repair_nonstring_finish_reasons_fail_as_malformed_through_real_sdk_and_composer(tmp_path, monkeypatch):
    real_runner = ModelRunner
    for index, finish in enumerate(([], {}, 0, True, 3.5)):
        stream = Stream([frame('{"answer":"public fixture"}', finish=finish), DONE])
        calls = []
        runner = real_runner(profile(), free_budget(),
            transport=httpx.MockTransport(lambda req, calls=calls, stream=stream: calls.append(req) or response(stream)),
            wait_for_completion=True)
        try:
            with pytest.raises(ModelError) as caught:
                await runner.run(TypedAnswer, "public fixture", stage="malformed-finish-repair")
            assert caught.value.code == "model_generation_malformed"
            assert stream.closed and len(calls) == runner.budget.counts["model_requests"] == 1
        finally:
            await runner.aclose()

        store = JobStore(tmp_path / str(index))
        report = fixture_report().model_copy(update={"job_id": store.job_id})
        original = report.model_dump_json()
        draft = {"text": "機能Aを利用できます．", "claim_ids": ["c1"],
            "opinion": "条件を比べるのがよいと思います．", "opinion_claim_ids": ["c1"]}
        stream = Stream([frame(json.dumps(draft, ensure_ascii=False)), frame(finish=finish), DONE])
        calls = []

        def monitored(p, b, calls=calls, stream=stream, **kwargs):
            return real_runner(p, b,
                transport=httpx.MockTransport(lambda req: calls.append(req) or response(stream)), **kwargs)

        monkeypatch.setattr(answer, "ModelRunner", monitored)
        text = await answer.compose_answer(report, profile(), free_budget(), store,
            require_opinion=True, wait_for_completion=True)
        saved = json.loads((store.directory / "discord-answer.json").read_bytes())
        assert saved["status"] == "fallback" and saved["reason"] == "generation_malformed"
        assert saved["generation_wait"]["monitor_cause"] == "model_generation_malformed"
        assert saved["opinion"]["status"] == "fallback" and "わたしとしては，" in text
        assert "機能A" in text and "https://example.org/manual" in text
        assert stream.closed and len(calls) == 1 and report.model_dump_json() == original


async def test_repair_cancelled_heartbeat_write_preserves_primary_cancel_and_failure_chain():
    stream = Stream([frame('{"answer":"')], hang=True)
    calls = []
    failure = OSError("fixture cancelled heartbeat write failure")

    def notify(event):
        if event["status"] == "cancelled":
            raise failure

    runner = ModelRunner(profile(), free_budget(),
        transport=httpx.MockTransport(lambda req: calls.append(req) or response(stream)),
        wait_for_completion=True, generation_idle_seconds=1, on_generation_event=notify)
    task = asyncio.create_task(runner.run(TypedAnswer, "public fixture", stage="cancel-write-repair"))
    try:
        await asyncio.wait_for(stream.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        pending, seen, found = [caught.value], set(), False
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            found = found or error is failure
            pending.extend(link for link in (error.__cause__, error.__context__) if link is not None)
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        assert found and stream.closed and len(calls) == runner.budget.counts["model_requests"] == 1
    finally:
        if not task.done():
            task.cancel()
        await runner.aclose()


async def test_repair_handled_cancel_marker_does_not_mask_later_unrelated_error():
    stream = Stream([frame('{"answer":"')], hang=True)
    calls = []
    notification_error = OSError("fixture cancelled notification failure")
    later_error = OSError("fixture later unrelated transport failure")

    def notify(event):
        if event["status"] == "cancelled":
            raise notification_error

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return response(stream)
        raise later_error

    runner = ModelRunner(profile(), Budget(Limits(model_requests=3), enforce_time_limit=False),
        transport=httpx.MockTransport(handler), wait_for_completion=True,
        generation_idle_seconds=1, on_generation_event=notify)

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await runner.run(TypedAnswer, "public fixture", stage="handled-cancel-repair")
        assert asyncio.current_task().cancelling() == 1
        with pytest.raises(OSError) as caught:
            await runner.run(TypedAnswer, "public fixture", stage="unrelated-error-repair")
        assert caught.value is later_error
        assert runner.transport.last_cancelled_error is None

    task = asyncio.create_task(exercise())
    try:
        await asyncio.wait_for(stream.entered.wait(), 1)
        task.cancel()
        await task
        assert stream.closed and len(calls) == runner.budget.counts["model_requests"] == 2
    finally:
        if not task.done():
            task.cancel()
        await runner.aclose()


async def test_repair_bare_graph_cancellation_retains_only_contemporaneous_notification_cause(monkeypatch):
    from ephy_worker import models

    for contemporaneous in (True, False):
        notification_error = OSError("fixture cancellation notification failure")
        child_cancel = asyncio.CancelledError("fixture transport cancellation")
        child_cancel.__cause__ = notification_error
        bare_cancel = asyncio.CancelledError("fixture graph receive cancellation")
        runner = ModelRunner(profile(), free_budget(),
            transport=httpx.MockTransport(pytest.fail), wait_for_completion=True)

        async def bare_graph_run(self, *args, runner=runner, child_cancel=child_cancel,
                                 contemporaneous=contemporaneous, bare_cancel=bare_cancel, **kwargs):
            runner.transport.last_cancelled_error = child_cancel
            if contemporaneous:
                asyncio.current_task().cancel()
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    pass
            raise bare_cancel from None

        monkeypatch.setattr(models.Agent, "run", bare_graph_run)
        task = asyncio.create_task(runner.run(TypedAnswer, "public fixture", stage="bare-graph-repair"))
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value is bare_cancel
            if contemporaneous:
                assert task.cancelling() == 1
                assert caught.value.__cause__ is child_cancel
                assert child_cancel.__cause__ is notification_error
            else:
                assert task.cancelling() == 0
                assert caught.value.__cause__ is None
            assert runner.budget.counts["model_requests"] == 0
        finally:
            await runner.aclose()


async def test_repair_cleanup_wrappers_preserve_cancel_notification_failure_and_wrapper(monkeypatch):
    from anyio import ClosedResourceError

    from ephy_worker import models

    def chain_contains(root, target):
        pending, seen = [root], set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            if error is target:
                return True
            pending.extend(link for link in (error.__cause__, error.__context__) if link is not None)
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        return False

    real_run = models.Agent.run
    for kind in ("closed", "group", "api"):
        stream = Stream([frame('{"answer":"')], hang=True)
        calls = []
        notification_error = OSError("fixture cancelled heartbeat write failure")
        cleanup = {
            "closed": ClosedResourceError("fixture graph receive cleanup"),
            "group": ExceptionGroup("fixture graph cleanup", [ClosedResourceError("fixture child cleanup")]),
            "api": ValueError("fixture backend wrapper"),
        }[kind]

        def notify(event, notification_error=notification_error):
            if event["status"] == "cancelled":
                raise notification_error

        async def cleanup_replacement(self, *args, cleanup=cleanup, **kwargs):
            try:
                return await real_run(self, *args, **kwargs)
            except asyncio.CancelledError:
                pass
            raise cleanup

        runner = ModelRunner(profile(), free_budget(),
            transport=httpx.MockTransport(lambda req, calls=calls, stream=stream: calls.append(req) or response(stream)),
            wait_for_completion=True, generation_idle_seconds=1, on_generation_event=notify)
        monkeypatch.setattr(models.Agent, "run", cleanup_replacement)
        task = asyncio.create_task(runner.run(TypedAnswer, "public fixture", stage="cleanup-wrapper-repair"))
        try:
            await asyncio.wait_for(stream.entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            marker = runner.transport.last_cancelled_error
            assert task.cancelling() == 1 and isinstance(marker, asyncio.CancelledError)
            assert chain_contains(marker, notification_error)
            assert chain_contains(caught.value, marker)
            assert chain_contains(caught.value, notification_error)
            assert chain_contains(caught.value, cleanup)
            assert stream.closed and len(calls) == runner.budget.counts["model_requests"] == 1
        finally:
            if not task.done():
                task.cancel()
            await runner.aclose()
