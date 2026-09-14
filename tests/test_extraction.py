from __future__ import annotations

import asyncio
import io
import multiprocessing
import time

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from ephy_worker.extraction import extract_document, split_passages
from ephy_worker.schema import Limits


def make_pdf(pages, *, encrypted=False):
    stream = io.BytesIO()
    canvas = Canvas(stream)
    canvas.setTitle("Physical page evidence")
    canvas.setAuthor("Fixture Author")
    for text, image in pages:
        if text:
            canvas.drawString(50, 750, text)
        if image:
            fixture = Image.new("RGB", (20, 20), color="red")
            canvas.drawImage(ImageReader(fixture), 50, 600, width=100, height=100)
        canvas.showPage()
    canvas.save()
    if encrypted:
        writer = PdfWriter()
        writer.append(PdfReader(stream))
        writer.encrypt("fixture-password")
        encrypted_stream = io.BytesIO()
        writer.write(encrypted_stream)
        return encrypted_stream.getvalue()
    return stream.getvalue()


async def pdf_extract(data, **limits):
    return await extract_document(
        data, url="https://example.org/paper.pdf", kind="pdf", source_id="s9", limits=Limits(**limits)
    )


async def test_all_pdf_pages_indexed_with_physical_page_offsets():
    pages = [
        ("Early overview.", False),
        ("Prior printed page 99.", False),
        ("This result applies only to version 4 after September 2026.", False),
    ]
    result = await pdf_extract(make_pdf(pages))
    assert result["status"] == "ok"
    assert result["page_count"] == 3
    late = [p for p in result["passages"] if p["page"] == 3]
    assert "only to version 4" in late[0]["text"]
    assert late[0]["source_id"] == "s9" and late[0]["start"] == 0
    assert late[0]["end"] == len(late[0]["text"])
    assert result["page_statuses"] == {"1": "text_extracted", "2": "text_extracted", "3": "text_extracted"}
    assert result["author"] == "Fixture Author"
    assert result["metadata_evidence"]["author"].startswith("PDF metadata /Author:")
    assert "published_at" not in result


async def test_image_only_and_mixed_pages_are_explicit():
    image_result = await pdf_extract(make_pdf([("", True)]))
    assert image_result["status"] == "image_only"
    assert not image_result["passages"]
    mixed = await pdf_extract(
        make_pdf([("Extractable text.", False), ("", True), ("Text and a figure.", True)])
    )
    assert mixed["status"] == "partial"
    assert mixed["page_statuses"]["2"] == "image_only"
    assert mixed["page_statuses"]["3"] == "text_with_unread_images"
    assert mixed["limitations"]


async def test_encrypted_broken_and_empty_pdf_are_failures():
    encrypted = await pdf_extract(make_pdf([("Private text.", False)], encrypted=True))
    assert encrypted["status"] == "encrypted"
    broken = await pdf_extract(b"%PDF-1.7\nbroken")
    assert broken["status"] == "invalid"
    empty = await pdf_extract(make_pdf([("", False)]))
    assert empty["status"] == "extraction_failed"
    assert empty["page_statuses"]["1"] == "no_extractable_text"


async def test_page_and_text_limits_are_partial_not_full_read():
    limited_pages = await pdf_extract(make_pdf([("page one", False), ("page two", False)]), pdf_pages=1)
    assert limited_pages["status"] == "partial"
    assert limited_pages["page_count"] == 2
    assert limited_pages["page_statuses"]["2-2"] == "not_extracted_page_limit"
    limited_text = await pdf_extract(
        make_pdf([("A" * 1100, False), ("Late omitted condition", False)]), text_chars=1000
    )
    assert limited_text["status"] == "partial"
    assert limited_text["page_statuses"]["1"] == "text_limit"
    assert limited_text["page_statuses"]["2"] == "not_extracted_text_limit"
    assert sum(len(p["text"]) for p in limited_text["passages"]) == 1000


async def test_html_metadata_explicit_origin_and_duplicate_hash():
    html = b"""<html><head><title>Original publication</title><meta name="author" content="Test Author">
    <meta property="og:site_name" content="Fixture Publisher"><meta property="article:published_time" content="2026-09-01">
    <link rel="canonical" href="https://unrelated.example/canonical"></head><body><article>
    <h1>Research evidence</h1><p>Version four has a documented limitation after September 2026.
    This observation comes from a public document and must retain the original condition.</p>
    <p><a href="https://primary.example/paper">Original paper</a></p>
    <p><a href="https://other.example/background">Background reading</a></p></article></body></html>"""
    result = await extract_document(
        html, url="https://example.org/article", kind="html", source_id="s1", limits=Limits()
    )
    duplicate = await extract_document(
        html, url="https://copy.example/article", kind="html", source_id="s2", limits=Limits()
    )
    assert result["status"] == "ok"
    assert result["body_hash"] == duplicate["body_hash"]
    assert result["origin_urls"] == ["https://primary.example/paper"]
    assert result["author"] == "Test Author" and result["publisher"] == "Fixture Publisher"
    assert result["published_at"] == "2026-09-01"
    assert "article:published_time" in result["metadata_evidence"]["published_at"]


async def test_empty_html_fails_and_html_limit_marks_partial():
    empty = await extract_document(
        b"<html><head></head><body></body></html>",
        url="https://example.org",
        kind="html",
        source_id="s1",
        limits=Limits(),
    )
    assert empty["status"] == "extraction_failed"
    html = (
        "<html><body><article><p>"
        + "A documented condition remains unverified. " * 100
        + "</p></article></body></html>"
    ).encode()
    limited = await extract_document(
        html, url="https://example.org", kind="html", source_id="s1", limits=Limits(text_chars=1000)
    )
    assert limited["status"] == "partial"
    assert sum(len(p["text"]) for p in limited["passages"]) == 1000


def test_passages_cover_full_body_and_keep_offsets():
    text = "first paragraph\n" * 100 + "condition on the final page"
    parts = split_passages(text, "s1", page=9, size=200)
    assert "".join(p["text"] for p in parts) == text
    assert all(len(p["text"]) <= 200 and p["text"] == text[p["start"] : p["end"]] for p in parts)
    assert "condition on the final page" in parts[-1]["text"]


def sleeping_parser(connection, *args):
    time.sleep(60)


def allocating_parser(connection, *args):
    payload = bytearray(200 * 1024**2)
    time.sleep(60)
    connection.send(len(payload))


async def test_parser_timeout_kills_and_reaps_child():
    before = {p.pid for p in multiprocessing.active_children()}
    result = await extract_document(
        b"",
        url="https://example.org",
        kind="pdf",
        source_id="s1",
        limits=Limits(parser_seconds=0.1),
        _worker=sleeping_parser,
    )
    assert result == {"status": "timeout", "error": "parser_timeout"}
    assert {p.pid for p in multiprocessing.active_children()} == before


async def test_parser_memory_cap_is_enforced_by_parent():
    result = await extract_document(
        b"",
        url="https://example.org",
        kind="pdf",
        source_id="s1",
        limits=Limits(parser_seconds=10, parser_memory_mb=128),
        _worker=allocating_parser,
    )
    assert result == {"status": "limit", "error": "parser_memory_mb"}


async def test_parser_cancellation_reaps_child():
    before = {p.pid for p in multiprocessing.active_children()}
    task = asyncio.create_task(
        extract_document(
            b"",
            url="https://example.org",
            kind="pdf",
            source_id="s1",
            limits=Limits(),
            _worker=sleeping_parser,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {p.pid for p in multiprocessing.active_children()} == before
