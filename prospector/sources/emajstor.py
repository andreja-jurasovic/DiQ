"""emajstor.hr contractor directory (https://www.emajstor.hr/imenik/<category>/<town>).

Only public directory and profile pages are read, at a slow pace; robots.txt-disallowed
paths (search, the phone-number reveal endpoint) are never requested.
"""
import json
import re
import unicodedata

from bs4 import BeautifulSoup

from .. import http

BASE = "https://www.emajstor.hr"


def town_slug(town: str) -> str:
    s = unicodedata.normalize("NFKD", town.lower().replace("đ", "d"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def listing(category_slug: str, town: str, max_pages: int = 3) -> list[dict]:
    """Directory entries for a category near a town. emajstor sorts by distance and mixes
    in companies from elsewhere, so the caller filters by location."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        url = f"{BASE}/imenik/{category_slug}/{town_slug(town)}" + (f"?page={page}" if page > 1 else "")
        html = http.get_text(url, delay=3)
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select('a.crt[href*="/izvodjac/"]')
        new = 0
        for a in cards:
            href = a["href"]
            if href in seen:
                continue
            seen.add(href)
            new += 1
            container = a.find_parent("div").find_parent("div")
            parts = container.get_text("|", strip=True).split("|") if container else []
            location = parts[-1].split(",", 1)[-1].strip() if parts and "," in parts[-1] else None
            out.append({"name": a.get_text(" ", strip=True), "profile_url": href, "town": location})
        if not new or not soup.find("a", href=re.compile(rf"\?page={page + 1}\b")):
            break
    return out


def profile(url: str) -> dict:
    """Name, location, services and description from a public profile page."""
    html = http.get_text(url, delay=3)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    info = {"source": "emajstor", "source_id": url, "profile_url": url}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "", strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "LocalBusiness":
            info["description"] = (data.get("description") or "").strip()
            name, _, services = (data.get("name") or "").partition(":")
            info["name"] = name.strip()
            info["services"] = services.strip()
            addr = data.get("address") or {}
            if isinstance(addr, dict):
                info["town"] = addr.get("addressLocality") or None
                info["address"] = ", ".join(x for x in [addr.get("streetAddress"), addr.get("postalCode"), addr.get("addressLocality")] if x) or None
            if data.get("url") and "emajstor" not in data["url"]:
                info["website"] = data["url"]
    text = soup.get_text("|", strip=True)
    m = re.search(r"\|([^|,]+), ([^|]+)\|Tel:", text)
    if m:
        info["town"] = info.get("town") or m.group(1).strip()
        info["county"] = m.group(2).strip()
    m = re.search(r"\|([^|]+?)\|predstavnik tvrtke", text)
    if m:
        info["contact_name"] = m.group(1).strip()
    return info
