# Prospector: on-demand sales research for BFT-Adria

Finds installers, window makers, bravarije and distributors in a sales zone, reads
their websites, has Claude classify each prospect, scores them by the client's
priorities, and writes the result to Google Sheets (one tab per zone) and CSV.

## Setup (once)

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```
Secrets go in `.env` (never share or commit it). The Google service-account key goes
in `secrets/google-service-account.json`, and the target Google Sheet must be shared
(Editor) with the service account's email.

## Use

```
# Local browser interface: enter a phrase such as "tende osjecko baranjska"
# or "tende zona 1", review the detected category/area, then download Excel/CSV.
.venv/bin/streamlit run app.py

# whole zone, one product category
.venv/bin/python -m prospector run --zone 1 --category rolete

# quick test: first town per county, at most 15 companies, CSV only
.venv/bin/python -m prospector run --zone 1 --category rolete --towns 1 --limit 15 --no-sheet

# one company, right before a call (add --zone 1 to also write it to the sheet)
.venv/bin/python -m prospector company "Windor d.o.o." --town Pleternica --category stolarija

# refresh the court register (weekly is plenty)
.venv/bin/python -m prospector sync-register

# load FINA revenue files placed in data/fina/
.venv/bin/python -m prospector import-fina
```
Categories: rolete, tende, stolarija, garazna_vrata, dvorisna_vrata, zastita, distributeri.

The browser interface can search a whole zone or one county. It creates a full
Excel and CSV export in `output/`, including a `Datum obrade` timestamp for each
run. Updating the shared Google Sheet is optional in the interface. Web-search
revenue estimates are off by default there; FINA and public CompanyWall figures
are still included when available.

## How it works

1. **Discover**: Google Places (category keyword × town), emajstor.hr directory pages,
   and the court register (activity code or registered name, by county).
2. **Resolve**: every hit is matched to an OIB in a local copy of the court register,
   duplicates are merged, and companies in `customers.csv` get their status.
3. **Enrich**: up to 4 pages of the company website are read (emails, phones, keywords);
   revenue comes from FINA files when imported, then from a public CompanyWall profile
   matched by OIB. CompanyWall results include the reported year and profile link and
   are cached for 180 days. For relevant companies still missing a figure, Claude can
   make a web-search estimate shown as "~2.9M EUR" with "procjena (web)" and its source
   link; this estimate is cached for 180 days and can be skipped with `--no-web-revenue`.
   Leads without a court-register match can be checked one at a time through the
   official Obrtni registar search form using Playwright. The lookup is capped at
   15 records per run, checks name/owner/location before accepting a match, and
   extracts the craft status, MBO, address, activity, owner's name, public email,
   website, and record link. It stops on CAPTCHA/access denial; unmatched records
   retain a manual search link. Owner OIB is not copied into the sales output.
4. **Qualify**: Claude returns segment, fit, installation/motorization signals, product
   categories, brands and named contacts as schema-checked JSON. About $0.02 per company.
5. **Score**: weighted score normalized to 0-100 from `config/scoring.yaml`. Revenue
   score bands follow the brief's €25k and €250k–€8m priorities; numbered tiers follow
   the supplied `Prioriteti` worksheet and remain a separate classification. The
   worksheet leaves €50k–€100k new companies unassigned; this range is treated as P4.
6. **Output**: Google Sheet tab per zone (upsert by OIB or name/town; salesperson-edited
   Status and Bilješke are preserved) and `output/*.csv`.

## Configuration

- `config/zones.yaml`: zones, counties and the towns searched
- `config/categories.yaml`: search phrases, emajstor slugs, NKD codes, website keywords
- `config/scoring.yaml`: points and tiers
- `customers.csv`: existing customers (name, oib, town, county, status, ...)

Places results are cached for 30 days, website pages for 14 days, and CompanyWall and
web-search revenue lookups for 180 days in `cache/prospector.db`. Qualification results
are cached for 60 days. The project currently searches Google Places, eMajstor and the
court register; Facebook is not an automated discovery source.

## Data sources and limits

- Sudski registar open API: free, official. Obrti are not in it.
- Obrtni registar: individual searches and extracts are public and free. This app
  performs only limited individual lookups through the regular public form; it does
  not download a bulk registry dataset. It stops rather than bypassing any CAPTCHA
  or access-denied response. For Streamlit hosting, install the Playwright Chromium
  browser as part of the deployment build (`playwright install chromium`).
- FINA RGFI: open data, but the download sits behind a login and captcha, so a person
  downloads the yearly CSV into `data/fina/`. Until then revenue shows "Nema podatka".
- emajstor.hr: public directory and profile pages only, slowly, respecting robots.txt.
- trebam.hr does not publish company listings (it forwards customer requests to
  contractors), so it can't be used for discovery.
