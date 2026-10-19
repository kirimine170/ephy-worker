"""Isolated，explicit DDGS engine calls with parent-owned HTTP admission．"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil

from .schema import Candidate
from .search import SearchError, validate_query
from .tavily_keyless import _json_object, _result_url, _unicode_string

ENDPOINTS = {
    "duckduckgo": "https://html.duckduckgo.com/html/",
    "brave": "https://search.brave.com/search",
    "mojeek": "https://www.mojeek.com/search",
    "google": "https://www.google.com/wml/search",
    "yahoo": "https://search.yahoo.com/search",
}
VERSIONS = {"ddgs": "9.16.0", "primp": "2.0.1", "click": "8.5.0"}
_IPC_LIMIT = 256 * 1024


def _environment() -> dict:
    # An allowlist，rather than credential-name heuristics，also removes DDGS_PROXY．
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP") if name in os.environ}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    if os.name == "nt":
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return env


def _results(items, query_id, backend, maximum):
    if not isinstance(items, list) or not items or len(items) > maximum:
        raise SearchError("ddgs_indeterminate_results")
    results = []
    for rank, item in enumerate(items, 1):
        if (not isinstance(item, dict) or set(item) != {"title", "href", "body"}
                or any(not _unicode_string(item[key]) for key in item)
                or not item["title"].strip() or not _result_url(item["href"])):
            raise SearchError("ddgs_invalid_result")
        results.append(Candidate(url=_result_url(item["href"]), title=item["title"][:500],
                                 snippet=item["body"][:1000], query_id=query_id, engine=backend, rank=rank))
    return results


class DDGSSearchProvider:
    def __init__(self, config, budget):
        self.config, self.budget = config, budget
        self.base_url = config.endpoint()
        self.last_diagnostic = {}
        try:
            if any(version(name) != expected for name, expected in VERSIONS.items()):
                raise SearchError("ddgs_dependency_version_mismatch")
        except PackageNotFoundError:
            raise SearchError("ddgs_optional_dependency_missing") from None

    @property
    def metadata(self):
        return {"provider": "ddgs", "engine": self.config.backend, "backend": self.config.backend,
                "region": self.config.region, "versions": VERSIONS, "automatic_fallback": False,
                "impersonate": "chrome_146", "impersonate_os": "windows"}

    async def engine_health(self):
        return {**self.metadata, "configured": True, "enabled": True, "network_probe": False,
                "availability": "not_probed"}

    async def search(self, query, query_id):
        query = validate_query(query)
        self.budget.check()
        start, before = time.monotonic(), self.budget.counts["search_requests"]
        self.last_diagnostic = {"engine": self.config.backend, "cache": "miss"}
        process = None
        try:
            async with asyncio.timeout(min(self.config.timeout_seconds, self.budget.limits.request_seconds,
                                           self.budget.remaining_seconds)):
                process = await asyncio.create_subprocess_exec(
                    sys._base_executable if os.name == "nt" else sys.executable,
                    "-s", "-m", "ephy_worker.ddgs_child",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=_environment(), limit=_IPC_LIMIT,
                )
                watched = psutil.Process(process.pid)
                process.stdin.write((json.dumps({"query": query, "backend": self.config.backend,
                                                "region": self.config.region, "maximum": self.config.max_results,
                                                "bytes": self.config.response_bytes,
                                                "timeout": min(self.config.timeout_seconds,
                                                               self.budget.limits.request_seconds)}) + "\n").encode())
                await process.stdin.drain()
                while True:
                    task = asyncio.create_task(process.stdout.readline())
                    try:
                        while not task.done():
                            try:
                                if watched.memory_info().rss > self.budget.limits.parser_memory_mb * 1024**2:
                                    raise SearchError("ddgs_memory_limit")
                            except psutil.NoSuchProcess:
                                pass
                            except psutil.AccessDenied:
                                raise SearchError("ddgs_resource_monitor_unavailable") from None
                            await asyncio.wait({task}, timeout=0.02)
                        line = task.result()
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    try:
                        msg = json.loads(line, object_pairs_hook=_json_object)
                    except (ValueError, UnicodeError, RecursionError):
                        raise SearchError("ddgs_invalid_child_response") from None
                    if not isinstance(msg, dict):
                        raise SearchError("ddgs_invalid_child_response")
                    if msg.get("type") == "request":
                        if set(msg) != {"type", "method", "url"} or msg["url"] != self.base_url:
                            raise SearchError("ddgs_endpoint_refused")
                        expected = "POST" if self.config.backend == "duckduckgo" else "GET"
                        if msg["method"] != expected:
                            raise SearchError("ddgs_endpoint_refused")
                        self.budget.take("search_requests")
                        process.stdin.write(b'{"permit":true}\n')
                        await process.stdin.drain()
                    elif msg.get("type") == "error":
                        code = msg.get("code")
                        if code not in {"challenge", "rate_limited", "access_denied", "redirect_refused",
                                        "http_error", "timeout", "network_error", "response_size_limit",
                                        "invalid_content_type", "indeterminate_results", "parse_error",
                                        "endpoint_refused", "dependency_error", "invalid_request"}:
                            raise SearchError("ddgs_invalid_child_response")
                        status, retry = msg.get("status"), msg.get("retry_after")
                        if status is not None and (type(status) is not int or not 100 <= status <= 599):
                            raise SearchError("ddgs_invalid_child_response")
                        if retry is not None and (type(retry) not in {int, float} or retry < 0 or not math.isfinite(retry)):
                            raise SearchError("ddgs_invalid_child_response")
                        raise SearchError("ddgs_" + code, status=status, retry_after=retry)
                    elif msg.get("type") == "result" and set(msg) == {"type", "results"}:
                        result = _results(msg["results"], query_id, self.config.backend, self.config.max_results)
                        self.last_diagnostic.update(result_count=len(result), code="ok")
                        return result
                    else:
                        raise SearchError("ddgs_invalid_child_response")
        except (TimeoutError, asyncio.LimitOverrunError):
            self.last_diagnostic["code"] = "ddgs_timeout"
            raise SearchError("ddgs_timeout") from None
        except (ValueError, BrokenPipeError, ConnectionError):
            self.last_diagnostic["code"] = "ddgs_invalid_child_response"
            raise SearchError("ddgs_invalid_child_response") from None
        except OSError:
            self.last_diagnostic["code"] = "ddgs_child_start_failed"
            raise SearchError("ddgs_child_start_failed") from None
        except SearchError as exc:
            self.last_diagnostic["code"] = exc.code
            raise
        finally:
            self.last_diagnostic.update(latency_seconds=round(time.monotonic() - start, 3),
                                        actual_requests=self.budget.counts["search_requests"] - before)
            if process is not None:
                if process.returncode is None:
                    process.kill()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    raise SearchError("ddgs_child_reap_failed") from None
                if process.stdin:
                    process.stdin.close()

    async def aclose(self):
        pass
