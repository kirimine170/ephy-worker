"""Tavily basic search for free-plan validation，without paid fallback or retries．"""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from .budget import Budget, BudgetExceeded
from .config import SearchConfig
from .schema import Candidate
from .search import SearchError, normalize_url, validate_query


def _usage_field_diagnostic(account: dict, name: str) -> dict:
    """Describe only expected fields，never copy arbitrary account or credential strings．"""
    if name not in account:
        return {"type": "missing"}
    value = account[name]
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "boolean", "value": value}
    if type(value) in {int, float}:
        if -1_000_000_000 <= value <= 1_000_000_000:
            return {"type": "number", "value": value}
        return {"type": "number", "value": "out_of_diagnostic_range"}
    if isinstance(value, str):
        if name == "current_plan" and value.casefold() in {"researcher", "free"}:
            return {"type": "string", "value": value.casefold()}
        if len(value) <= 20 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            return {"type": "string", "value": value}
        return {"type": "string", "value": "redacted_unrecognized_value"}
    return {"type": "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "other"}


class TavilySearchProvider:
    def __init__(
        self, config: SearchConfig, budget: Budget, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.config, self.budget = config, budget
        self.base_url = config.endpoint()
        self._api_key = config.resolve_api_key()
        self.client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=config.timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self.last_diagnostic: dict = {}
        self._account: dict = {}
        self._remaining: float | None = None
        self._attempted = 0
        self._known_credits: float = 0
        self._known_responses = 0
        self._halt: SearchError | None = None
        # Serialize usage-check + credit reservation，including callers outside the CLI．
        self._lock = asyncio.Lock()

    @property
    def metadata(self) -> dict:
        return {
            "provider": "tavily",
            "engine": "tavily",
            "search_depth": "basic",
            "auto_parameters": False,
            "free_plan_required": True,
            "max_credits_per_job": self.config.max_credits_per_job,
            "search_attempts": self._attempted,
            "credits_reserved": self._attempted,
            "credits_reported": self._known_credits if self._known_responses else None,
            "usage_known_requests": self._known_responses,
            "usage_unknown_requests": self._attempted - self._known_responses,
            "account": dict(self._account),
            "remaining_credits_conservative": self._remaining,
        }

    async def _json(self, path: str, *, body: dict | None = None) -> dict:
        if path not in {"/usage", "/search"}:
            raise SearchError("tavily_endpoint_not_allowed")
        self.budget.take("search_requests")
        try:
            async with asyncio.timeout(
                min(
                    self.config.timeout_seconds,
                    self.budget.limits.request_seconds,
                    self.budget.remaining_seconds,
                )
            ):
                async with self.client.stream(
                    "POST" if body is not None else "GET",
                    self.base_url + path,
                    json=body,
                    headers={"Authorization": "Bearer " + self._api_key.get_secret_value()},
                ) as response:
                    self.last_diagnostic.update(
                        stage=path.lstrip("/"), endpoint=path, http_status=response.status_code
                    )
                    if response.status_code != 200:
                        code = {
                            401: "tavily_authentication_failed",
                            403: "tavily_access_denied",
                            429: "tavily_rate_limited",
                            432: "tavily_quota_exhausted",
                            433: "tavily_paygo_limit",
                        }.get(response.status_code, "tavily_http_error")
                        # Never store error bodies，request headers or arbitrary upstream messages．
                        raise SearchError(code, status=response.status_code)
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.config.response_bytes:
                            raise SearchError("tavily_response_size_limit")
                    try:
                        payload = json.loads(content)
                    except (ValueError, UnicodeError):
                        raise SearchError("tavily_invalid_json") from None
                    if not isinstance(payload, dict) or "error" in payload or "detail" in payload:
                        raise SearchError("tavily_invalid_response")
                    return payload
        except (TimeoutError, httpx.TimeoutException):
            raise SearchError("tavily_timeout") from None
        except httpx.RequestError:
            raise SearchError("tavily_network_error") from None

    async def _check_allowance(self) -> dict:
        self.last_diagnostic = {"provider": "tavily", "stage": "usage", "endpoint": "/usage"}
        payload = await self._json("/usage")
        account = payload.get("account")
        if not isinstance(account, dict):
            self.last_diagnostic.update(reason="account_object_missing_or_invalid")
            raise SearchError("tavily_free_allowance_unverified")
        plan = account.get("current_plan")
        fields = ("plan_usage", "plan_limit", "paygo_usage", "paygo_limit")
        self.last_diagnostic["account_fields"] = {
            name: _usage_field_diagnostic(account, name) for name in ("current_plan", *fields)
        }
        values = [account.get(key) for key in fields]
        invalid = [
            name
            for name, value in zip(fields, values, strict=True)
            if not (name == "paygo_limit" and name in account and value is None)
            and (type(value) not in {int, float} or not 0 <= value < float("inf"))
        ]
        if invalid:
            self.last_diagnostic.update(
                reason="usage_fields_not_nonnegative_numbers", unverified_fields=invalid
            )
            raise SearchError("tavily_free_allowance_unverified")
        self._account = dict(zip(fields, values, strict=True))
        # Do not save arbitrary account strings．Only recognized free plan names are retained．
        if not isinstance(plan, str) or plan.casefold() not in {"researcher", "free"}:
            raise SearchError("tavily_free_plan_required")
        self._account["current_plan"] = plan.casefold()
        # /usage can return an explicit null limit on Researcher accounts．It does
        # not prove PAYG is disabled．Preserve null，and authorize only against the
        # independently verified free-plan balance and local reservations below．
        if account["paygo_limit"] not in (None, 0) or account["paygo_usage"] != 0:
            raise SearchError("tavily_paygo_must_be_disabled")
        if not 0 < account["plan_limit"] <= 1000:
            self.last_diagnostic.update(
                reason="plan_limit_outside_free_range", unverified_fields=["plan_limit"]
            )
            raise SearchError("tavily_free_allowance_unverified")
        remaining = max(0, account["plan_limit"] - account["plan_usage"])
        # Usage may be eventually consistent．Never restore locally reserved credits within a job．
        self._remaining = remaining if self._remaining is None else min(self._remaining, remaining)
        if self._remaining < 1:
            raise SearchError("tavily_free_credits_exhausted")
        return {
            "engine": "tavily",
            "configured": True,
            "enabled": True,
            "free_plan_verified": True,
            "paygo_limit_state": "not_reported" if account["paygo_limit"] is None else "zero",
        }

    async def engine_health(self) -> dict:
        async with self._lock:
            return await self._check_allowance()

    async def search(self, query: str, query_id: str) -> list[Candidate]:
        query = validate_query(query)
        async with self._lock:
            self.last_diagnostic = {"provider": "tavily", "engine": "tavily"}
            if self._halt:
                raise self._halt
            if self._attempted >= self.config.max_credits_per_job:
                raise BudgetExceeded("tavily_credits_per_job")
            try:
                await self._check_allowance()
                # Reserve before sending，including uncertain timeouts and cancellation．
                self.budget.check()
                if self.budget.counts["search_requests"] >= self.budget.limits.search_requests:
                    raise BudgetExceeded("search_requests")
                self._attempted += 1
                self._remaining -= 1
                payload = await self._json(
                    "/search",
                    body={
                        "query": query,
                        "search_depth": "basic",
                        "topic": "general",
                        "max_results": self.config.max_results,
                        "auto_parameters": False,
                        "include_answer": False,
                        "include_raw_content": False,
                        "include_images": False,
                        "include_usage": True,
                        **({"language": self.config.language} if self.config.language != "auto" else {}),
                    },
                )
                usage = payload.get("usage")
                credits = usage.get("credits") if isinstance(usage, dict) else None
                valid_credits = type(credits) in {int, float} and 0 <= credits < float("inf")
                if valid_credits:
                    self._known_credits += credits
                    self._known_responses += 1
                    if credits > 1:
                        raise SearchError("tavily_unexpected_credit_usage")
                self.last_diagnostic["credits_reported"] = credits if valid_credits else None
                items = payload.get("results")
                if not isinstance(items, list):
                    raise SearchError("tavily_missing_results")
                results = []
                for rank, item in enumerate(items, 1):
                    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                        raise SearchError("tavily_invalid_result")
                    if any(not isinstance(item.get(key, ""), str) for key in ("title", "content")):
                        raise SearchError("tavily_invalid_result")
                    url = normalize_url(item["url"])
                    if url:
                        results.append(
                            Candidate(
                                url=url,
                                title=item.get("title", "")[:500],
                                snippet=item.get("content", "")[:1000],
                                query_id=query_id,
                                engine="tavily",
                                rank=rank,
                            )
                        )
                    if len(results) == self.config.max_results:
                        break
                self.last_diagnostic["result_count"] = len(results)
                return results
            except SearchError as exc:
                # Stop this provider for the remainder of the job，with no retries or fallback．
                self._halt = exc
                self.last_diagnostic["error"] = exc.code
                raise

    async def aclose(self) -> None:
        await self.client.aclose()
