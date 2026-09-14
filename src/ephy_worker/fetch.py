"""Public HTTP(S) only，with checked DNS pinned to each actual connection．"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .budget import Budget, BudgetExceeded
from .extraction import extract_document
from .schema import Candidate, Source


class UnsafeURL(ValueError):
    pass


class DocumentLimit(RuntimeError):
    pass


class InvalidEncoding(ValueError):
    pass


class FetchResponse(Protocol):
    status: int
    headers: dict

    def iter_bytes(self) -> AsyncIterator[bytes]: ...


class FetchTransport(Protocol):
    def request(self, url: str, addresses: Sequence[str], timeout: float): ...
    async def aclose(self) -> None: ...


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise UnsafeURL("dns_non_ip_result") from exc
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or "%" in value
    ):
        raise UnsafeURL("non_public_address")
    # Block transition tunnels and mapped addresses，including embedded private IPv4．
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped
        or address.sixtofour
        or address.teredo
        or address in ipaddress.ip_network("64:ff9b::/96")
        or address in ipaddress.ip_network("64:ff9b:1::/48")
    ):
        raise UnsafeURL("transition_address")
    return str(address)


def validate_public_url(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise UnsafeURL("invalid_url")
    if "\\" in url:
        raise UnsafeURL("ambiguous_url")
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise UnsafeURL("non_http_url")
        if parts.username is not None or parts.password is not None:
            raise UnsafeURL("url_credentials")
        host = parts.hostname.lower().rstrip(".")
        if "%" in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise UnsafeURL("non_public_hostname")
        host = host.encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not (1 <= port <= 65535):
            raise UnsafeURL("invalid_port")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*", host):
                raise UnsafeURL("ambiguous_numeric_address")
        else:
            _public_address(host)
        return urldefrag(url)[0], host, port
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, UnsafeURL):
            raise
        raise UnsafeURL("invalid_url") from exc


async def resolve_public(host: str, port: int) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class _PinnedResolver(AbstractResolver):
    def __init__(self, host: str, port: int, addresses: Sequence[str]):
        self.host, self.port = host, port
        self.addresses = tuple(_public_address(value) for value in addresses)

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        if host.lower().rstrip(".") != self.host or port != self.port:
            raise UnsafeURL("unpinned_dns_request")
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for ip in self.addresses
            if family in (socket.AF_UNSPEC, socket.AF_INET6 if ":" in ip else socket.AF_INET)
        ]

    async def close(self) -> None:
        return None


class _AioResponse:
    def __init__(self, response: aiohttp.ClientResponse):
        self.status = response.status
        self.headers = {key.lower(): value for key, value in response.headers.items()}
        self.response = response

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.content.iter_chunked(64 * 1024):
            yield chunk


class AioHTTPTransport:
    """Fresh connection per redirect；no credentials，proxy，cookie jar，or hidden retry．"""

    @asynccontextmanager
    async def request(self, url: str, addresses: Sequence[str], timeout: float):
        _, host, port = validate_public_url(url)
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(host, port, addresses),
            use_dns_cache=False,
            force_close=True,
            limit=1,
            limit_per_host=1,
            ssl=True,
        )
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={
                "User-Agent": "EphyWorker/0.1 public-research",
                "Accept": "text/html,application/pdf;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
        ) as session:
            # aiohttp 3.x can repeat a GET after a disconnect even with a fresh session．
            # Disable that internal retry so every network attempt consumes the shared budget．
            session._retry_connection = False
            async with session.get(url, allow_redirects=False, ssl=True) as response:
                yield _AioResponse(response)

    async def aclose(self) -> None:
        return None


async def _decoded_chunks(response: FetchResponse, max_wire_bytes: int) -> AsyncIterator[bytes]:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "", "gzip", "deflate"}:
        raise InvalidEncoding("unsupported_content_encoding")
    decoder = (
        zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        if encoding in {"gzip", "deflate"}
        else None
    )
    wire_bytes = 0
    async for chunk in response.iter_bytes():
        wire_bytes += len(chunk)
        if wire_bytes > max_wire_bytes:
            raise DocumentLimit("wire_bytes")
        if decoder is None:
            yield chunk
            continue
        try:
            pending = chunk
            while pending:
                output = decoder.decompress(pending, 64 * 1024)
                pending = decoder.unconsumed_tail
                if output:
                    yield output
                if decoder.unused_data:
                    # Concatenated streams are not silently accepted as a truncated document．
                    raise InvalidEncoding("multiple_compressed_streams")
        except zlib.error as exc:
            raise InvalidEncoding("invalid_compressed_body") from exc
    if decoder is not None and not decoder.eof:
        raise InvalidEncoding("incomplete_compressed_body")


def identify_document(media_type: str, prefix: bytes) -> tuple[str, str | None]:
    stripped = prefix.lstrip(b"\xef\xbb\xbf\x00\t\n\r ")
    if stripped.startswith(b"%PDF-"):
        return "pdf", None if media_type == "application/pdf" else "PDF magicとContent-Typeが一致しない．"
    html_like = bool(
        re.search(
            rb"<(?:!doctype\s+html|html|head|body|article|main|p)(?:\s|>)", stripped[:1024], re.IGNORECASE
        )
    )
    if html_like or media_type in {"text/html", "application/xhtml+xml"}:
        return "html", "PDFではなくHTML応答を抽出した．" if media_type == "application/pdf" else None
    if media_type == "application/pdf":
        return "invalid", "pdf_magic_missing"
    return "unknown", "unsupported_media_type"


class PublicFetcher:
    def __init__(
        self, budget: Budget, *, transport: FetchTransport | None = None, resolver: Resolver | None = None
    ):
        self.budget = budget
        self.transport = transport or AioHTTPTransport()
        self.resolver = resolver or resolve_public
        self._all = asyncio.Semaphore(3)
        self._hosts: dict[str, asyncio.Semaphore] = {}

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def fetch(self, candidate: Candidate, source_id: str) -> Source:
        source = Source(
            source_id=source_id,
            requested_url=candidate.url,
            title=candidate.title,
            supplied=candidate.supplied,
            query_ids=[candidate.query_id] if candidate.query_id else [],
            engines=[candidate.engine] if candidate.engine else [],
        )
        async with self._all:
            try:
                # One outer deadline includes DNS，every redirect，body reads，and extraction．
                async with asyncio.timeout(self.budget.remaining_seconds):
                    return await self._fetch(candidate.url, source)
            except UnsafeURL as exc:
                source.status, source.error = "blocked", str(exc)
            except BudgetExceeded as exc:
                source.status, source.error = "limit", str(exc)
            except DocumentLimit as exc:
                source.status, source.error = "limit", str(exc)
            except InvalidEncoding as exc:
                source.status, source.error = "invalid", str(exc)
            except TimeoutError:
                source.status, source.error = "timeout", "fetch_timeout"
            except (aiohttp.ClientError, OSError) as exc:
                source.status, source.error = "network_error", type(exc).__name__
        return source

    async def _fetch(self, url: str, source: Source) -> Source:
        limits = self.budget.limits
        for redirects in range(limits.max_redirects + 1):
            url, host, port = validate_public_url(url)
            timeout = min(limits.request_seconds, self.budget.remaining_seconds)
            async with asyncio.timeout(timeout):
                addresses = tuple(_public_address(value) for value in await self.resolver(host, port))
                if not addresses:
                    raise UnsafeURL("dns_empty_result")
                # No fallback DNS resolution occurs later；transport receives these exact IPs．
                lock = self._hosts.setdefault(host, asyncio.Semaphore(1))
                async with lock:
                    self.budget.take("fetch_requests")
                    async with self.transport.request(url, addresses, timeout) as response:
                        source.final_url, source.http_status = url, response.status
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                source.status, source.error = "http_error", "redirect_without_location"
                                return source
                            if redirects >= limits.max_redirects:
                                raise DocumentLimit("max_redirects")
                            url = urljoin(url, location)
                            continue
                        if not 200 <= response.status < 300:
                            source.status, source.error = "http_error", f"http_{response.status}"
                            return source
                        source.media_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        )
                        body = bytearray()
                        async for chunk in _decoded_chunks(
                            response, max(limits.html_bytes, limits.pdf_bytes)
                        ):
                            source.bytes_received += len(chunk)
                            self.budget.take("cache_bytes", len(chunk))
                            body.extend(chunk)
                            kind, note = identify_document(source.media_type, body[:1024])
                            limit = limits.pdf_bytes if kind == "pdf" else limits.html_bytes
                            if len(body) > limit:
                                raise DocumentLimit("pdf_bytes" if kind == "pdf" else "html_bytes")
                        kind, note = identify_document(source.media_type, body[:1024])
                        if kind in {"invalid", "unknown"}:
                            source.status = "invalid" if kind == "invalid" else "unsupported"
                            source.error = note
                            return source
                        source.kind = kind
                        if note:
                            source.limitations.append(note)
            parsed = await extract_document(
                bytes(body),
                url=url,
                kind=kind,
                source_id=source.source_id,
                limits=limits,
                seconds=self.budget.remaining_seconds,
            )
            text_bytes = sum(len(p["text"].encode("utf-8")) for p in parsed.get("passages", []))
            try:
                self.budget.take("cache_bytes", text_bytes)
            except BudgetExceeded:
                # Preserve fetched provenance while refusing to retain unbudgeted parser output．
                source.status, source.error = "limit", "cache_bytes"
                return source
            existing_notes = source.limitations
            source = Source.model_validate({**source.model_dump(), **parsed})
            source.limitations = existing_notes + source.limitations
            return source
        raise DocumentLimit("max_redirects")
