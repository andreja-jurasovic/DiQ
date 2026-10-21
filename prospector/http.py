"""Polite HTTP fetching with a cache: one request at a time per host, a pause between
requests, and no retries past a block (403/429/captcha) - the source is skipped instead."""
import threading
import time
from urllib.parse import urlparse

import httpx

from . import db
from .config import USER_AGENT

_host_locks: dict[str, threading.Lock] = {}
_last_hit: dict[str, float] = {}
_guard = threading.Lock()

client = httpx.Client(
    headers={"User-Agent": USER_AGENT, "Accept-Language": "hr,en;q=0.8"},
    follow_redirects=True,
    timeout=20,
)


class Blocked(Exception):
    """The site refused or challenged the request."""


def get_text(url: str, *, delay: float = 1.5, max_age_days: float = 14) -> str | None:
    """Fetch a page's HTML (cached). Returns None for 404/unreachable pages."""
    key = f"GET {url}"
    cached = db.cache_get(key, max_age_days)
    if cached is not None:
        return cached["html"]
    host = urlparse(url).netloc
    with _guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _last_hit.get(host, 0) + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url)
        except httpx.HTTPError:
            return None
        finally:
            _last_hit[host] = time.time()
    if r.status_code in (403, 429) or "challenges.cloudflare.com/cdn-cgi/challenge" in r.text[:5000]:
        raise Blocked(f"{host} returned {r.status_code}")
    if r.status_code >= 400:
        db.cache_put(key, {"html": None})
        return None
    ctype = r.headers.get("content-type", "")
    html = r.text if "html" in ctype or "text" in ctype else None
    db.cache_put(key, {"html": html})
    return html
