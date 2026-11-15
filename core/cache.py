"""Small SQLite response cache with per-entry age, kept in the plugin's own data directory.

Entries are never trusted past the TTL the caller asks for; older entries are only used as a
clearly flagged fallback when a fresh fetch fails (see ``http.get_json``).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any

from . import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key     TEXT PRIMARY KEY,
    status  INTEGER NOT NULL,
    body    TEXT NOT NULL,
    fetched REAL NOT NULL
)
"""

# Nothing older than this is worth keeping, whatever the caller's max_stale.
MAX_KEEP_SECONDS = 45 * 24 * 3600


class Cache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def get(self, key: str) -> tuple[int, str, float] | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT status, body, fetched FROM http_cache WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.Error:  # busy/locked/corrupt: behave like a cache miss, never fail the caller
            logger.debug("better-weather: cache read failed", exc_info=True)
            return None
        if row is None:
            return None
        return int(row[0]), str(row[1]), float(row[2])

    def put(self, key: str, status: int, body: str, fetched: float | None = None) -> None:
        stamp = time.time() if fetched is None else fetched
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO http_cache (key, status, body, fetched) VALUES (?, ?, ?, ?)",
                    (key, status, body, stamp),
                )
                self._conn.commit()
        except sqlite3.Error:
            logger.debug("better-weather: cache write failed", exc_info=True)

    def prune(self, older_than: float = MAX_KEEP_SECONDS) -> int:
        cutoff = time.time() - older_than
        with self._lock:
            cur = self._conn.execute("DELETE FROM http_cache WHERE fetched < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount or 0

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM http_cache")
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            count, oldest = self._conn.execute(
                "SELECT COUNT(*), MIN(fetched) FROM http_cache"
            ).fetchone()
        return {"entries": int(count or 0), "oldest_age_s": (time.time() - oldest) if oldest else None}


_cache: Cache | None = None
_cache_lock = threading.Lock()


def _open_connection() -> sqlite3.Connection:
    try:
        from plugins.plugin_storage import plugin_db  # type: ignore[import-not-found]

        return plugin_db(settings.PLUGIN_NAME, "cache.db")
    except Exception:
        pass
    directory = settings.data_dir()
    if directory is not None:
        try:
            return sqlite3.connect(str(directory / "cache.db"), check_same_thread=False, timeout=5)
        except sqlite3.Error:
            logger.warning("better-weather: could not open cache.db; caching in memory only")
    return sqlite3.connect(":memory:", check_same_thread=False)


def get_cache() -> Cache:
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is None:
            try:
                cache = Cache(_open_connection())
            except sqlite3.Error:
                logger.warning("better-weather: cache.db unusable; caching in memory only")
                cache = Cache(sqlite3.connect(":memory:", check_same_thread=False))
            try:
                cache.prune()
            except sqlite3.Error:
                logger.debug("better-weather: cache prune failed", exc_info=True)
            _cache = cache
    return _cache


def use_memory_cache_for_tests() -> Cache:
    global _cache
    with _cache_lock:
        _cache = Cache(sqlite3.connect(":memory:", check_same_thread=False))
    return _cache
