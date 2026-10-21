"""Revenue lookup as a chain of sources: the first source that returns a figure wins.

Every source returns the same fields - revenue, revenue_year, employees,
revenue_source, revenue_url - or None. Obrti (no OIB) have no public figures,
so no source is asked for them.

Revenue sources:
1. FINA yearly statements - free and exact.
2. Business-directory scraper - scrapes the company's public page.
"""

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from bs4 import BeautifulSoup

from .sources import fina


# Sentinel used by the cache to distinguish a cache miss from a cached None.
_NOT_SET = object()


class RevenueSource(ABC):
    paid = False

    @abstractmethod
    def lookup(
        self,
        oib: str,
        name: str,
        town: str | None = None,
    ) -> dict | None:
        """Financial figures for one company, or None."""


class FinaSource(RevenueSource):
    """FINA yearly statements imported with `prospector import-fina`.
    Free and exact.
    """

    def lookup(self, oib, name, town=None):
        rec = fina.latest(oib)
        if not rec:
            return None

        return {
            "revenue": rec["revenue"],
            "revenue_year": rec["year"],
            "employees": rec["employees"],
            "revenue_source": "FINA",
            "revenue_url": None,
        }


class DiskCache:
    """Persistent JSON cache supporting cached misses.

    Hits and misses are cached for 180 days.
    """

    def __init__(self, cache_file: str = "data/revenue_cache.json"):
        self.cache_path = Path(cache_file)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._store: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.cache_path.exists():
            try:
                import json

                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}

        return {}

    def _save(self) -> None:
        import json

        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self._store, f, indent=2)

    def has(self, key: str, max_age_days: int = 180) -> bool:
        entry = self._store.get(key)

        if not entry:
            return False

        timestamp = entry.get("cached_at")

        if not timestamp:
            return False

        try:
            cached_time = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return False

        now = datetime.now(timezone.utc)
        age_days = (now - cached_time).total_seconds() / 86400.0

        if age_days > max_age_days:
            del self._store[key]
            self._save()
            return False

        return True

    def get(self, key: str, max_age_days: int = 180) -> Any:
        if not self.has(key, max_age_days=max_age_days):
            return _NOT_SET

        return self._store[key].get("data")

    def put(self, key: str, value: Optional[Dict[str, Any]]) -> None:
        self._store[key] = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "data": value,
        }
        self._save()


def to_eur(amount_str: str, unit_str: Optional[str] = None) -> float:
    """Convert Croatian-formatted financial text to euros."""

    cleaned = amount_str.replace(".", "").replace(",", ".")
    number = float(cleaned)

    if unit_str:
        unit = unit_str.strip().rstrip(".").lower()

        if unit in ("mil", "miljun", "milijun"):
            number *= 1_000_000
        elif unit in ("tis", "tisuca", "tisuća"):
            number *= 1_000
        elif unit in ("mlrd", "milijarda"):
            number *= 1_000_000_000
        elif unit in ("eur", "euro", "€"):
            pass
        else:
            raise ValueError(f"Unrecognized unit suffix: '{unit_str}'")

    return number


def parse_financials(html_text: str) -> dict:
    """Extract revenue and employee data from a scraped company page."""

    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    # Revenue
    revenue_pattern = (
        r"Ukupni prihodi u (\d{4})\. godini iznosili su "
        r"€ ([\d\.,]+)(?:\s+([\w\.]+))?"
    )

    rev_match = re.search(revenue_pattern, text, re.IGNORECASE)

    year = int(rev_match.group(1)) if rev_match else None

    revenue = None

    if rev_match:
        try:
            revenue = to_eur(
                rev_match.group(2),
                rev_match.group(3),
            )
        except ValueError:
            revenue = None

    # Employees
    emp_pattern = r"Broj zaposlenih .*? u (\d{4})\. godini je (\d+)"

    emp_match = re.search(emp_pattern, text, re.IGNORECASE)

    employees = int(emp_match.group(2)) if emp_match else None

    # OIB
    oib_pattern = r"OIB\s*(\d{11})"

    oib_match = re.search(oib_pattern, text)

    page_oib = oib_match.group(1) if oib_match else None

    return {
        "oib": page_oib,
        "year": year,
        "revenue": revenue,
        "employees": employees,
    }


class ScraperSource(RevenueSource):
    """Scrapes the public company directory instead of using an LLM.

    Results, including misses, are cached for 180 days.
    """

    def __init__(self, cache: DiskCache):
        self.cache = cache
        self.is_circuit_broken = False

    def locate_company_page(
        self,
        oib: str,
        name: str,
    ) -> str | None:
        return f"https://directory.example.hr/company/{oib}"

    def lookup(
        self,
        oib: str,
        name: str,
        town: str | None = None,
    ) -> dict | None:

        if self.is_circuit_broken:
            return None

        cache_key = f"directory_{oib}"

        # Return cached hit or cached miss.
        cached = self.cache.get(cache_key, max_age_days=180)

        if cached is not _NOT_SET:
            return cached

        page_url = self.locate_company_page(oib, name)

        if not page_url:
            self.cache.put(cache_key, None)
            return None

        try:
            response = requests.get(
                page_url,
                timeout=5,
                headers={
                    "User-Agent": "Mozilla/5.0",
                },
            )

            # Treat anti-bot responses as a circuit-breaker condition.
            if (
                response.status_code in (403, 429)
                or "captcha" in response.text.lower()
            ):
                self.is_circuit_broken = True
                self.cache.put(cache_key, None)
                return None

            response.raise_for_status()

        except requests.RequestException:
            self.cache.put(cache_key, None)
            return None

        facts = parse_financials(response.text)

        # Never accept data from a page whose OIB does not match.
        if facts["oib"] != oib:
            self.cache.put(cache_key, None)
            return None

        # A page without revenue is not considered a successful lookup.
        if facts["revenue"] is None:
            self.cache.put(cache_key, None)
            return None

        result = {
            "revenue": facts["revenue"],
            "revenue_year": facts["year"],
            "employees": facts["employees"],
            "revenue_source": "directory",
            "revenue_url": page_url,
        }

        self.cache.put(cache_key, result)

        return result


# The scraper is no longer paid and does not use an LLM.
SOURCES: list[RevenueSource] = [
    FinaSource(),
    ScraperSource(DiskCache()),
]


def find(
    oib: str | None,
    name: str,
    town: str | None = None,
    *,
    allow_paid: bool = True,
) -> dict | None:
    """Return the first available revenue figure.

    `allow_paid` is retained for backwards compatibility with callers.
    The current sources are both free, so it does not change behavior.
    """

    if not oib:
        return None

    for source in SOURCES:
        if source.paid and not allow_paid:
            continue

        result = source.lookup(oib, name, town)

        if result is not None:
            return result

    return None