"""Claude classifies a prospect from website, directory and register information."""
import hashlib
import json
import threading
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import db
from .config import CLAUDE_MODEL, categories

Segment = Literal[
    "sjenila", "stolarija", "bravarija", "garazna_vrata", "zastita",
    "distributer", "gradevina", "elektro", "ostalo",
]


class Contact(BaseModel):
    name: str
    role: Optional[str] = Field(None, description="e.g. vlasnik, direktor, prodaja")
    email: Optional[str] = None
    phone: Optional[str] = None


class Qualification(BaseModel):
    segment: Segment
    categories: list[str] = Field(description="Product category keys this company fits, from the list given")
    is_relevant: bool = Field(description="Could this company plausibly buy motors/automation from BFT-Adria?")
    relevance_reason: str = Field(description="One sentence, Croatian")
    installs_products: bool = Field(description="Does the company install (montaža/ugradnja) what it sells?")
    mentions_motorization: bool = Field(description="Website mentions motorised/automated products")
    brands_mentioned: list[str] = Field(description="Product brands named on the website (Somfy, BFT, Nice, Came, ...)")
    contacts: list[Contact] = Field(description="Named people only; empty if none found")


SYSTEM = """You qualify B2B sales prospects for BFT-Adria, a Croatian wholesaler and official representative of Somfy, SIMU and BFT.

What BFT-Adria sells (wholesale only, to installers, manufacturers and distributors):
- Somfy/SIMU: motors and controls for rolete (roller shutters), unutarnje rolo zavjese, unutarnji venecijaneri, vanjske žaluzine, tende, pergole, rolo garažna vrata, sekcijska garažna vrata (ceiling motor), klizna and krilna dvorišna vrata, wind/rain/sun sensors, smart-home control by phone.
- BFT: motors for klizna and krilna dvorišna vrata, sekcijska garažna vrata, rampe (barriers), smart control by phone.

Good customers: shading specialists (rolete, tende, pergole), PVC/ALU stolarija makers who fit shutters with motors, bravarije that build gates and fences and fit motors, garage-door installers, security companies (tehnička zaštita), distributors reselling to installers. Electricians are low priority. Pure construction firms, interior carpenters (sobna vrata, namještaj) and unrelated businesses are usually not relevant.

Rules:
- Use only the material provided. Never invent people, emails or phone numbers; leave fields empty when unknown.
- Segment values: sjenila, stolarija, bravarija, garazna_vrata, zastita, distributer, gradevina, elektro, ostalo."""
_client = None
# USD per million tokens for claude-opus-5-5 (input, output)
PRICE = (4.0, 20.0)
usage = {"input": 0, "output": 0, "calls": 0}
_usage_lock = threading.Lock()


def cost_usd() -> float:
    return (usage["input"] * PRICE[0] + usage["output"] * PRICE[1]) / 1e6


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def qualify(prospect: dict, site: dict) -> Qualification | None:
    cats = {k: v["label"] for k, v in categories().items()}
    facts = {
        k: prospect.get(k)
        for k in [
            "name", "legal_name", "town", "county", "address", "nkd_name", "founded",
            "google_category", "emajstor_services", "emajstor_description", "rating", "reviews",
        ]
        if prospect.get(k)
    }
    website_part = (
        f"Website text ({site['url']}):\n{site['text'][:24000]}"
        if site.get("ok")
        else f"Website: not available ({site.get('reason', 'none')})."
    )
    content = (
        f"Product category keys: {json.dumps(cats, ensure_ascii=False)}\n\n"
        f"Known facts:\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
        f"{website_part}"
    )
    # same model, prompt and input -> same answer; re-runs don't pay twice
    key = "qualify " + hashlib.sha256(f"{CLAUDE_MODEL}\n{SYSTEM}\n{content}".encode()).hexdigest()
    cached = db.cache_get(key, 60)
    if cached is not None:
        return Qualification.model_validate(cached)
    response = _get_client().beta.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=SYSTEM,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
        output_format=Qualification,
    )
    with _usage_lock:
        usage["calls"] += 1
        usage["input"] += response.usage.input_tokens + (response.usage.cache_read_input_tokens or 0)
        usage["output"] += response.usage.output_tokens
    if response.stop_reason in ("refusal", "max_tokens"):
        return None
    result = response.parsed_output
    result.categories = [c for c in result.categories if c in cats]
    db.cache_put(key, result.model_dump())
    return result
