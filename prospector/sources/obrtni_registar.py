"""Low-volume lookups in the official public Obrtni registar search UI.

Use the portal's regular search form and visible text only. Do not retry or
work around CAPTCHA/access-denied pages. This is for matching known prospects,
not downloading the registry as a bulk dataset.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin

from . import sudreg

SEARCH_URL = "https://pretrazivac-obrta.gov.hr/pretraga.htm"
LOOKUP_DELAY_SECONDS = 1.5

GENERIC_WORDS = {
    "obrt", "obrtni", "za", "i", "vl", "vlasnik", "vlasnica", "proizvodnja", "proizvodnju",
    "usluge", "trgovina", "servis", "montaza", "rolete", "roleta", "tende", "tenda", "sjenila",
    "vrata", "prozori", "stolarija", "stolarije", "pvc", "alu", "gradnja", "izrada", "obavljanje",
}


class PortalBlocked(RuntimeError):
    """The public portal returned a CAPTCHA, denial, or other access challenge."""


def _tokens(value: str | None) -> set[str]:
    return {
        token for token in sudreg.normalize(value or "").split()
        if len(token) >= 3 and token not in GENERIC_WORDS
    }


def _owner_and_trade_name(p: dict) -> tuple[str | None, str]:
    name = p.get("legal_name") or p.get("name") or ""
    match = re.search(r"\bvl(?:\.|asnik|asnica)\s*:?\s*([^,;]+)", name, re.I)
    if not match:
        return None, name.strip()
    return match.group(1).strip(), name[:match.start()].rstrip(" ,")


def _read_value(lines: list[str], *labels: str) -> str | None:
    normalized_labels = {sudreg.normalize(label).rstrip(":") for label in labels}
    for index, line in enumerate(lines):
        normalized = sudreg.normalize(line)
        for label in normalized_labels:
            if normalized == label:
                if index + 1 < len(lines):
                    value = lines[index + 1].strip()
                    return value if value and value != "-" else None
            if normalized.startswith(label + " ") or normalized.startswith(label + ":"):
                value = line[len(label):].lstrip(" :\t")
                return value if value and value != "-" else None
    return None


def _blocked(page) -> bool:
    url = (page.url or "").casefold()
    body = page.locator("body").inner_text(timeout=5000).casefold()
    return any(token in url or token in body[:4000] for token in (
        "captcha", "request rejected", "requested url was rejected", "access denied",
        "too many requests", "automated access", "support id:",
    ))


def _open_search(page, *, owner: str | None, trade_name: str) -> list[tuple[str, str]]:
    """Return candidate (visible name, detail URL) pairs from the normal form."""
    response = page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
    if response and response.status in (403, 429):
        raise PortalBlocked(f"portal returned HTTP {response.status}")
    if _blocked(page):
        raise PortalBlocked("portal returned a CAPTCHA or access-denied page")

    input_id = "#vlasnikImePrezime" if owner else "#obrtNaziv"
    if page.locator(input_id).count() == 0:
        raise PortalBlocked("official search form was not served; stopping rather than retrying")
    if owner:
        page.locator(input_id).first.fill(owner)
    else:
        page.locator(input_id).first.fill(trade_name)
    page.locator("#jednostavnaPretragaBtn").click()
    page.wait_for_timeout(1200)
    if _blocked(page):
        raise PortalBlocked("portal returned a CAPTCHA or access-denied page")

    links = page.locator("a[href*='detalji.htm?id=']")
    candidates = []
    for index in range(links.count()):
        link = links.nth(index)
        label = " ".join(link.inner_text().split())
        href = link.get_attribute("href")
        if label and href:
            candidates.append((label, urljoin(page.url, href)))
    return list(dict.fromkeys(candidates))


def _candidate_matches(label: str, p: dict, owner: str | None, trade_name: str) -> bool:
    owner_tokens = _tokens(owner)
    label_tokens = _tokens(label)
    if owner_tokens:
        return owner_tokens <= label_tokens
    name_tokens = _tokens(trade_name or p.get("name"))
    if not name_tokens or not name_tokens <= label_tokens:
        return False
    town_tokens = _tokens(p.get("town"))
    return not town_tokens or town_tokens <= label_tokens


def _extract_detail(page, p: dict, owner_query: str | None, trade_name: str) -> dict | None:
    if _blocked(page):
        raise PortalBlocked("portal returned a CAPTCHA or access-denied page")
    lines = [line.strip() for line in page.locator("body").inner_text().splitlines() if line.strip()]
    full_name = next((line for line in lines
                      if "obrt" in sudreg.normalize(line) and len(line) > 12
                      and not sudreg.normalize(line).startswith("preglednik")), None)
    owner = _read_value(lines, "Prezime i ime")
    town = _read_value(lines, "Naselje", "Općina / grad")
    county = _read_value(lines, "Županija")
    street = _read_value(lines, "Ulica")
    status = _read_value(lines, "Stanje obrta")
    mbo = _read_value(lines, "MBO")
    email = _read_value(lines, "e-mail", "E-mail")
    web = _read_value(lines, "WWW adresa")
    activity = next((line.split(":", 1)[1].strip() for line in lines
                     if sudreg.normalize(line).startswith("pretezita djelatnost:")), None)
    if not activity:
        activity = next((lines[i + 1] for i, line in enumerate(lines[:-1])
                         if sudreg.normalize(line) == "pretezita djelatnost"), None)

    if not full_name or not status or not mbo:
        return None
    # Confirm against the actual detail record before using any data.
    if owner_query and _tokens(owner_query) and not _tokens(owner_query) <= _tokens(owner or full_name):
        return None
    if not owner_query and not _candidate_matches(full_name, p, None, trade_name):
        return None
    expected_town = _tokens(p.get("town"))
    if expected_town and not expected_town <= _tokens(town or ""):
        return None

    data = {
        "obrt_registry_name": full_name,
        "obrt_registry_status": status,
        "obrt_registry_mbo": mbo,
        "obrt_registry_activity": activity,
        "obrt_registry_url": page.url,
        "obrt_registry_checked": True,
    }
    if owner:
        data["contact_name"] = owner.title()
        data["contacts"] = [{"name": owner.title(), "role": "Vlasnik"}]
    if street:
        data["register_address"] = ", ".join(part for part in (street, town) if part)
    if town:
        data["town"] = town.title()
    if county:
        normalized_county = county.title()
        if normalized_county.endswith(" Županija"):
            normalized_county = normalized_county[:-len(" Županija")] + " županija"
        data["county"] = normalized_county
    if email and "@" in email:
        data["emails"] = [email.lower()]
    if web and "." in web:
        data["website"] = web if web.startswith(("http://", "https://")) else f"https://{web}"
    return data


def lookup_one(page, p: dict) -> dict | None:
    owner, trade_name = _owner_and_trade_name(p)
    if not trade_name.strip():
        return None
    candidates = _open_search(page, owner=owner, trade_name=trade_name)
    matches = [(label, url) for label, url in candidates if _candidate_matches(label, p, owner, trade_name)]
    if len(matches) != 1:
        # If owner search had no unique result, try the trade name once.
        if owner:
            time.sleep(LOOKUP_DELAY_SECONDS)
            candidates = _open_search(page, owner=None, trade_name=trade_name)
            matches = [(label, url) for label, url in candidates
                       if _candidate_matches(label, p, None, trade_name)]
    if len(matches) != 1:
        return None
    time.sleep(LOOKUP_DELAY_SECONDS)
    page.goto(matches[0][1], wait_until="domcontentloaded", timeout=30000)
    return _extract_detail(page, p, owner if owner else None, trade_name)


def enrich(prospects: list[dict], *, limit: int = 15, log=print) -> None:
    """Look up up to ``limit`` potential crafts, serially, using the public UI."""
    candidates = [p for p in prospects if not p.get("oib")]
    if not candidates:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Obrtni registar: Playwright nije instaliran; koristim ručne poveznice.")
        return

    selected = candidates[:max(0, limit)]
    log(f"Obrtni registar: provjera {len(selected)}/{len(candidates)} potencijalnih obrta (pojedinačno).")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(locale="hr-HR")
            try:
                for index, prospect in enumerate(selected, 1):
                    if index > 1:
                        time.sleep(LOOKUP_DELAY_SECONDS)
                    try:
                        result = lookup_one(page, prospect)
                    except PortalBlocked as error:
                        log(f"Obrtni registar zaustavljen: {error}; preostali zapisi ostaju neprovjereni.")
                        break
                    except Exception as error:
                        log(f"Obrtni registar: preskačem {prospect.get('name', 'zapis')} ({type(error).__name__}).")
                        continue
                    if result:
                        prospect.update(result)
                        prospect.setdefault("sources", set()).add("obrtni_registar")
                    else:
                        prospect["obrt_registry_checked"] = True
                    log(f"  {index}/{len(selected)}: {'pronađen' if result else 'nije potvrđen'} — {prospect.get('name', '')}")
            finally:
                browser.close()
    except Exception as error:
        # Missing browser binaries and launch failures should never fail prospecting.
        log(f"Obrtni registar nedostupan ({type(error).__name__}); koristim ručne poveznice.")
