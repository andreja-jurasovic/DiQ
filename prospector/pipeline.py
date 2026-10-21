"""Discover -> resolve -> enrich -> qualify -> score, for one zone and category
or for a single named company."""
import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

from . import http, qualify, revenue, scoring, website
from .config import ROOT, categories, general_keywords, scoring as scoring_config, zones
from .sources import emajstor, obrtni_registar, places, sudreg

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "net.hr", "t-com.hr", "inet.hr", "live.com", "icloud.com"}


def _county_stem(county: str | None) -> str:
    return sudreg.normalize(county or "").split(" ")[0] if county else ""


def _in_zone(county: str | None, zone_counties: list[str]) -> bool:
    stem = _county_stem(county)
    return bool(stem) and any(stem == _county_stem(c) for c in zone_counties)


def _domain(url: str | None) -> str | None:
    url = website.normalize_url(url)
    return urlparse(url).netloc.removeprefix("www.").lower() if url else None


def _website_from_emails(emails: list[str]) -> str | None:
    """A company email on its own domain (info@firma.hr) usually means firma.hr exists."""
    for e in emails:
        domain = e.split("@")[-1].lower()
        if domain not in FREE_MAIL and not domain.endswith(("t-com.hr", "htnet.hr")):
            return f"https://{domain}"
    return None


# ---------------- discovery ----------------

def discover(zone_id: int, category: str, *, max_towns: int | None, sources: set[str],
             counties_filter: list[str] | None = None, log=print) -> list[dict]:
    zone = zones()[zone_id]
    cat = categories()[category]
    counties = [c for c in zone["counties"] if not counties_filter or c in counties_filter]
    if not counties:
        raise ValueError(f"No counties in zone {zone_id} match the requested area")
    towns = [
        (town, county)
        for county, county_towns in zone["counties"].items()
        if county in counties
        for town in (county_towns[:max_towns] if max_towns is not None else county_towns)
    ]
    found: list[dict] = []

    if "places" in sources:
        queries = [f"{kw} {town}" for town, _ in towns for kw in cat["places"]]
        log(f"Google Places: {len(queries)} searches")
        with ThreadPoolExecutor(4) as pool:
            for results in pool.map(places.search, queries):
                found.extend(r for r in results if _in_zone(r["county"], counties))

    if "emajstor" in sources and cat["emajstor"]:
        log(f"emajstor.hr: {len(cat['emajstor']) * len(towns)} directory pages")
        entries: dict[str, dict] = {}
        try:
            for slug in cat["emajstor"]:
                for town, _ in towns:
                    for entry in emajstor.listing(slug, town, max_pages=2):
                        entries.setdefault(entry["profile_url"], entry)
            # listings mix in companies from elsewhere; the profile page has the county
            for url, entry in entries.items():
                prof = emajstor.profile(url)
                item = {"source": "emajstor", "source_id": url, "phones": []} | entry | {k: v for k, v in prof.items() if v}
                if _in_zone(item.get("county"), counties):
                    found.append(item)
        except http.Blocked as e:
            log(f"  emajstor.hr stopped early: {e}")

    if "register" in sources:
        rows = sudreg.search(counties, cat["nkd"], cat["register_name"])
        log(f"Court register: {len(rows)} companies by activity code / registered name")
        for r in rows:
            found.append(_from_register({"source": "register", "source_id": r["oib"]}, r))
    return found


def _from_register(p: dict, r: dict) -> dict:
    p.update({
        "oib": r["oib"],
        "legal_name": r["short_name"] or r["full_name"],
        "name": p.get("name") or r["short_name"] or r["full_name"],
        "registered_name": r["full_name"],
        "county": p.get("county") or r["county"],
        "town": p.get("town") or r["town"],
        "register_address": f"{r['street']}, {r['town']}" if r["street"] else r["town"],
        "nkd_name": r["nkd_name"],
        "founded": (r["datum_osnivanja"] or "")[:4],
        "register_emails": [e.strip().lower() for e in (r["emails"] or "").split(",") if e.strip()],
        "blocked": r["postupak"] not in (1, None),
    })
    return p


# ---------------- resolve & merge ----------------

def resolve_and_merge(found: list[dict], log=print) -> list[dict]:
    for p in found:
        if not p.get("oib"):
            m = sudreg.match(p["name"], p.get("county"), p.get("town"))
            if m:
                _from_register(p, m)

    # Union records through shared identity keys. Unlike the old one-pass alias
    # map, this also joins groups when a later hit bridges two earlier hits.
    # Refuse to combine groups with different known OIBs: shared domains and
    # trading names sometimes belong to separate legal entities.
    parent = list(range(len(found)))
    group_oibs = [{str(p["oib"])} if p.get("oib") else set() for p in found]

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(left: int, right: int) -> None:
        a, b = root(left), root(right)
        if a == b or (group_oibs[a] and group_oibs[b] and group_oibs[a] != group_oibs[b]):
            return
        parent[b] = a
        group_oibs[a].update(group_oibs[b])

    buckets: dict[tuple[str, str], list[int]] = {}
    for i, p in enumerate(found):
        if p.get("oib"):
            buckets.setdefault(("oib", str(p["oib"])), []).append(i)
        town = sudreg.normalize(p.get("town") or "")
        for name in {p.get("name"), p.get("legal_name"), p.get("registered_name")}:
            normalized = sudreg.normalize(name or "")
            if normalized:
                buckets.setdefault(("name", f"{normalized}|{town}"), []).append(i)
        domain = _domain(p.get("website"))
        if domain:
            buckets.setdefault(("domain", domain), []).append(i)

    # Names plus town are stronger evidence than a shared website domain.
    for kind in ("oib", "name", "domain"):
        for (bucket_kind, _), indexes in buckets.items():
            if bucket_kind != kind or len(indexes) < 2:
                continue
            distinct_oibs = {str(found[i]["oib"]) for i in indexes if found[i].get("oib")}
            if len(distinct_oibs) > 1:
                continue  # conflicting registered identities make this key ambiguous
            first = indexes[0]
            for i in indexes[1:]:
                union(first, i)

    groups: dict[int, list[dict]] = {}
    for i, p in enumerate(found):
        groups.setdefault(root(i), []).append(p)

    merged = []
    for records in groups.values():
        rec = {"sources": set(), "phones": [], "emails": [], "name_from_register": False}
        for p in records:
            rec["sources"].add(p["source"])
            for field in ["oib", "legal_name", "registered_name", "county", "town", "register_address", "nkd_name",
                          "founded", "blocked", "website", "maps_url", "google_category", "rating", "reviews",
                          "profile_url", "contact_name", "address"]:
                if p.get(field) not in (None, "", []) and not rec.get(field):
                    rec[field] = p[field]
            if p["source"] == "emajstor":
                rec["emajstor_services"] = p.get("services")
                rec["emajstor_description"] = (p.get("description") or "")[:3000]
            # Prefer the public-facing listing name over the court-register name.
            if not rec.get("name") or (rec["name_from_register"] and p["source"] != "register"):
                rec["name"] = p["name"]
                rec["name_from_register"] = p["source"] == "register"
            rec["phones"] += [website.normalize_phone(x) for x in p.get("phones", [])]
            rec["emails"] += p.get("emails", [])
            rec["emails"] += p.get("register_emails", [])
        rec["phones"] = list(dict.fromkeys(rec["phones"]))
        rec["emails"] = list(dict.fromkeys(e.lower() for e in rec["emails"] if e and "@" in e))
        merged.append(rec)
    log(f"{len(found)} hits -> {len(merged)} unique companies ({sum(1 for r in merged if r.get('oib'))} with OIB)")
    return merged


def load_customers() -> list[dict]:
    path = ROOT / "customers.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def apply_customers(prospects: list[dict], customers: list[dict]) -> None:
    by_oib = {c["oib"]: c for c in customers if c.get("oib")}
    by_name = {sudreg.normalize(c["name"]): c for c in customers}
    for p in prospects:
        names = (p.get("name"), p.get("legal_name"), p.get("registered_name"))
        c = by_oib.get(p.get("oib") or "") or next(
            (by_name[sudreg.normalize(name)] for name in names
             if name and sudreg.normalize(name) in by_name),
            None,
        )
        p["status"] = (c or {}).get("status") or "New"


# ---------------- enrich, qualify, score ----------------

def _phone_str(phones: list[str]) -> str:
    uniq = list(dict.fromkeys(phones))
    mobiles = [x for x in uniq if website.is_mobile(x)]
    return " / ".join(mobiles + [x for x in uniq if x not in mobiles][:3])


GENERIC_WORDS = {
    "pvc", "alu", "stolarija", "stolarije", "stolar", "rolete", "roleta", "roletarski", "roletarstvo", "servis",
    "montaza", "monting", "vrata", "prozori", "prozora", "garazna", "bravarija", "bravarski", "tende", "sjenila",
    "trgovina", "usluge", "proizvodnja", "gradnja", "obrt", "plast", "plastika", "centar", "tvornica", "i", "za",
}


def _distinctive(name: str) -> set[str]:
    return {t for t in sudreg.normalize(name).split() if len(t) >= 3 and t not in GENERIC_WORDS}


def _fill_from_places(p: dict) -> None:
    """Companies found on emajstor.hr or in the court register often have no phone.
    Look the company up on Google Maps by name and town and take its public phone
    and website, if the listing clearly is the same company."""
    query = f"{p.get('legal_name') or p['name']} {p.get('town') or ''}".strip()
    distinctive = _distinctive(p.get("legal_name") or p["name"])
    if not distinctive:
        return  # a name like "PVC stolarija" alone can't identify a company
    for hit in places.search(query, max_pages=1)[:3]:
        if not distinctive <= _distinctive(hit["name"]) | set(sudreg.normalize(hit["name"]).split()):
            continue
        if p.get("county") and hit.get("county") and not _in_zone(hit["county"], [p["county"]]):
            continue
        p["phones"] += [website.normalize_phone(x) for x in hit["phones"]]
        p["website"] = p.get("website") or hit.get("website")
        p["maps_url"] = p.get("maps_url") or hit.get("maps_url")
        p["sources"].add("google_places")
        return


def enrich_one(p: dict, category: str | None, use_web_revenue: bool = True) -> dict:
    cats = categories()
    general = general_keywords()
    if not p["phones"]:
        _fill_from_places(p)
    if not p.get("website"):
        p["website"] = _website_from_emails(p["emails"])
    site = website.read(p.get("website"))
    p["website_ok"] = site.get("ok", False)
    if site.get("ok"):
        p["website"] = site["url"]
        p["emails"] += site["emails"]
        p["phones"] += site["phones"]
    elif p.get("website") and site.get("reason") == "unreachable":
        p["website"] = None  # guessed from an email domain and not there
    text = " ".join(filter(None, [site.get("text"), p.get("emajstor_services"), p.get("emajstor_description")]))
    p["general_keywords"] = website.keywords_found(text, general)
    p["category_keywords"] = website.keywords_found(text, cats[category]["site_keywords"]) if category else []
    p["emails"] = list(dict.fromkeys(e for e in p["emails"] if "@" in e))
    p["phones"] = list(dict.fromkeys(p["phones"]))

    # free sources first; the paid web search waits until Claude has judged relevance
    p.update(revenue=None, revenue_year=None, employees=None, revenue_source=None, revenue_url=None)
    profile_data = revenue.find(
        p.get("oib"), p.get("legal_name") or p["name"], p.get("town"), allow_paid=False
    ) or {}
    for key in ("revenue", "revenue_year", "employees", "revenue_source", "revenue_url"):
        if key in profile_data:
            p[key] = profile_data[key]
    p["emails"] = list(dict.fromkeys(
        [e.lower() for e in p.get("emails", []) if e]
        + [e.lower() for e in profile_data.get("emails", []) if e]
    ))
    p["phones"] = list(dict.fromkeys(
        [website.normalize_phone(x) for x in p.get("phones", []) if x]
        + [website.normalize_phone(x) for x in profile_data.get("phones", []) if x]
    ))
    p["contacts"] = list(p.get("contacts", [])) + profile_data.get("contacts", [])
    if not p.get("contact_name"):
        p["contact_name"] = profile_data.get("contact_name")
    if not p.get("website"):
        p["website"] = profile_data.get("company_website")
    if profile_data.get("revenue_url"):
        p.setdefault("sources", set()).add("companywall")

    q = qualify.qualify(p, site)
    if q:
        p.update(segment=q.segment, ai_categories=q.categories, is_relevant=q.is_relevant,
                 relevance_reason=q.relevance_reason, mentions_motorization=q.mentions_motorization,
                 brands=q.brands_mentioned)
        p["contacts"] = list(p.get("contacts", [])) + [c.model_dump() for c in q.contacts]
        for c in q.contacts:
            if c.email:
                p["emails"].append(c.email.lower())
            if c.phone:
                p["phones"].append(website.normalize_phone(c.phone))
        p["emails"] = list(dict.fromkeys(p["emails"]))
        p["phones"] = list(dict.fromkeys(p["phones"]))
    # Spend on an estimate only after Claude positively qualifies the prospect.
    if use_web_revenue and p["revenue"] is None and p.get("is_relevant") is True:
        paid_result = revenue.find(p.get("oib"), p.get("legal_name") or p["name"], p.get("town")) or {}
        for key in ("revenue", "revenue_year", "employees", "revenue_source", "revenue_url"):
            if key in paid_result:
                p[key] = paid_result[key]
    p["obrtni_registar"] = obrt_lookup_hint(p)
    p["score"], p["tier"], p["score_reasons"] = scoring.score(p, category)
    p["phone_display"] = _phone_str(p["phones"])
    return p


def obrt_lookup_hint(p: dict) -> str:
    """Return a targeted link for manual verification in the official craft register.

    The public portal permits individual searches, but does not expose a supported
    public bulk/API interface. Keep this as an explicit human verification step;
    absence from Sudski registar alone does not prove that a lead is an obrt.
    """
    if p.get("oib"):
        return ""
    if p.get("obrt_registry_name"):
        return (f"Potvrđen obrt — {p.get('obrt_registry_status', 'status nije naveden')}; "
                f"{p.get('obrt_registry_url', 'https://pretrazivac-obrta.gov.hr/')}")
    name = p["name"]
    owner = next((c["name"] for c in p.get("contacts", []) if c.get("role") and "vlasni" in c["role"].lower()), None)
    owner = owner or p.get("contact_name")
    m = re.search(r"\bvl(?:\.|asnik|asnica)\s*:?\s*([^,;]+)", name, re.I)
    if m:
        owner = owner or m.group(1).strip()
        name = name[: m.start()].rstrip(" ,")
    parts = [f"naziv: {name}"]
    if owner:
        parts.append(f"vlasnik: {owner}")
    if p.get("town"):
        parts.append(f"mjesto: {p['town']}")
    query = "; ".join(parts)
    url = "https://pretrazivac-obrta.gov.hr/pretraga.htm"
    state = "Nije potvrđen automatski — " if p.get("obrt_registry_checked") else "Provjeriti pojedinačno — "
    return f"{state}{query} — {url}"


def enrich_all(prospects: list[dict], category: str | None, use_web_revenue: bool = True,
               workers: int = 6, log=print) -> list[dict]:
    done = []
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(enrich_one, p, category, use_web_revenue): p for p in prospects}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                done.append(fut.result())
            except Exception as e:  # one bad website must not stop the run
                p["error"] = f"{type(e).__name__}: {e}"
                p["score"], p["tier"], p["score_reasons"] = scoring.score(p, category)
                p["phone_display"] = _phone_str(p["phones"])
                done.append(p)
            if i % 10 == 0 or i == len(prospects):
                log(f"  qualified {i}/{len(prospects)}")
    done.sort(key=lambda p: (p.get("score") is None, -(p.get("score") or 0)))
    return done


def _pre_rank_key(p: dict) -> tuple:
    """Cheap ordering for --limit: use imported FINA and already-known lead signals."""
    if p.get("oib"):
        record = revenue.fina.latest(p["oib"])
        if record:
            p.update(revenue=record["revenue"], revenue_year=record["year"], employees=record["employees"])
    cfg = scoring_config()
    value = p.get("revenue")
    revenue_points = cfg["unknown_revenue_points"] if value is None else 0
    if value is not None:
        revenue_points = max(
            (band["points"] for band in cfg["revenue_bands"]
             if value >= band["min"] and (band["max"] is None or value < band["max"])),
            default=0,
        )
    return (
        -revenue_points,
        -len(p.get("sources", [])),
        not bool(p.get("website")),
        not any(website.is_mobile(phone) for phone in p.get("phones", [])),
        not bool(p.get("emails")),
        not bool(p.get("oib")),
    )


# ---------------- entry points ----------------

def run_zone(zone_id: int, category: str, *, max_towns=None, limit=None, use_web_revenue=True,
             sources=frozenset({"places", "emajstor", "register"}), counties_filter=None,
             lookup_obrti=False, max_obrt_lookups=15, log=print) -> list[dict]:
    if not sudreg.is_synced():
        log("Downloading the court register (first run only)...")
        sudreg.sync(log)
    found = discover(zone_id, category, max_towns=max_towns, sources=set(sources),
                     counties_filter=counties_filter, log=log)
    prospects = resolve_and_merge(found, log)
    apply_customers(prospects, load_customers())
    prospects = [p for p in prospects if not p.get("blocked")]
    # cheap pre-rank so --limit keeps the most promising before paying for Claude
    prospects.sort(key=_pre_rank_key)
    if limit is not None:
        prospects = prospects[:limit]
    if lookup_obrti:
        obrtni_registar.enrich(prospects, limit=max_obrt_lookups, log=log)
    processed_at = datetime.now().astimezone().isoformat(timespec="minutes")
    for prospect in prospects:
        prospect["processed_at"] = processed_at
    log(f"Reading websites and qualifying {len(prospects)} companies with Claude...")
    return enrich_all(prospects, category, use_web_revenue, log=log)


def run_company(name: str, town: str | None = None, website_url: str | None = None,
                category: str | None = None, use_web_revenue: bool = True, log=print) -> dict:
    if not sudreg.is_synced():
        sudreg.sync(log)
    query = f"{name} {town or ''}".strip()
    hits = places.search(query, max_pages=1)[:1]
    base = {"source": "manual", "name": name, "town": town, "website": website_url, "phones": []}
    if hits:
        h = hits[0]
        base.update({k: v for k, v in h.items() if v and k not in ("source", "source_id", "name")})
        base["phones"] = h["phones"]
    m = sudreg.match(name, base.get("county"), base.get("town")) or sudreg.match(name)
    if m:
        _from_register(base, m)
    [p] = resolve_and_merge([base], log=lambda *_: None)
    apply_customers([p], load_customers())
    return enrich_one(p, category, use_web_revenue)
