"""Bounded，explicit direct search through DuckDuckGo's public HTML interface．"""

from __future__ import annotations

import asyncio
import ipaddress
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from .budget import Budget
from .config import SearchConfig
from .schema import Candidate
from .search import SearchError, normalize_url, validate_query


def _public_result_url(value: str) -> str | None:
    """Unwrap only a trusted DDG redirect；never visit the wrapper or result here．"""
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        if (
            parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com", "www.duckduckgo.com"}
            and parsed.path == "/l/" and not parsed.username and not parsed.password
        ):
            targets = parse_qs(parsed.query).get("uddg", [])
            if len(targets) != 1:
                return None
            value = targets[0]
        url = normalize_url(value)
        if not url:
            return None
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if (
            "." not in host
            or host.endswith((".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".test"))
            or "%" in host
            or "\\" in value
            or any(ord(char) < 33 for char in value)
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            # Reject alternate numeric IPv4 spellings accepted by some resolvers．
            if all(part.isdigit() or part.lower().startswith("0x") for part in host.split(".")):
                return None
        return url
    except ValueError:
        return None


class _ResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.result_depth: int | None = None
        self.capture: tuple[str, int] | None = None
        self.current: dict[str, str] = {}
        self.results: list[dict[str, str]] = []
        self.empty = False
        self.challenge = False
        self.malformed = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if (
            "anomaly" in (attributes.get("id") or "").lower()
            or "anomaly.js" in (attributes.get("action") or "").lower()
            or "anomaly-modal" in classes
        ):
            self.challenge = True
        if "no-results" in classes:
            self.empty = True
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        self.stack.append(tag)
        depth = len(self.stack)
        if "result" in classes and "result--no-result" not in classes:
            if self.result_depth is not None:
                self.malformed = True
            self.result_depth = depth
            self.current = {}
        if self.result_depth is not None:
            if "result__a" in classes and tag == "a":
                if "url" in self.current:
                    self.malformed = True
                self.current["url"] = attributes.get("href") or ""
                self.capture = ("title", depth)
            elif "result__snippet" in classes:
                self.capture = ("snippet", depth)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack)
        if self.stack[-1] != tag and self.result_depth is not None:
            self.malformed = True
        closing = len(self.stack) - self.stack[::-1].index(tag)
        if self.capture and closing <= self.capture[1]:
            self.capture = None
        if self.result_depth is not None and closing <= self.result_depth <= depth:
            self.results.append(self.current)
            self.result_depth = None
        del self.stack[closing - 1:]

    def handle_data(self, data):
        if "unfortunately, bots use duckduckgo too" in data.lower():
            self.challenge = True
        if self.capture:
            key = self.capture[0]
            self.current[key] = self.current.get(key, "") + data


class DuckDuckGoHTMLProvider:
    def __init__(self, config: SearchConfig, budget: Budget, *, transport=None):
        self.config, self.budget = config, budget
        self.base_url = config.endpoint()
        self.client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=config.timeout_seconds,
            headers={"Accept": "text/html", "User-Agent": "ephy-worker/0.1"},
        )
        self.last_diagnostic: dict = {}

    @property
    def metadata(self) -> dict:
        return {"provider": "duckduckgo_html", "engine": "duckduckgo", "interface": "html"}

    async def _html(self, query: str) -> str:
        for attempt in range(self.config.retries + 1):
            self.budget.take("search_requests")
            try:
                async with asyncio.timeout(min(
                    self.config.timeout_seconds,
                    self.budget.limits.request_seconds,
                    self.budget.remaining_seconds,
                )):
                    async with self.client.stream("GET", self.base_url, params={"q": query}) as response:
                        if response.status_code == 202:
                            raise SearchError("duckduckgo_challenge", status=202)
                        if response.status_code == 429:
                            raise SearchError("duckduckgo_rate_limited", status=429)
                        if 300 <= response.status_code < 400:
                            raise SearchError("duckduckgo_redirect_refused", status=response.status_code)
                        if response.status_code != 200:
                            raise SearchError("duckduckgo_http_error", status=response.status_code)
                        if "text/html" not in response.headers.get("content-type", "").lower():
                            raise SearchError("duckduckgo_invalid_content_type")
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > self.config.response_bytes:
                                raise SearchError("duckduckgo_response_size_limit")
                            content.extend(chunk)
                        try:
                            return content.decode("utf-8")
                        except UnicodeError:
                            raise SearchError("duckduckgo_invalid_html") from None
            except (TimeoutError, httpx.TimeoutException) as exc:
                if attempt == self.config.retries:
                    raise SearchError("duckduckgo_timeout") from exc
            except httpx.RequestError as exc:
                if attempt == self.config.retries:
                    raise SearchError("duckduckgo_network_error") from exc
            except SearchError as exc:
                if attempt == self.config.retries or exc.status not in {502, 503, 504}:
                    raise
            await asyncio.sleep(min(0.25 * (attempt + 1), self.budget.remaining_seconds))
        raise SearchError("duckduckgo_retry_exhausted")

    async def search(self, query: str, query_id: str) -> list[Candidate]:
        query = validate_query(query)
        self.last_diagnostic = {"interface": "html"}
        parser = _ResultsParser()
        parser.feed(await self._html(query))
        parser.close()
        if parser.challenge:
            raise SearchError("duckduckgo_challenge")
        if parser.malformed or parser.result_depth is not None:
            raise SearchError("duckduckgo_invalid_html")
        if not parser.results:
            if parser.empty:
                self.last_diagnostic["result_count"] = 0
                return []
            raise SearchError("duckduckgo_unrecognized_html")
        results = []
        for rank, item in enumerate(parser.results, 1):
            url = _public_result_url(item.get("url", ""))
            title = " ".join(item.get("title", "").split())
            if not url or not title:
                raise SearchError("duckduckgo_invalid_result")
            results.append(Candidate(
                url=url, title=title[:500], snippet=" ".join(item.get("snippet", "").split())[:1000],
                query_id=query_id, engine="duckduckgo", rank=rank,
            ))
            if len(results) == self.config.max_results:
                break
        self.last_diagnostic["result_count"] = len(results)
        return results

    async def engine_health(self) -> dict:
        return {
            **self.metadata,
            "configured": True,
            "enabled": True,
            "network_probe": False,
            "availability": "not_probed",
        }

    async def aclose(self) -> None:
        await self.client.aclose()
