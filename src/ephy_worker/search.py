"""Bounded SearXNG JSON calls to the explicitly configured search service．"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from .budget import Budget
from .config import SearchConfig
from .schema import Candidate


class QueryPrivacyError(ValueError):
    pass


class SearchError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(code)


_PRIVATE_PATTERNS = {
    "private_key": r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----",
    "credential": r"\b(?:Bearer\s+\S+|AKIA[0-9A-Z]{16}|ghp_\w{20,}|github_pat_\w{20,}|sk-[\w-]{15,})",
    "credential_assignment": r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+",
    "jwt": r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}",
    "local_path": r"(?:(?<![A-Z0-9])[A-Z]:[\\/]\S+|\\\\[^\s\\]+\\\S+|file://|(?<![\w/])~[\\/]\S+)",
    "email": r"[\w.+-]+@[\w.-]+\.[A-Z]{2,}",
    "internal_host": r"\b(?:localhost|[\w.-]+\.(?:local|internal|intranet|lan))\b",
    "private_context": r"社外秘|部外秘|機密情報|顧客情報|個人情報|internal[- ]only|confidential|do not share",
    "raw_log": r"```|Traceback \(most recent call last\)|(?:^|\s)(?:user|assistant|system):",
    "phone": r"(?<!\d)(?:\+81[- ]?|0)\d{1,4}[- ]\d{1,4}[- ]\d{3,4}(?!\d)",
    "engine_override": r"(?:^|\s)[!:]\S+|!!",
}


def validate_public_text(value: str, *, max_chars: int = 8000) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    normalized = " ".join(normalized.split())
    if not normalized or len(normalized) > max_chars:
        raise QueryPrivacyError(f"public text length must be 1–{max_chars} characters")
    inspected = normalized
    # Decode only the inspection copy．Do not rewrite legitimate public URLs or search syntax．
    for _ in range(3):
        decoded = unicodedata.normalize("NFKC", html.unescape(unquote(inspected)))
        decoded = "".join(char for char in decoded if unicodedata.category(char) != "Cf")
        if decoded == inspected:
            break
        inspected = decoded
    for reason, pattern in _PRIVATE_PATTERNS.items():
        if re.search(pattern, inspected, re.IGNORECASE):
            raise QueryPrivacyError(f"query rejected: {reason}")
    # Exclude full HTTP(S) URLs before looking for POSIX paths．The same /Users/... path
    # can be public in a URL，but a bare absolute local path must never become a query．
    non_url_text = re.sub(r"https?://[^\s<>\"']+", " ", inspected, flags=re.IGNORECASE)
    if re.search(
        r"(?<![\w/:])/(?:[^\s/]+/[^\s]+|(?:Users|home|private|var|etc|opt|tmp|Volumes|Library|srv|mnt|media|run)(?:\b|/))",
        non_url_text,
        re.IGNORECASE,
    ):
        raise QueryPrivacyError("query rejected: local_path")
    for match in re.findall(r"https?://\S+", inspected, re.IGNORECASE):
        try:
            url = urlsplit(match)
            if url.username or url.password:
                raise QueryPrivacyError("query rejected: credential_url")
        except ValueError:
            raise QueryPrivacyError("query rejected: invalid_url") from None
    for literal in re.findall(
        r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|\[[0-9a-f:]+\]", inspected, re.IGNORECASE
    ):
        try:
            address = ipaddress.ip_address(literal.strip("[]"))
        except ValueError:
            continue
        if not address.is_global:
            raise QueryPrivacyError("query rejected: private_address")
    return normalized


def validate_query(value: str) -> str:
    return validate_public_text(value, max_chars=240)


def normalize_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        _ = parsed.port
        # Keep application parameters intact，only remove well-known tracking names．
        params = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))
    except ValueError:
        return None


class SearchProvider:
    def __init__(
        self, config: SearchConfig, budget: Budget, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.config, self.budget = config, budget
        self.base_url = config.endpoint()
        self.client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=config.timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self.last_diagnostic: dict = {}

    async def _json(self, path: str, *, data: dict | None = None) -> dict:
        for attempt in range(self.config.retries + 1):
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
                        "POST" if data else "GET", self.base_url + path, data=data
                    ) as response:
                        if response.status_code == 403:
                            raise SearchError("searxng_forbidden_json_or_access_disabled", status=403)
                        if response.status_code != 200:
                            raise SearchError("searxng_http_error", status=response.status_code)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > self.config.response_bytes:
                                raise SearchError("searxng_response_size_limit")
                        try:
                            payload = json.loads(content)
                        except (ValueError, UnicodeError):
                            raise SearchError("searxng_invalid_json") from None
                        if not isinstance(payload, dict):
                            raise SearchError("searxng_invalid_response")
                        return payload
            except (TimeoutError, httpx.TimeoutException) as exc:
                error = SearchError("searxng_timeout")
                if attempt == self.config.retries:
                    raise error from exc
            except httpx.RequestError as exc:
                if attempt == self.config.retries:
                    raise SearchError("searxng_network_error") from exc
            except SearchError as exc:
                if attempt == self.config.retries or exc.status not in {429, 502, 503, 504}:
                    raise
            await asyncio.sleep(min(0.25 * (attempt + 1), self.budget.remaining_seconds))
        raise SearchError("searxng_retry_exhausted")

    async def search(self, query: str, query_id: str) -> list[Candidate]:
        query = validate_query(query)
        payload = await self._json(
            "/search",
            data={
                "q": query,
                "format": "json",
                "engines": self.config.engine,
                "language": self.config.language,
                "safesearch": "0",
            },
        )
        unresponsive = payload.get("unresponsive_engines", [])
        failed = any(
            (entry[0] if isinstance(entry, list) and entry else entry) == self.config.engine
            for entry in unresponsive
        )
        self.last_diagnostic = {"engine": self.config.engine, "unresponsive": failed}
        if failed:
            # Never echo arbitrary upstream errors that may contain a query or credentials．
            reasons = [
                str(entry[1]).lower()
                for entry in unresponsive
                if isinstance(entry, list) and len(entry) > 1 and entry[0] == self.config.engine
            ]
            known = [
                reason
                for reason in ("captcha", "timeout", "too many requests", "access denied", "suspended")
                if any(reason in upstream for upstream in reasons)
            ]
            self.last_diagnostic["reasons"] = known or ["engine_error"]
            raise SearchError("searxng_engine_unresponsive")
        if not isinstance(payload.get("results"), list):
            raise SearchError("searxng_missing_results")
        results = []
        for rank, item in enumerate(payload["results"], 1):
            if not isinstance(item, dict):
                raise SearchError("searxng_invalid_result")
            engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
            if engines and self.config.engine not in engines:
                raise SearchError("searxng_unexpected_engine")
            url = normalize_url(str(item.get("url", "")))
            if url:
                results.append(
                    Candidate(
                        url=url,
                        title=str(item.get("title", ""))[:500],
                        snippet=str(item.get("content", ""))[:1000],
                        query_id=query_id,
                        engine=self.config.engine,
                        rank=rank,
                    )
                )
            if len(results) == self.config.max_results:
                break
        self.last_diagnostic["result_count"] = len(results)
        return results

    async def engine_health(self) -> dict:
        payload = await self._json("/config")
        engines = payload.get("engines")
        if not isinstance(engines, list):
            raise SearchError("searxng_engine_config_unavailable")
        matches = [e for e in engines if isinstance(e, dict) and e.get("name") == self.config.engine]
        if not matches:
            raise SearchError("searxng_engine_not_configured")
        engine = matches[0]
        return {
            "engine": self.config.engine,
            "configured": True,
            "enabled": engine.get("enabled", not engine.get("disabled", False)),
            "stats": {key: engine[key] for key in ("reliability", "time", "error_rate") if key in engine},
        }

    async def aclose(self) -> None:
        await self.client.aclose()
