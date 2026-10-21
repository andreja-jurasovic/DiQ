"""Sudski registar open-data API (sudreg-data.gov.hr).

The API has no name search, so the register is downloaded in bulk into SQLite
(about 190k companies, under a minute) and matched locally. Obrti are not in
the court register; they are matched by other sources.
"""
import re
import time
import unicodedata
from difflib import SequenceMatcher

import httpx

from .. import db
from ..config import env

BASE = "https://sudreg-data.gov.hr/api"
PAGE = 10000

# table -> columns kept locally
TABLES = {
    "subjekti": ["mbs", "status", "oib", "glavna_djelatnost", "datum_osnivanja", "postupak"],
    "tvrtke": ["mbs", "ime"],
    "skracene_tvrtke": ["mbs", "ime"],
    "sjedista": ["mbs", "naziv_zupanije", "naziv_opcine", "naziv_naselja", "ulica", "kucni_broj", "kucni_podbroj"],
    "email_adrese": ["mbs", "adresa"],
    "nacionalna_klasifikacija_djelatnosti": ["id", "sifra", "puni_naziv", "verzija"],
}

_token = {"value": None, "expires": 0.0}


def _auth_header() -> dict:
    if time.time() > _token["expires"] - 60:
        r = httpx.post(
            f"{BASE}/oauth/token",
            auth=(env("SUDREG_CLIENT_ID"), env("SUDREG_CLIENT_SECRET")),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        _token.update(value=body["access_token"], expires=time.time() + body["expires_in"])
    return {"Authorization": f"Bearer {_token['value']}"}


def _get(path: str, **params):
    for attempt in range(4):
        r = httpx.get(f"{BASE}/javni/{path}", params=params, headers=_auth_header(), timeout=120)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()


def sync(progress=print) -> None:
    """Download the register tables into SQLite (replaces the previous copy)."""
    conn = db.conn()
    snapshot = _get("subjekti", offset=0, limit=1).headers.get("X-Snapshot-Id")
    for table, cols in TABLES.items():
        rows, offset = [], 0
        while True:
            params = {"offset": offset, "limit": PAGE}
            if snapshot:
                params["snapshot_id"] = snapshot
            page = _get(table, **params).json()
            rows.extend(tuple(item.get(c) for c in cols) for item in page)
            if len(page) < PAGE:
                break
            offset += PAGE
        with db.tx() as c:
            c.execute(f"DROP TABLE IF EXISTS sr_{table}")
            c.execute(f"CREATE TABLE sr_{table} ({', '.join(cols)})")
            c.executemany(f"INSERT INTO sr_{table} VALUES ({', '.join('?' * len(cols))})", rows)
            if "mbs" in cols:
                c.execute(f"CREATE INDEX idx_sr_{table}_mbs ON sr_{table}(mbs)")
        progress(f"  {table}: {len(rows)} rows")
    with db.tx() as c:
        c.execute("DROP TABLE IF EXISTS sr_companies")
        c.execute(
            """
            CREATE TABLE sr_companies AS
            SELECT s.mbs, printf('%011d', s.oib) AS oib, s.status, s.postupak, s.datum_osnivanja,
                   t.ime AS full_name, st.ime AS short_name,
                   n.sifra AS nkd, n.puni_naziv AS nkd_name,
                   j.naziv_zupanije AS county, j.naziv_naselja AS town,
                   trim(coalesce(j.ulica, '') || ' ' || coalesce(j.kucni_broj, '') || coalesce(j.kucni_podbroj, '')) AS street,
                   (SELECT group_concat(e.adresa, ', ') FROM sr_email_adrese e WHERE e.mbs = s.mbs) AS emails
            FROM sr_subjekti s
            LEFT JOIN sr_tvrtke t ON t.mbs = s.mbs
            LEFT JOIN sr_skracene_tvrtke st ON st.mbs = s.mbs
            LEFT JOIN sr_sjedista j ON j.mbs = s.mbs
            -- glavna_djelatnost is the NKD 2007 code without the dot (4321 -> 43.21)
            LEFT JOIN sr_nacionalna_klasifikacija_djelatnosti n
                   ON n.verzija = 'NKD 2007'
                  AND n.sifra = substr(printf('%04d', s.glavna_djelatnost), 1, 2) || '.' || substr(printf('%04d', s.glavna_djelatnost), 3, 2)
            """
        )
        c.execute("CREATE INDEX idx_sr_companies_county ON sr_companies(county)")
        c.execute("CREATE INDEX idx_sr_companies_oib ON sr_companies(oib)")
    progress("  sr_companies view built")


def is_synced() -> bool:
    return bool(
        db.conn().execute("SELECT name FROM sqlite_master WHERE name='sr_companies'").fetchone()
    )


# ---------- name matching ----------

LEGAL_FORMS = r"\b(j\.?\s?d\.?\s?o\.?\s?o\.?|d\.?\s?o\.?\s?o\.?|d\.?\s?d\.?|obrt za .*|obrt\b|vl\.\s.*|vlasni(k|ca)\b.*|društvo s ograničenom odgovornošću.*|jednostavno društvo.*)"


def normalize(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(LEGAL_FORMS, " ", s)
    s = unicodedata.normalize("NFKD", s).replace("đ", "d")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    common = len(ta & tb)
    # one name fully inside the other counts only for multi-word names
    containment = max(
        common / len(ta) if len(ta) >= 2 else 0,
        common / len(tb) if len(tb) >= 2 else 0,
    )
    return max(ratio, 0.9 * containment)


def by_oib(oib: str):
    row = db.conn().execute("SELECT * FROM sr_companies WHERE oib=?", (oib,)).fetchone()
    return dict(row) if row else None


_county_index: dict[str, list] = {}


def _index(county: str | None) -> list:
    """Normalized names of a county's companies, built once per process."""
    key = county.split()[0] if county else "*"
    if key not in _county_index:
        sql, params = "SELECT * FROM sr_companies WHERE postupak=1", []
        if county:
            sql += " AND county LIKE ?"
            params.append(f"{key}%")
        entries = []
        for row in db.conn().execute(sql, params):
            short, full = normalize(row["short_name"]), normalize(row["full_name"])
            entries.append((dict(row), short, full, set(short.split()) | set(full.split())))
        _county_index[key] = entries
    return _county_index[key]


def match(name: str, county: str | None = None, town: str | None = None, threshold: float = 0.86):
    """Best register match for a company name within a county (or nationwide)."""
    target = normalize(name)
    if not target:
        return None
    tokens = {t for t in target.split() if len(t) >= 3} or set(target.split())
    town_norm = normalize(town) if town else None
    best, best_score = None, 0.0
    for row, short, full, row_tokens in _index(county):
        if not tokens & row_tokens:
            continue
        score = max(_similarity(target, short), _similarity(target, full))
        if town_norm and row["town"] and town_norm == normalize(row["town"]):
            score += 0.05
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= threshold:
        return best | {"match_score": round(min(best_score, 1.0), 2)}
    return None


def search(counties: list[str], nkd_codes: list[str], name_keywords: list[str]):
    """Register companies in the given counties whose main activity is one of the NKD
    codes or whose full registered name mentions one of the keywords.

    Two thirds of companies have no main activity recorded, but the full name usually
    states the business ("... za proizvodnju i montažu plastične stolarije").
    """
    county_sql = " OR ".join("county LIKE ?" for _ in counties)
    nkd_sql = " OR ".join("nkd = ?" for _ in nkd_codes) or "0"
    # SQLite LIKE ignores case only for ASCII, so non-ASCII stems are tried in both cases
    variants = sorted({v for k in name_keywords for v in (k.lower(), k.upper())})
    name_sql = " OR ".join("full_name LIKE ?" for _ in variants) or "0"
    sql = (
        f"SELECT * FROM sr_companies WHERE postupak=1 AND ({county_sql}) "
        f"AND (({nkd_sql}) OR ({name_sql}))"
    )
    params = (
        [f"{c.split()[0]}%" for c in counties]
        + list(nkd_codes)
        + [f"%{v}%" for v in variants]
    )
    return [dict(r) for r in db.conn().execute(sql, params)]
