from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ephy_worker import ddgs_child, ddgs_search
from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import SearchConfig
from ephy_worker.ddgs_search import DDGSSearchProvider, _environment, _results
from ephy_worker.schema import Limits
from ephy_worker.search import QueryPrivacyError, SearchError


def config(**kwargs):
    return SearchConfig(provider="ddgs", backend="duckduckgo", region="jp-jp", **kwargs)


@pytest.mark.parametrize("extra", [{"backend": "auto"}, {"backend": "bing"}, {"region": "auto"},
                                  {"retries": 1}, {"api_key_env": "TOKEN"}, {"language": "ja"}])
def test_config_rejects_implicit_or_unsafe_settings(extra):
    with pytest.raises(ValidationError):
        SearchConfig(**{"provider": "ddgs", "backend": "duckduckgo", "region": "jp-jp", **extra})


def test_config_requires_backend_region_and_fixed_versions():
    with pytest.raises(ValidationError):
        SearchConfig(provider="ddgs")
    provider = DDGSSearchProvider(config(), Budget(Limits()))
    assert provider.metadata["versions"]["ddgs"] == "9.16.0"
    assert provider.base_url == "https://html.duckduckgo.com/html/"
    assert not provider.metadata["automatic_fallback"]


def test_child_environment_excludes_secrets_proxies(monkeypatch):
    keys = {"TAVILY_API_KEY", "EPHY_DISCORD_BOT_TOKEN", "HTTP_PROXY", "DDGS_PROXY", "HOME"}
    for key in keys:
        monkeypatch.setenv(key, "sensitive-value")
    assert not set(_environment()) & keys


@pytest.mark.parametrize("rows", [[], {}, [{"title": "x", "href": "http://127.0.0.1/a", "body": "x"}],
                                  [{"title": "\ud800", "href": "https://example.com", "body": "x"}]])
def test_indeterminate_and_invalid_result_are_never_empty_success(rows):
    with pytest.raises(SearchError):
        _results(rows, "q", "duckduckgo", 5)


class FakeProcess:
    def __init__(self, messages):
        self.pid, self.returncode = 1, None
        self.killed = self.waited = False
        self.stdout = SimpleNamespace(readline=self.readline)
        self.stdin = SimpleNamespace(write=lambda b: None, drain=self.drain, close=lambda: None)
        self.messages = messages

    async def readline(self):
        value = self.messages.pop(0)
        if value == "wait":
            await asyncio.sleep(60)
        return (json.dumps(value) + "\n").encode()

    async def drain(self):
        pass

    def kill(self):
        self.killed, self.returncode = True, -1

    async def wait(self):
        self.waited = True
        return self.returncode


def fake_process(monkeypatch, messages):
    process = FakeProcess(messages)

    async def spawn(*args, **kwargs):
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        if sys.platform == "win32":
            assert args[0] == sys._base_executable
            assert kwargs["env"]["__PYVENV_LAUNCHER__"] == sys.executable
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ddgs_search.psutil, "Process", lambda pid: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=1024)))
    return process


async def test_every_http_admitted_and_child_reaped(monkeypatch):
    messages = [{"type": "request", "method": "POST", "url": config().endpoint()}] * 2 + [
        {"type": "result", "results": [{"title": "doc", "href": "https://example.com", "body": "text"}]}]
    process = fake_process(monkeypatch, messages)
    provider = DDGSSearchProvider(config(), Budget(Limits()))
    assert len(await provider.search("public query", "q")) == 1
    assert provider.budget.counts["search_requests"] == 2
    assert process.killed and process.waited and provider.last_diagnostic["actual_requests"] == 2


async def test_budget_denies_and_reaps(monkeypatch):
    process = fake_process(monkeypatch, [{"type": "request", "method": "POST", "url": config().endpoint()}] * 2)
    provider = DDGSSearchProvider(config(), Budget(Limits(search_requests=1)))
    with pytest.raises(BudgetExceeded):
        await provider.search("public query", "q")
    assert process.killed and process.waited and provider.budget.counts["search_requests"] == 1


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_cancel_and_timeout_reap_owned_child(monkeypatch, mode):
    process = fake_process(monkeypatch, ["wait"])
    provider = DDGSSearchProvider(config(timeout_seconds=0.03 if mode == "timeout" else 10), Budget(Limits()))
    task = asyncio.create_task(provider.search("public query", "q"))
    if mode == "cancel":
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(SearchError, match="ddgs_timeout"):
            await task
    assert process.killed and process.waited


async def test_privacy_before_child_creation(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **kw: pytest.fail("private query"))
    with pytest.raises(QueryPrivacyError):
        await DDGSSearchProvider(config(), Budget(Limits())).search("api_key=private", "q")


@pytest.mark.parametrize("status,code", [(202, "challenge"), (429, "rate_limited"), (403, "access_denied"),
                                        (302, "redirect_refused"), (500, "http_error")])
def test_native_stream_status_retry_after_closes_without_body(monkeypatch, status, code):
    import primp
    closed, profile = [], []
    response = SimpleNamespace(status_code=status, headers={"retry-after": "600"},
                               iter_bytes=lambda: pytest.fail("do not read error body"), close=lambda: closed.append(True))

    class Client:
        def __init__(self, **kwargs):
            profile.append(kwargs)

        def headers_update(self, value):
            assert value == {}

        def request(self, *args, **kwargs):
            assert kwargs["stream"] is True
            return response

    monkeypatch.setattr(primp, "Client", Client)
    monkeypatch.setattr(ddgs_child, "_send", lambda value: None)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(readline=lambda n: b'{"permit":true}\n')))
    with pytest.raises(SearchError, match=code) as error:
        ddgs_child.run({"query": "public query", "backend": "duckduckgo", "region": "jp-jp",
                        "maximum": 5, "bytes": 1024, "timeout": 5})
    assert error.value.status == status and error.value.retry_after == 600
    assert closed == [True] and profile[0]["impersonate"] == "chrome_146"
    assert profile[0]["follow_redirects"] is False and profile[0]["verify"] is True


@pytest.mark.parametrize("mode", ["success", "oversize", "challenge", "unicode", "timeout"])
def test_native_stream_boundaries_and_parser(monkeypatch, mode):
    import primp
    closed, sent = [], []
    bodies = {"success": b'<div class="body"><h2>Reference</h2><a href="https://example.com/doc">text</a></div>',
              "oversize": b"x" * 1025, "challenge": b'<form id="anomaly-form"></form>', "unicode": b"\xff"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def headers_update(self, value):
            pass

        def request(self, *args, **kwargs):
            assert sent[0]["type"] == "request"
            if mode == "timeout":
                raise primp.TimeoutError("private error must not escape")
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"},
                                   iter_bytes=lambda: iter([bodies[mode]]), close=lambda: closed.append(True))

    monkeypatch.setattr(primp, "Client", Client)
    monkeypatch.setattr(ddgs_child, "_send", sent.append)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(readline=lambda n: b'{"permit":true}\n')))
    spec = {"query": "public query", "backend": "duckduckgo", "region": "jp-jp",
            "maximum": 5, "bytes": 1024, "timeout": 5}
    if mode == "success":
        ddgs_child.run(spec)
        assert sent[-1]["type"] == "result" and sent[-1]["results"][0]["title"] == "Reference"
    else:
        code = {"oversize": "response_size_limit", "unicode": "parse_error"}.get(mode, mode)
        with pytest.raises(SearchError, match=code):
            ddgs_child.run(spec)
    assert closed == ([] if mode == "timeout" else [True])


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_real_interpreter_is_reaped_without_descendants(monkeypatch, mode):
    original = asyncio.create_subprocess_exec
    owned = []

    async def spawn(*args, **kwargs):
        process = await original(args[0], "-s", "-c", "import time; time.sleep(60)", **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    provider = DDGSSearchProvider(config(timeout_seconds=0.3 if mode == "timeout" else 5), Budget(Limits()))
    task = asyncio.create_task(provider.search("public query", "q"))
    if mode == "cancel":
        while not owned:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.03)
        assert ddgs_search.psutil.Process(owned[0].pid).children(recursive=True) == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(SearchError, match="ddgs_timeout"):
            await task
    assert owned and owned[0].returncode is not None
    assert not ddgs_search.psutil.pid_exists(owned[0].pid)
