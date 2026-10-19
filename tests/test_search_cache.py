from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from ephy_worker.budget import Budget
from ephy_worker.config import SearchConfig, SearchRequestPolicy
from ephy_worker.schema import Candidate, Limits
from ephy_worker.search import QueryPrivacyError, SearchError, create_search_provider
from ephy_worker.search_cache import SQLiteSearchCache, decode_candidates


def cache(tmp_path, **kwargs):
    return SQLiteSearchCache(SearchRequestPolicy(sqlite_path=str(tmp_path / "cache.sqlite3"), **kwargs))


def data():
    return json.dumps([Candidate(url="https://example.com/doc", title="資料", snippet="text", engine="brave",
                                 query_id="original", rank=1).model_dump()], ensure_ascii=False).encode()


def test_round_trip_restart_cooldown_and_provenance(tmp_path):
    first = cache(tmp_path)
    payload = data()
    first.put("query-hash", payload)
    first.cool("backend-hash", 600, "rate_limited")
    second = cache(tmp_path)
    blob, cooldown = second.lookup("query-hash", "backend-hash")
    a, b = decode_candidates(blob, "new"), decode_candidates(payload, "old")
    assert a[0].query_id == "new" and a[0].discovered_at == b[0].discovered_at
    assert 599 < cooldown[0] <= 600
    second.cool("backend-hash", 1, "rate_limited")
    assert second.lookup("query-hash", "backend-hash")[1][0] > 599


def test_ttl_byte_entry_caps(tmp_path):
    first = cache(tmp_path, cache_max_entries=1, cache_max_bytes=1024, cache_ttl_seconds=30)
    first.put("a", data())
    first.put("b", data())
    assert first.lookup("a", "x")[0] is None and first.lookup("b", "x")[0]
    with sqlite3.connect(first.path) as db:
        db.execute("UPDATE cache SET expires=expires-60,created=created-60")
    db.close()
    assert first.lookup("b", "x")[0] is None
    first.put("too-big", b"x" * 1025)
    assert first.lookup("too-big", "x")[0] is None


def test_corrupt_schema_locked_db_fail_explicitly(tmp_path):
    first = cache(tmp_path)
    first.path.write_bytes(b"corrupt")
    with pytest.raises(SearchError, match="storage_error"):
        first.lookup("a", "b")
    first.path.unlink()
    with sqlite3.connect(first.path) as db:
        db.execute("CREATE TABLE wrong(x TEXT)")
    db.close()
    with pytest.raises(SearchError, match="schema_error"):
        first.lookup("a", "b")
    first.path.unlink()
    first.put("a", data())
    with sqlite3.connect(first.path) as db:
        db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(SearchError, match="storage_error"):
            first.lookup("a", "b")
    db.close()


def test_unsafe_path_and_invalid_candidate(tmp_path):
    with pytest.raises(SearchError, match="unsafe_path"):
        SQLiteSearchCache(SearchRequestPolicy(sqlite_path="relative.sqlite3"))
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").write_text("gitdir: somewhere")
    with pytest.raises(SearchError, match="unsafe_path"):
        SQLiteSearchCache(SearchRequestPolicy(sqlite_path=str(root / "cache.db")))
    for value in [b'{}', b'[{"url":"http://127.0.0.1","rank":1}]', b'[[[[[[[']:
        with pytest.raises(SearchError, match="invalid_data"):
            decode_candidates(value, "q")


def test_db_resources_closed_after_operations(tmp_path):
    first = cache(tmp_path)
    first.put("a", data())
    first.lookup("a", "b")
    moved = first.path.with_suffix(".moved")
    first.path.rename(moved)
    assert moved.exists()


def test_restart_policy_cache_cooldown_across_event_loops(tmp_path):
    path, discovered = str(tmp_path / "cache.db"), []

    async def run(first):
        def handler(req):
            if not first:
                pytest.fail("disk state must avoid HTTP")
            if b"blocked" in req.content:
                return httpx.Response(429, headers={"retry-after": "600"})
            return httpx.Response(200, json={"results": [{"url": "https://example.com", "title": "title",
                                                         "content": "content"}]})
        provider = create_search_provider(SearchConfig(provider="tavily_keyless", request_policy={
            "sqlite_path": path, "min_interval_seconds": 0}), Budget(Limits()), transport=httpx.MockTransport(handler))
        try:
            candidates = await provider.search("cached public query", "first" if first else "second")
            discovered.append(candidates[0].discovered_at)
            assert candidates[0].query_id == ("first" if first else "second")
            with pytest.raises(SearchError, match="rate_limited" if first else "cooldown"):
                await provider.search("blocked public query", "q")
            assert await provider.search("cached public query", "cached-during-cooldown")
            with pytest.raises(QueryPrivacyError):
                await provider.search("api_key=secret", "q")
            assert provider.budget.counts["search_requests"] == (2 if first else 0)
        finally:
            await provider.aclose()
    asyncio.run(run(True))
    asyncio.run(run(False))
    assert discovered[0] == discovered[1]


def test_schema_missing_primary_key_rejected(tmp_path):
    first = cache(tmp_path)
    with sqlite3.connect(first.path) as db:
        db.execute("CREATE TABLE cache(key TEXT,created REAL NOT NULL,expires REAL NOT NULL,data BLOB NOT NULL)")
        db.execute("CREATE TABLE cooldown(backend TEXT PRIMARY KEY,expires REAL NOT NULL,reason TEXT NOT NULL)")
        db.execute("PRAGMA user_version=1")
    db.close()
    with pytest.raises(SearchError, match="schema_error"):
        first.lookup("a", "b")


def test_cache_ddgs_backend_region_version_partitions(tmp_path):
    from ephy_worker.ddgs_search import DDGSSearchProvider
    from ephy_worker.search_policy import PolicySearchProvider
    identities = []
    for backend, region in [("brave", "jp-jp"), ("brave", "us-en"), ("google", "jp-jp")]:
        provider = DDGSSearchProvider(SearchConfig(provider="ddgs", backend=backend, region=region,
                                                  request_policy={}), Budget(Limits()))
        wrapped = PolicySearchProvider(provider)
        identities.append((wrapped._backend, wrapped._semantic))
    assert len(set(identities)) == 3
    assert identities[0][0] == identities[1][0]


async def test_region_changes_cache_semantics_but_never_bypasses_cooldown():
    from ephy_worker.ddgs_search import DDGSSearchProvider
    from ephy_worker.search_policy import PolicySearchProvider
    searches, calls = [], []
    for region in ["jp-jp", "us-en"]:
        raw = DDGSSearchProvider(SearchConfig(provider="ddgs", backend="brave", region=region,
                                             request_policy={"min_interval_seconds": 0}), Budget(Limits()))

        async def search(query, query_id, raw=raw):
            calls.append(raw.config.region)
            raw.budget.take("search_requests")
            if query == "blocked query":
                raise SearchError("ddgs_rate_limited", status=429, retry_after=600)
            return [Candidate(url="https://example.com", title="title", engine="brave", rank=1, query_id=query_id)]

        raw.search = search
        searches.append(PolicySearchProvider(raw))
    try:
        for search in searches:
            await search.search("public query", "q")
        assert calls == ["jp-jp", "us-en"]
        with pytest.raises(SearchError, match="rate_limited"):
            await searches[0].search("blocked query", "q")
        with pytest.raises(SearchError, match="cooldown"):
            await searches[1].search("different query", "q")
        assert len(calls) == 3
        assert await searches[1].search("public query", "cache-during-cooldown")
    finally:
        for search in searches:
            await search.aclose()


def test_physical_page_limit_uses_actual_page_size(tmp_path):
    first = cache(tmp_path, cache_max_bytes=1024)
    with sqlite3.connect(first.path) as db:
        db.execute("PRAGMA page_size=8192")
        db.execute("VACUUM")
    db.close()
    first.put("hash", data())
    with first._db() as db:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        maximum = db.execute("PRAGMA max_page_count").fetchone()[0]
        assert page_size == 8192 and page_size * maximum <= first.limit
