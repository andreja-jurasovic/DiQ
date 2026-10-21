"""Fallback revenue estimate from web search results, for companies with an OIB
but no FINA figure. Claude runs a normal web search and reads the results; no
financial site is fetched or scraped. Figures are marked as estimates."""
import threading
from typing import Optional

from pydantic import BaseModel, Field

from .. import db, qualify

MODEL = "claude-sonnet-5-5"
PRICE = (2.0, 10.0)        # USD per million tokens (input, output) for MODEL
SEARCH_PRICE = 0.01        # USD per web search


class RevenueEstimate(BaseModel):
    found: bool
    revenue_eur: Optional[float] = Field(None, description="Total revenue (ukupni prihodi) in EUR")
    year: Optional[int] = None
    employees: Optional[int] = None
    source_url: Optional[str] = None
    confidence: str = Field(description="high, medium or low")


PROMPT = (
    "Find the latest annual revenue (ukupni prihodi) and number of employees of the Croatian company "
    "{name}, OIB {oib}, {town}. Use web search. Accept a figure only if the result clearly refers to "
    "this company (same OIB, or same name and town). Convert HRK to EUR at 7.5345 if needed. "
    "If nothing reliable is found, set found to false."
)
CACHE_DAYS = 180
_lock = threading.Lock()
usage = {"calls": 0, "searches": 0, "input": 0, "output": 0}


def search(name: str, oib: str, town: str | None) -> dict:
    """One uncached lookup. Returns the estimate fields, or {} if nothing reliable was found."""
    response = qualify._get_client().beta.messages.parse(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}],
        messages=[{"role": "user", "content": PROMPT.format(name=name, oib=oib, town=town or "Hrvatska")}],
        output_format=RevenueEstimate,
    )
    with _lock:
        usage["calls"] += 1
        usage["searches"] += getattr(response.usage.server_tool_use, "web_search_requests", 0) or 0
        usage["input"] += response.usage.input_tokens + (response.usage.cache_read_input_tokens or 0)
        usage["output"] += response.usage.output_tokens
    est = response.parsed_output if response.stop_reason == "end_turn" else None
    if est and est.found and est.revenue_eur and est.confidence in ("high", "medium"):
        return est.model_dump()
    return {}


def lookup(name: str, oib: str, town: str | None) -> dict | None:
    """Cached lookup; misses are remembered too, so a company is searched once per 180 days."""
    key = f"web_revenue {oib}"
    cached = db.cache_get(key, CACHE_DAYS)
    if cached is not None:
        return cached or None
    result = search(name, oib, town)
    db.cache_put(key, result)
    return result or None


def cost_usd() -> float:
    return (usage["input"] * PRICE[0] + usage["output"] * PRICE[1]) / 1e6 + usage["searches"] * SEARCH_PRICE
