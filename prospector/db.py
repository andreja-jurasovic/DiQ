"""SQLite cache for HTTP responses and the local copy of the court register.
Each thread gets its own connection; WAL mode lets them read while one writes."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import CACHE_DIR

DB_PATH = CACHE_DIR / "prospector.db"
_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, value TEXT, fetched_at REAL)")
        _local.conn = c
    return c


@contextmanager
def tx():
    c = conn()
    with c:
        yield c


def cache_get(key: str, max_age_days: float):
    row = conn().execute("SELECT value, fetched_at FROM http_cache WHERE key=?", (key,)).fetchone()
    if row and time.time() - row["fetched_at"] < max_age_days * 86400:
        return json.loads(row["value"])
    return None


def cache_put(key: str, value) -> None:
    with tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
