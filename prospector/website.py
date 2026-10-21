"""Read a prospect's own website: a few relevant pages, as plain text, plus the
emails, phone numbers and keywords found on them."""
import re
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from . import http

SUBPAGE_HINTS = re.compile(
    r"kontakt|contact|impressum|o-nama|o_nama|onama|about|nas-tim|proizvod|usluge|ponuda|program|asortiman|rolet|tend|vrata|automatik|ograd|sjenil",
    re.I,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+385|00385|\b0)[\s/().-]*\d{1,2}[\s/().-]*\d{3}[\s/.-]*\d{2,4}(?:[\s/.-]*\d{1,4})?")
MAX_PAGES = 4
MAX_CHARS_PER_PAGE = 6000


def normalize_url(url: str | None) -> str | None:
    if not url or url.lower() in ("no info", "nema web stranicu"):
        return None
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def normalize_phone(raw: str) -> str:
    raw = unquote(raw)
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00385"):
        digits = digits[5:]
    elif digits.startswith("385"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if not 7 <= len(digits) <= 10:
        return raw.strip()
    area_len = 1 if digits[0] == "1" else 2
    area, rest = digits[:area_len], digits[area_len:]
    return f"+385 {area} {rest[:3]} {rest[3:]}".strip()


def is_mobile(phone: str) -> bool:
    return normalize_phone(phone).startswith("+385 9")


def read(url: str | None) -> dict:
    """Pages, emails, phones from a website. Returns {'ok': False} if it can't be read."""
    url = normalize_url(url)
    if not url:
        return {"ok": False, "reason": "no website"}
    try:
        home = http.get_text(url, delay=1)
    except http.Blocked as e:
        return {"ok": False, "reason": str(e), "url": url}
    if not home:
        return {"ok": False, "reason": "unreachable", "url": url}

    pages = [(url, home)]
    soup = BeautifulSoup(home, "lxml")
    host = urlparse(url).netloc.removeprefix("www.")
    candidates = []
    for a in soup.find_all("a", href=True):
        link = urljoin(url, a["href"]).split("#")[0]
        if urlparse(link).netloc.removeprefix("www.") != host or link.rstrip("/") == url.rstrip("/"):
            continue
        if SUBPAGE_HINTS.search(link) or SUBPAGE_HINTS.search(a.get_text(" ", strip=True)):
            if link not in candidates:
                candidates.append(link)
    # contact page first, then product pages
    candidates.sort(key=lambda l: 0 if re.search(r"kontakt|contact", l, re.I) else 1 if re.search(r"impressum|o-nama|o_nama|onama|about|nas-tim", l, re.I) else 2)
    for link in candidates[: MAX_PAGES - 1]:
        try:
            html = http.get_text(link, delay=1)
        except http.Blocked:
            break
        if html:
            pages.append((link, html))

    texts, emails, phones = [], set(), set()
    for link, html in pages:
        page_soup = BeautifulSoup(html, "lxml")
        for a in page_soup.select('a[href^="mailto:"]'):
            emails.add(a["href"][7:].split("?")[0].strip().lower())
        for a in page_soup.select('a[href^="tel:"]'):
            phones.add(normalize_phone(a["href"][4:]))
        text = _visible_text(page_soup)
        emails.update(e.lower() for e in EMAIL_RE.findall(text) if not e.lower().endswith((".png", ".jpg", ".webp")))
        phones.update(normalize_phone(p) for p in PHONE_RE.findall(text))
        texts.append(f"=== {link}\n{text[:MAX_CHARS_PER_PAGE]}")
    return {
        "ok": True,
        "url": url,
        "text": "\n\n".join(texts),
        "emails": sorted(emails),
        "phones": sorted(p for p in phones if p.startswith("+385")),
    }


def keywords_found(text: str, keywords: list[str]) -> list[str]:
    lower = (text or "").lower()
    return [k for k in keywords if re.search(rf"(?<!\w){re.escape(k.lower())}", lower)]
