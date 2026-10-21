"""Revenue lookup as a chain of sources: the first source that returns a figure wins.

Every source returns the same fields - revenue, revenue_year, employees,
revenue_source, revenue_url - or None. Obrti (no OIB) have no public figures,
so no source is asked for them.

Sources:
1. FINA yearly statements - free and exact.
2. CompanyWall public profile scraper - exact public financial summaries.
3. Web-search estimate - used only when the caller permits paid lookups.
"""

from abc import ABC, abstractmethod

from .sources import fina, revenue_scraper, web_revenue


class RevenueSource(ABC):
    paid = False  # paid sources run only when the caller allows them

    @abstractmethod
    def lookup(self, oib: str, name: str, town: str | None = None) -> dict | None:
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


class ScraperSource(RevenueSource):
    """CompanyWall scraper, with persistent caching for hits and misses.

    This source runs after FINA and before the paid web-search fallback.
    """

    def __init__(self):
        self.scraper = revenue_scraper.DirectoryPageSource(
            revenue_scraper.DiskCache()
        )

    def lookup(self, oib, name, town=None):
        return self.scraper.lookup(oib, name, town)


class WebSearchSource(RevenueSource):
    """Paid estimate fallback for relevant companies without public figures."""

    paid = True

    def lookup(self, oib, name, town=None):
        estimate = web_revenue.lookup(name, oib, town)
        if not estimate:
            return None
        return {
            "revenue": estimate.get("revenue_eur"),
            "revenue_year": estimate.get("year"),
            "employees": estimate.get("employees"),
            "revenue_source": "procjena (web)",
            "revenue_url": estimate.get("source_url"),
        }


SOURCES: list[RevenueSource] = [
    FinaSource(),
    ScraperSource(),
    WebSearchSource(),
]


def find(
    oib: str | None,
    name: str,
    town: str | None = None,
    *,
    allow_paid: bool = True,
) -> dict | None:
    if not oib:
        return None
    fina_result = SOURCES[0].lookup(oib, name, town)
    # CompanyWall also publishes profile contacts. Query it even when FINA has
    # financials so the prospect record can combine the independent sources.
    companywall_result = SOURCES[1].lookup(oib, name, town)
    result = dict(companywall_result or {})
    if fina_result:
        result.update(fina_result)  # FINA remains the preferred revenue source
    if result.get("revenue") is not None or not allow_paid:
        return result or None

    estimate = SOURCES[2].lookup(oib, name, town)
    if estimate:
        result.update(estimate)
    return result or None
