"""Bounded，offline HTML and physical-page PDF extraction．

Parsers run in a disposable process．No parser fetches links or runs page instructions．
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import multiprocessing
import re
import sys
import time
from importlib.metadata import version
from urllib.parse import urljoin, urlsplit

import psutil

from .schema import Limits, Passage


def split_passages(text: str, source_id: str, *, page: int | None, size: int) -> list[dict]:
    """Split all retained text，preserving exact quote offsets and page boundaries．"""
    result = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary < 0:
                boundary = text.rfind(" ", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        chunk = text[start:end]
        if chunk.strip():
            location = f"p{page}" if page is not None else "html"
            result.append(
                Passage(
                    passage_id=f"{source_id}-{location}-{start}",
                    source_id=source_id,
                    text=chunk,
                    page=page,
                    start=start,
                    end=end,
                ).model_dump()
            )
        start = end
    return result


def _public_link(base: str, value: str) -> str | None:
    """Only preserve provenance hints，never fetch them from the parser．"""
    try:
        value = urljoin(base, value.strip())
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
            return None
        if parsed.password is not None or any(ord(c) < 32 for c in value):
            return None
        return value[:2048]
    except ValueError:
        return None


def _html_metadata(data: bytes, url: str) -> dict:
    from lxml import html

    root = html.fromstring(data)
    result: dict = {"metadata_evidence": {}, "origin_urls": []}
    mapping = {
        "author": ("author", "citation_author"),
        "publisher": ("og:site_name", "publisher", "citation_publisher"),
        "published_at": ("article:published_time", "citation_publication_date", "datepublished"),
    }
    metas = {}
    for node in root.xpath("//meta[@content]"):
        key = (node.get("property") or node.get("name") or node.get("itemprop") or "").lower()
        metas.setdefault(key, node.get("content", "").strip()[:1000])
    for field, keys in mapping.items():
        for key in keys:
            if metas.get(key):
                result[field] = metas[key]
                result["metadata_evidence"][field] = f"HTML meta {key}: {metas[key]}"
                break
    if metas.get("article:modified_time"):
        result["metadata_evidence"]["modified_at"] = (
            "HTML meta article:modified_time: " + metas["article:modified_time"]
        )
    for key in ("citation_pdf_url", "citation_fulltext_html_url", "original-source"):
        if metas.get(key) and (link := _public_link(url, metas[key])):
            result["origin_urls"].append(link)
            result["metadata_evidence"][f"origin:{link}"] = f"HTML meta {key}: {metas[key]}"
    # Generic hyperlinks，canonical URLs，and citations alone do not prove shared origin．
    # Only explicit article-level attribution labels become origin hints．
    attribution = re.compile(
        r"^(?:original (?:article|paper|publication|source)|"
        r"reprinted from|republished from|転載元|原論文|原記事|原資料)(?:\s*[:：].*)?$",
        re.IGNORECASE,
    )
    for node in root.xpath("//a[@href]"):
        label = " ".join(node.text_content().split())[:200]
        if attribution.fullmatch(label) and (link := _public_link(url, node.get("href", ""))):
            result["origin_urls"].append(link)
            result["metadata_evidence"][f"origin:{link}"] = f"Explicit attribution link: {label}"
    result["origin_urls"] = list(dict.fromkeys(result["origin_urls"]))[:20]
    result["metadata_evidence"] = dict(list(result["metadata_evidence"].items())[:30])
    return result


def _parse_html(data: bytes, url: str, source_id: str, limits: Limits) -> dict:
    import trafilatura

    document = trafilatura.bare_extraction(
        data,
        url=url,
        include_comments=False,
        include_tables=True,
        with_metadata=True,
        date_extraction_params={"extensive_search": False},
    )
    extractor = f"trafilatura/{version('trafilatura')}"
    if document is None or not document.text or not document.text.strip():
        return {"status": "extraction_failed", "error": "html_body_unavailable", "extractor": extractor}
    text = document.text.strip()
    truncated = len(text) > limits.text_chars
    text = text[: limits.text_chars]
    metadata = _html_metadata(data, url)
    return {
        **metadata,
        "title": (document.title or "")[:2000],
        "status": "partial" if truncated else "ok",
        "error": "text_chars" if truncated else None,
        "extractor": extractor,
        "body_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "passages": split_passages(text, source_id, page=None, size=limits.passage_chars),
        "limitations": ["text_chars上限によりHTML本文を途中で打ち切った．"] if truncated else [],
    }


def _has_images(page) -> bool:
    """Inspect image references without decoding potentially huge image payloads．"""
    seen: set[int] = set()

    def walk(resources, depth: int = 0) -> bool:
        if resources is None or depth > 20:
            return False
        resources = resources.get_object()
        if id(resources) in seen:
            return False
        seen.add(id(resources))
        xobjects = resources.get("/XObject", {})
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for reference in xobjects.values():
            obj = reference.get_object()
            if obj.get("/Subtype") == "/Image":
                return True
            if obj.get("/Subtype") == "/Form" and walk(obj.get("/Resources"), depth + 1):
                return True
        return False

    if walk(page.get("/Resources")):
        return True
    # Inline images are present directly inside the content stream．
    contents = page.get_contents()
    return bool(contents and re.search(rb"(?:^|\s)BI\s", contents.get_data()))


def _parse_pdf(data: bytes, url: str, source_id: str, limits: Limits) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    extractor = f"pypdf/{version('pypdf')}"
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return {
                "status": "encrypted",
                "error": "encrypted_pdf",
                "extractor": extractor,
                "limitations": ["暗号化PDFは本文を抽出していない．"],
            }
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError):
        return {"status": "invalid", "error": "invalid_pdf", "extractor": extractor}
    metadata = reader.metadata or {}
    result: dict = {
        "status": "ok",
        "extractor": extractor,
        "passages": [],
        "page_count": page_count,
        "page_statuses": {},
        "limitations": [],
        "metadata_evidence": {},
    }
    for key, field in (("/Title", "title"), ("/Author", "author")):
        if value := metadata.get(key):
            result[field] = str(value)[:2000]
            result["metadata_evidence"][field] = f"PDF metadata {key}: {str(value)[:2000]}"
    # PDF creation time is document metadata，not a verified publication date．
    if value := metadata.get("/CreationDate"):
        result["metadata_evidence"]["created_at"] = f"PDF metadata /CreationDate: {str(value)[:1000]}"
    pieces: list[str] = []
    retained = 0
    for index in range(min(page_count, limits.pdf_pages)):
        number = index + 1
        if retained >= limits.text_chars:
            result["page_statuses"][str(number)] = "not_extracted_text_limit"
            continue
        try:
            page = reader.pages[index]
            images = _has_images(page)
            text = (page.extract_text() or "").strip()
            if not text:
                result["page_statuses"][str(number)] = "image_only" if images else "no_extractable_text"
                continue
            remaining = limits.text_chars - retained
            truncated = len(text) > remaining
            text = text[:remaining]
            retained += len(text)
            pieces.append(f"\n[physical-page:{number}]\n{text}")
            result["passages"].extend(split_passages(text, source_id, page=number, size=limits.passage_chars))
            result["page_statuses"][str(number)] = (
                "text_limit" if truncated else "text_with_unread_images" if images else "text_extracted"
            )
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolate arbitrary failures of one untrusted PDF page．
            # Failure class is enough for diagnostics；never include arbitrary parser payloads．
            result["page_statuses"][str(number)] = f"extraction_failed:{type(exc).__name__}"
    if page_count > limits.pdf_pages:
        result["limitations"].append(f"pdf_pages上限により物理ページ{limits.pdf_pages + 1}以降は未抽出．")
        # Bound metadata as well as text，even when a malformed PDF declares many pages．
        result["page_statuses"][f"{limits.pdf_pages + 1}-{page_count}"] = "not_extracted_page_limit"
    states = set(result["page_statuses"].values())
    if any("image" in state for state in states):
        result["limitations"].append("画像の内容は未読であり，OCR・図表の画像理解は実施していない．")
    if any("text_limit" in state for state in states):
        result["limitations"].append("text_chars上限により本文を途中で打ち切った．")
    if any(state.startswith("extraction_failed") or state == "no_extractable_text" for state in states):
        result["limitations"].append("本文を抽出できない物理ページがある．")
    if result["passages"]:
        result["status"] = "partial" if states - {"text_extracted"} else "ok"
        result["body_hash"] = hashlib.sha256("".join(pieces).encode("utf-8")).hexdigest()
    else:
        result["status"] = "image_only" if states == {"image_only"} else "extraction_failed"
        result["error"] = "pdf_text_unavailable"
    return result


def _parse_child(connection, data: bytes, url: str, kind: str, source_id: str, config: dict) -> None:
    try:
        limits = Limits.model_validate(config)
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(limits.parser_seconds) + 1,) * 2)
            # macOS virtual address reservations are unrelated to resident consumption．
            # RSS is independently enforced by the parent on every supported OS．
            if sys.platform.startswith("linux"):
                ceiling = limits.parser_memory_mb * 1024**2
                resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        result = (_parse_pdf if kind == "pdf" else _parse_html)(data, url, source_id, limits)
        connection.send(result)
    except MemoryError:
        connection.send({"status": "limit", "error": "parser_memory_mb"})
    except Exception as exc:  # noqa: BLE001 — process boundary returns structured parser failures．
        connection.send({"status": "extraction_failed", "error": f"parser:{type(exc).__name__}"})
    finally:
        connection.close()


async def extract_document(
    data: bytes,
    *,
    url: str,
    kind: str,
    source_id: str,
    limits: Limits,
    seconds: float | None = None,
    _worker=None,
) -> dict:
    """Stop and reap the parser on timeout，memory excess，or caller cancellation．"""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker or _parse_child,
        args=(sender, data, url, kind, source_id, limits.model_dump()),
        daemon=True,
    )
    started = time.monotonic()
    deadline = min(limits.parser_seconds, seconds if seconds is not None else limits.parser_seconds)
    try:
        process.start()
        sender.close()
        watched = psutil.Process(process.pid)
        while True:
            if time.monotonic() - started >= deadline:
                return {"status": "timeout", "error": "parser_timeout"}
            try:
                # Parsers are pure Python/C library calls，with no child processes．
                # Inspect only our child PID；global process enumeration is denied in some sandboxes．
                rss = watched.memory_info().rss
                if rss > limits.parser_memory_mb * 1024**2:
                    return {"status": "limit", "error": "parser_memory_mb"}
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, PermissionError):
                return {"status": "limit", "error": "parser_resource_monitor_unavailable"}
            if receiver.poll():
                try:
                    return receiver.recv()
                except EOFError:
                    return {"status": "extraction_failed", "error": "parser_exited_without_result"}
            if not process.is_alive():
                return {"status": "extraction_failed", "error": "parser_exited_without_result"}
            await asyncio.sleep(0.02)
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            process.close()
