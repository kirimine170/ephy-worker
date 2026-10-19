"""Bounded，query-hash-only SQLite success cache and persisted service cooldown．"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .schema import Candidate
from .search import SearchError
from .tavily_keyless import _json_object, _result_url, _unicode_string

_SCHEMA = {
    "cache": [("key", "TEXT", 0, 1), ("created", "REAL", 1, 0),
              ("expires", "REAL", 1, 0), ("data", "BLOB", 1, 0)],
    "cooldown": [("backend", "TEXT", 0, 1), ("expires", "REAL", 1, 0), ("reason", "TEXT", 1, 0)],
}


def decode_candidates(data: bytes, query_id: str):
    try:
        rows = json.loads(data, object_pairs_hook=_json_object)
        if not isinstance(rows, list) or len(rows) > 20:
            raise ValueError
        candidates = [Candidate.model_validate(row) for row in rows]
        for candidate in candidates:
            if (not _result_url(candidate.url) or len(candidate.title) > 500 or len(candidate.snippet) > 1000
                    or not all(_unicode_string(getattr(candidate, key))
                               for key in ("url", "title", "snippet", "query_id", "engine", "discovered_at"))
                    or candidate.rank < 1 or candidate.supplied):
                raise ValueError
            candidate.query_id = query_id
            if datetime.fromisoformat(candidate.discovered_at).tzinfo is None:
                raise ValueError
        return candidates
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SearchError("search_cache_invalid_data") from None


class SQLiteSearchCache:
    def __init__(self, policy):
        self.policy = policy
        self.path = Path(policy.sqlite_path).expanduser()
        self.limit = policy.cache_max_bytes + 512 * 1024
        self._path()

    def _path(self):
        path = self.path
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise SearchError("search_cache_unsafe_path")
        for item in (path, *path.parents):
            if item.is_symlink() or item.is_junction() or (item / ".git").exists():
                raise SearchError("search_cache_unsafe_path")
        if path.exists() and (not path.is_file() or path.stat().st_size > self.limit):
            raise SearchError("search_cache_storage_limit")
        return path

    @contextmanager
    def _db(self):
        db = None
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(path, timeout=0.05)
            if os.name == "posix":
                os.chmod(path, 0o600)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.policy.cache_max_bytes + 8192)
            db.execute("PRAGMA busy_timeout=50")
            db.execute("PRAGMA trusted_schema=OFF")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={max(1, self.limit // page_size)}")
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not tables and version == 0:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE cache(key TEXT PRIMARY KEY,created REAL NOT NULL,"
                           "expires REAL NOT NULL,data BLOB NOT NULL)")
                db.execute("CREATE TABLE cooldown(backend TEXT PRIMARY KEY,expires REAL NOT NULL,"
                           "reason TEXT NOT NULL)")
                db.execute("PRAGMA user_version=1")
                db.commit()
            elif version != 1 or {row[0] for row in tables} != set(_SCHEMA):
                raise SearchError("search_cache_schema_error")
            for table, expected in _SCHEMA.items():
                actual = [(row[1], row[2], row[3], row[5]) for row in db.execute(f"PRAGMA table_info({table})")]
                if actual != expected:
                    raise SearchError("search_cache_schema_error")
            extras = db.execute("SELECT type,name FROM sqlite_master WHERE type NOT IN ('table','index') "
                                "OR (type='index' AND name NOT LIKE 'sqlite_autoindex_%')").fetchall()
            if extras:
                raise SearchError("search_cache_schema_error")
            if db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise SearchError("search_cache_corrupt")
            yield db
        except (sqlite3.Error, OSError):
            raise SearchError("search_cache_storage_error") from None
        finally:
            if db is not None:
                db.close()

    def lookup(self, key, backend):
        now = time.time()
        with self._db() as db:
            row = db.execute("SELECT created,expires,data FROM cache WHERE key=?", (key,)).fetchone()
            cooldown = db.execute("SELECT expires,reason FROM cooldown WHERE backend=?", (backend,)).fetchone()
            data = None
            if row is not None:
                created, expires, blob = row
                if (type(created) not in {float, int} or type(expires) not in {float, int}
                        or not math.isfinite(created) or not math.isfinite(expires)
                        or expires < created or not isinstance(blob, bytes)
                        or len(blob) > self.policy.cache_max_bytes):
                    raise SearchError("search_cache_invalid_data")
                if 0 <= now - created < self.policy.cache_ttl_seconds and now < expires:
                    data = blob
            if cooldown is not None:
                expires, reason = cooldown
                if (type(expires) not in {float, int} or not math.isfinite(expires)
                        or reason not in {"challenge", "rate_limited", "access_denied"}):
                    raise SearchError("search_cache_invalid_data")
                cooldown = (max(0, expires - now), reason)
            return data, cooldown

    def put(self, key, data):
        if self.policy.cache_ttl_seconds <= 0 or len(data) > self.policy.cache_max_bytes:
            return
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM cache WHERE expires<=?", (now,))
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                       (key, now, now + self.policy.cache_ttl_seconds, data))
            while True:
                count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(data)),0) FROM cache").fetchone()
                if count <= self.policy.cache_max_entries and size <= self.policy.cache_max_bytes:
                    break
                db.execute("DELETE FROM cache WHERE key=(SELECT key FROM cache ORDER BY created,key LIMIT 1)")
            db.commit()

    def cool(self, backend, delay, reason):
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM cooldown WHERE expires<=?", (now,))
            count = db.execute("SELECT COUNT(*) FROM cooldown").fetchone()[0]
            old = db.execute("SELECT expires FROM cooldown WHERE backend=?", (backend,)).fetchone()
            if old is None and count >= 32:
                raise SearchError("search_cache_backend_limit")
            db.execute("INSERT OR REPLACE INTO cooldown VALUES(?,?,?)",
                       (backend, max(now + delay, old[0] if old else 0), reason))
            db.commit()
