"""Explicit free Tavily keyless search；credentials and fallback are never used．"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
from urllib.parse import urlsplit

import httpx

from .budget import Budget
from .config import SearchConfig
from .schema import Candidate
from .search import SearchError, normalize_url, retry_after_seconds, validate_query


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("nonfinite JSON number")


def _nonnegative_finite_number(value) -> bool:
    try:
        return type(value) in {int, float} and value >= 0 and math.isfinite(value)
    except OverflowError:
        # JSON integers are arbitrary precision；isfinite converts them to float．
        return False


def _unicode_string(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _result_url(value: str) -> str | None:
    url = normalize_url(value)
    if not url or "\\" in value or any(ord(char) < 33 for char in value):
        return None
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        return url if ipaddress.ip_address(host).is_global else None
    except ValueError:
        if (
            "." not in host or "%" in host
            or host.endswith((".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".test"))
            or all(part.isdigit() or part.startswith("0x") for part in host.split("."))
        ):
            return None
        return url


def _parse(payload: object, query: str, query_id: str, maximum: int) -> list[Candidate]:
    allowed = {"query", "results", "answer", "images", "follow_up_questions", "response_time", "request_id", "usage"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise SearchError("tavily_keyless_invalid_response")
    if "query" in payload and (not _unicode_string(payload["query"]) or payload["query"] != query):
        raise SearchError("tavily_keyless_invalid_response")
    if payload.get("answer") not in (None, "") or payload.get("images", []) != []:
        raise SearchError("tavily_keyless_unexpected_generated_content")
    if payload.get("follow_up_questions") not in (None, []):
        raise SearchError("tavily_keyless_invalid_response")
    if "request_id" in payload and not _unicode_string(payload["request_id"]):
        raise SearchError("tavily_keyless_invalid_response")
    if "response_time" in payload and (
        not _nonnegative_finite_number(payload["response_time"])
    ):
        raise SearchError("tavily_keyless_invalid_response")
    if "usage" in payload:
        usage = payload["usage"]
        if (
            not isinstance(usage, dict) or set(usage) != {"credits"}
            or not _nonnegative_finite_number(usage["credits"])
        ):
            raise SearchError("tavily_keyless_invalid_response")
    items = payload.get("results")
    if not isinstance(items, list) or len(items) > maximum:
        raise SearchError("tavily_keyless_invalid_results")
    results = []
    for rank, item in enumerate(items, 1):
        if (
            not isinstance(item, dict)
            or set(item) - {"url", "title", "content", "score", "raw_content", "published_date", "favicon"}
            or any(not _unicode_string(item.get(key)) for key in ("url", "title", "content"))
            or not item["title"].strip()
            or item.get("raw_content") is not None
            or any(item.get(key) is not None and not _unicode_string(item[key])
                   for key in ("published_date", "favicon"))
            or "score" in item and (not _nonnegative_finite_number(item["score"]) or item["score"] > 1)
        ):
            raise SearchError("tavily_keyless_invalid_result")
        url = _result_url(item["url"])
        if not url:
            raise SearchError("tavily_keyless_invalid_result")
        results.append(Candidate(
            url=url, title=item["title"][:500], snippet=item["content"][:1000],
            query_id=query_id, engine="tavily", rank=rank,
        ))
    return results


class TavilyKeylessSearchProvider:
    def __init__(self, config: SearchConfig, budget: Budget, *, transport=None):
        self.config, self.budget = config, budget
        self.base_url = config.endpoint()
        self.client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False,
            timeout=config.timeout_seconds,
            headers={"Accept": "application/json", "X-Tavily-Access-Mode": "keyless"},
        )
        self.last_diagnostic: dict = {}

    @property
    def metadata(self) -> dict:
        return {"provider": "tavily_keyless", "engine": "tavily", "access_mode": "keyless",
                "search_depth": "basic", "auto_parameters": False, "automatic_fallback": False}

    async def engine_health(self) -> dict:
        return {**self.metadata, "configured": True, "enabled": True,
                "network_probe": False, "availability": "not_probed"}

    async def search(self, query: str, query_id: str) -> list[Candidate]:
        query = validate_query(query)
        self.last_diagnostic = {"engine": "tavily"}
        self.budget.take("search_requests")
        try:
            async with asyncio.timeout(min(self.config.timeout_seconds, self.budget.limits.request_seconds,
                                           self.budget.remaining_seconds)):
                async with self.client.stream("POST", self.base_url + "/search", json={
                    "query": query, "search_depth": "basic", "topic": "general",
                    "max_results": self.config.max_results, "auto_parameters": False,
                    "include_answer": False, "include_raw_content": False, "include_images": False,
                    **({"language": self.config.language} if self.config.language != "auto" else {}),
                }) as response:
                    if response.status_code != 200:
                        code = {429: "rate_limited", 401: "access_denied", 403: "access_denied"}.get(
                            response.status_code, "redirect_refused" if 300 <= response.status_code < 400
                            else "http_error")
                        raise SearchError("tavily_keyless_" + code, status=response.status_code,
                                          retry_after=retry_after_seconds(response.headers.get("retry-after")))
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise SearchError("tavily_keyless_invalid_content_type")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > self.config.response_bytes:
                            raise SearchError("tavily_keyless_response_size_limit")
                        content.extend(chunk)
                    try:
                        payload = json.loads(content, object_pairs_hook=_json_object,
                                             parse_constant=_invalid_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        raise SearchError("tavily_keyless_invalid_json") from None
                    candidates = _parse(payload, query, query_id, self.config.max_results)
                    self.last_diagnostic["result_count"] = len(candidates)
                    return candidates
        except (TimeoutError, httpx.TimeoutException):
            raise SearchError("tavily_keyless_timeout") from None
        except httpx.RequestError:
            raise SearchError("tavily_keyless_network_error") from None
        except SearchError as exc:
            self.last_diagnostic["code"] = exc.code
            raise

    async def aclose(self) -> None:
        await self.client.aclose()
