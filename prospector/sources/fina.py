"""Revenue and employee counts from FINA's public annual statements (RGFI).

RGFI is open data under the Croatian open licence, but its site sits behind a
login and a captcha, so it is not scraped. A person downloads the yearly CSV
files ("Datoteka objavljenih izvještaja") into data/fina/ and `prospector
import-fina` loads them. Until then revenue stays unknown.
"""
import csv
import io
import re
from pathlib import Path

from .. import db
from ..config import DATA_DIR

FINA_DIR = DATA_DIR / "fina"

OIB_COLS = re.compile(r"^oib$", re.I)
REVENUE_COLS = re.compile(r"ukupn\w* prihod|poslovn\w* prihod|^prihod", re.I)
EMPLOYEE_COLS = re.compile(r"broj zaposlen|zaposlen", re.I)
YEAR_COLS = re.compile(r"godina|razdoblje|^god$", re.I)


def _read(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1250"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def _number(value: str | None) -> float | None:
    if not value:
        return None
    v = value.strip().replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def import_files(progress=print) -> int:
    files = sorted(FINA_DIR.glob("*.csv"))
    if not files:
        progress(f"No CSV files in {FINA_DIR}")
        return 0
    with db.tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS fina (oib TEXT, year INTEGER, revenue REAL, employees REAL, "
            "PRIMARY KEY (oib, year))"
        )
    total = 0
    for path in files:
        rows = _read(path)
        if not rows:
            continue
        cols = rows[0].keys()
        oib_col = next((c for c in cols if OIB_COLS.search(c.strip())), None)
        rev_col = next((c for c in cols if REVENUE_COLS.search(c.strip())), None)
        emp_col = next((c for c in cols if EMPLOYEE_COLS.search(c.strip())), None)
        year_col = next((c for c in cols if YEAR_COLS.search(c.strip())), None)
        if not oib_col or not rev_col:
            progress(f"  {path.name}: could not find OIB/revenue columns in {list(cols)[:15]}")
            continue
        file_year = re.search(r"20\d\d", path.name)
        records = []
        for r in rows:
            oib = re.sub(r"\D", "", r.get(oib_col) or "").zfill(11)
            year = int(_number(r.get(year_col)) or 0) if year_col else int(file_year.group()) if file_year else 0
            records.append((oib, year, _number(r.get(rev_col)), _number(r.get(emp_col)) if emp_col else None))
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO fina VALUES (?, ?, ?, ?)", records)
        progress(f"  {path.name}: {len(records)} rows (revenue column: {rev_col!r})")
        total += len(records)
    return total


def latest(oib: str | None) -> dict | None:
    if not oib:
        return None
    try:
        row = db.conn().execute(
            "SELECT year, revenue, employees FROM fina WHERE oib=? AND revenue IS NOT NULL ORDER BY year DESC LIMIT 1",
            (oib,),
        ).fetchone()
    except Exception:  # table not imported yet
        return None
    return dict(row) if row else None
