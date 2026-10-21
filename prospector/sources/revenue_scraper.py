"""Public revenue lookup from CompanyWall company profiles.

CompanyWall's search accepts an OIB at ``/pretraga?n=...``. Search results
link to profile URLs containing an opaque identifier, so profile URLs must be
discovered from search results rather than synthesized from a company name.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .. import db

BASE_URL = "https://www.companywall.hr"
SEARCH_URL = f"{BASE_URL}/pretraga"
CACHE_DAYS = 180
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; sales-DiQ/1.0; revenue lookup)"
_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_CACHED_MISS = object()


def _amount(value: str) -> float | None:
    """Parse a Croatian-formatted monetary or employee value."""
    value = value.strip().replace("\u00a0", " ")
    if not value or value in {"-", "—", "–"}:
        return None
    # CompanyWall formats financial amounts as 1.234.567,89 and employees as
    # 123,00. Strip any currency marker, then convert Croatian separators.
    value = re.sub(r"[^\d,.-]", "", value)
    if not value or value in {"-", ".", ","}:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):
        # Croatian pages can omit ,00 while retaining period thousands marks.
        value = value.replace(".", "")
    try:
        return float(value)
    except ValueError:
        return None


def _find_financials(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                for row in table.find_all("tr")]
        rows = [row for row in rows if row]
        if not rows:
            continue

        # CompanyWall's financial summary has years in one header row and
        # metric names down the first column.
        years: list[int | None] = []
        for cell in rows[0]:
            match = re.search(r"\b(20\d{2})\b", cell)
            years.append(int(match.group(1)) if match else None)
        revenue_row = next((r for r in rows if r[0].strip().casefold() == "ukupni prihodi"), None)
        employees_row = next((r for r in rows if r[0].strip().casefold() == "broj zaposlenih"), None)
        if revenue_row is None:
            continue

        # Align values to the last year in the table with a reported revenue.
        options = []
        for idx, year in enumerate(years):
            if year is None or idx >= len(revenue_row):
                continue
            amount = _amount(revenue_row[idx])
            if amount is not None:
                employees = _amount(employees_row[idx]) if employees_row and idx < len(employees_row) else None
                options.append((year, amount, employees))
        if options:
            year, revenue, employees = max(options, key=lambda item: item[0])
            return {"revenue": revenue, "revenue_year": year,
                    "employees": int(employees) if employees is not None else None}

    # Some profiles expose the latest summary text even when their financial
    # table is absent or gated. Keep that source year explicit as well.
    text = soup.get_text(" ", strip=True)
    match = re.search(
        r"U\s+(20\d{2})\.?\s+godini\s+tvrtka\s+je\s+ostvarila\s+ukupne\s+prihode\s+"
        r"u\s+iznosu\s+od\s+([\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    if match:
        revenue = _amount(match.group(2))
        if revenue is not None:
            emp_match = re.search(r"zapošljava\s+ukupno\s+(\d+)\s+radnika", text, re.IGNORECASE)
            return {"revenue": revenue, "revenue_year": int(match.group(1)),
                    "employees": int(emp_match.group(1)) if emp_match else None}
    return None


def _find_contacts(html: str) -> dict:
    """Extract contact details only from CompanyWall's Kontakti panel.

    The page repeats labels such as "Vlasnik" and "Direktor" in narrative
    descriptions, FAQs, and ownership tables. Reading them from all page text
    can turn a paragraph into a person's name, so use the contact section's
    definition-list fields only.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(
        lambda tag: tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
        and tag.get_text(" ", strip=True).casefold() == "kontakti"
    )
    section = heading.find_parent("section") if heading else None
    result = {"emails": [], "phones": [], "contacts": []}
    fields: dict[str, list[str]] = {}
    if section:
        for label in section.find_all("dt"):
            value = label.find_next_sibling("dd")
            if value:
                key = re.sub(r"\s+", " ", label.get_text(" ", strip=True)).casefold().strip()
                text = value.get_text("\n", strip=True)
                fields.setdefault(key, []).append(text)

    for value in fields.get("tel", []):
        for phone in value.splitlines():
            if re.search(r"\d[\d\s()+./-]{5,}\d", phone):
                result["phones"].append(phone.strip())
    for key in ("e-mail", "email"):
        for value in fields.get(key, []):
            result["emails"].extend(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value))
    web_values = fields.get("web", [])
    if web_values:
        website = web_values[0].strip()
        if website and not website.startswith(("http://", "https://")):
            website = "https://" + website
        if website:
            result["company_website"] = website

    for key, role in (("vlasnik", "Vlasnik"), ("direktor", "Direktor"), ("zastupnik", "Zastupnik")):
        for person in fields.get(key, []):
            # Contact values can include a short role/legal suffix after a
            # comma; the person's name is the first field only.
            name = person.split(",", 1)[0].strip()
            if _is_person_name(name):
                result["contacts"].append({"name": name, "role": role})
    if result["contacts"]:
        result["contact_name"] = result["contacts"][0]["name"]
    result["emails"] = list(dict.fromkeys(email.lower() for email in result["emails"]))
    result["phones"] = list(dict.fromkeys(result["phones"]))
    return result


def _is_person_name(value: str) -> bool:
    """Reject descriptions and entity names accidentally placed in a people field."""
    words = value.split()
    if not 2 <= len(words) <= 4:
        return False
    return all(
        word[0].isalpha() and all(char.isalpha() or char in ".'’-" for char in word)
        for word in words
    )


def _oib_from_profile(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(r"\bOIB\s*[:：]?\s*(\d{11})\b", text, re.IGNORECASE)
    return match.group(1) if match else None


class DiskCache:
    """Small adapter over the application's persistent SQLite HTTP cache."""

    def get(self, key: str, max_age_days: int = CACHE_DAYS):
        value = db.cache_get(key, max_age_days)
        if isinstance(value, dict) and value.get("_companywall_miss") is True:
            return _CACHED_MISS
        return value

    def put(self, key: str, value) -> None:
        # The app cache represents absent and expired entries as None. Store an
        # explicit marker so negative lookups are cached too.
        db.cache_put(key, value if value is not None else {"_companywall_miss": True})


class DirectoryPageSource:
    """Find and parse a public CompanyWall profile using the company's OIB."""

    def __init__(self, cache: DiskCache | None = None, *, session=None):
        self.cache = cache or DiskCache()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "hr,en;q=0.8"})

    def _find_company_profile(self, oib: str) -> tuple[str, str] | None:
        response = self.session.get(SEARCH_URL, params={"n": oib}, timeout=TIMEOUT)
        response.raise_for_status()
        if "ReCaptchaInfo" in response.url or "ograničena pretraga" in response.text.casefold():
            raise requests.RequestException("CompanyWall search is temporarily restricted")
        soup = BeautifulSoup(response.text, "html.parser")
        links = []
        for anchor in soup.select('a[href*="/tvrtka/"]'):
            href = anchor.get("href")
            if href:
                url = urljoin(BASE_URL, href)
                if url not in links:
                    links.append(url)
        # OIB search normally returns one company; cap requests if the site
        # returns broad/irrelevant results.
        for url in links[:5]:
            profile = self.session.get(url, timeout=TIMEOUT)
            profile.raise_for_status()
            if _oib_from_profile(profile.text) == oib:
                return profile.url, profile.text
        return None

    def locate_company_page(self, oib: str) -> str | None:
        """Return the exact CompanyWall profile URL for an OIB, if found."""
        profile = self._find_company_profile(oib)
        return profile[0] if profile else None

    def lookup(self, oib: str, name: str, town: str | None = None) -> dict | None:
        if not oib:
            return None
        key = f"companywall_revenue_{oib}"
        cached = self.cache.get(key, CACHE_DAYS)
        if cached is _CACHED_MISS:
            return None
        if cached is not None and cached.get("_contact_schema") == 2:
            return cached

        # Keep concurrent workers from duplicating the same lookup while
        # allowing independent companies to be fetched in parallel.
        with _lock:
            key_lock = _key_locks.setdefault(key, threading.Lock())
        with key_lock:
            cached = self.cache.get(key, CACHE_DAYS)
            if cached is _CACHED_MISS:
                return None
            if cached is not None and cached.get("_contact_schema") == 2:
                return cached
            try:
                profile = self._find_company_profile(oib)
                if profile is None:
                    self.cache.put(key, None)
                    return None
                page_url, html = profile
                facts = _find_financials(html) or {}
                contacts = _find_contacts(html)
            except requests.RequestException:
                # Temporary network or rate-limit errors should not be cached
                # as a permanent data miss.
                return None

            if not facts and not any(contacts.get(k) for k in ("emails", "phones", "contacts", "company_website")):
                self.cache.put(key, None)
                return None
            result = {
                **facts,
                **contacts,
                "_contact_schema": 2,
                "revenue_source": "CompanyWall",
                "revenue_url": page_url,
            }
            self.cache.put(key, result)
            return result
