"""Local Streamlit interface for running a prospect search and exporting results."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import streamlit as st

from . import output, pipeline
from .config import categories, zones
from .sources import sudreg


def _area_options() -> list[tuple[str, int, str | None]]:
    options = []
    for zone_id, zone in zones().items():
        options.append((f"Zona {zone_id} — {zone['name']} (cijela zona)", zone_id, None))
        options.extend((f"{county} (Zona {zone_id})", zone_id, county) for county in zone["counties"])
    return options


def _infer_category(query: str) -> str | None:
    normalized = sudreg.normalize(query)
    aliases = {
        "rolete": ("rolete", "roleta", "zaluzine", "sjenila"),
        "tende": ("tende", "tenda", "pergole", "pergola", "markize"),
        "stolarija": ("stolarija", "pvc", "alu", "prozori"),
        "garazna_vrata": ("garazna vrata", "garazna", "garaznih vrata"),
        "dvorisna_vrata": ("dvorisna vrata", "ograde", "bravarija", "klizna vrata"),
        "zastita": ("tehnicka zastita", "zastita", "alarm"),
        "distributeri": ("distributeri", "veleprodaja", "distributer"),
    }
    matches = [
        (len(alias), key)
        for key, values in aliases.items()
        for alias in values
        if re.search(rf"(?:^| ){re.escape(sudreg.normalize(alias))}(?:$| )", normalized)
    ]
    return max(matches)[1] if matches else None


def _infer_area(query: str, options: list[tuple[str, int, str | None]]) -> int:
    normalized = sudreg.normalize(query)
    county_matches = [
        (len(sudreg.normalize(county)), index)
        for index, (_, _, county) in enumerate(options)
        if county and sudreg.normalize(county) in normalized
    ]
    if county_matches:
        return max(county_matches)[1]
    zone_match = re.search(r"\bzona\s*(\d+)\b", normalized)
    if zone_match:
        zone_id = int(zone_match.group(1))
        for index, (_, option_zone, county) in enumerate(options):
            if option_zone == zone_id and county is None:
                return index
    for index, (_, zone_id, county) in enumerate(options):
        zone_name = sudreg.normalize(zones()[zone_id]["name"])
        if county is None and zone_name in normalized:
            return index
    return 0


def main() -> None:
    st.set_page_config(page_title="Sales DiQ Prospector", page_icon="🔎", layout="wide")
    st.title("Sales DiQ — pretraga potencijalnih klijenata")
    st.write("Upišite proizvod i područje, primjerice **tende osječko-baranjska** ili **tende zona 1**.")

    options = _area_options()
    with st.form("prospect-search"):
        query = st.text_input("Što tražite?", placeholder="tende osječko-baranjska")
        category_keys = list(categories())
        inferred_category = _infer_category(query) if query else None
        category_index = category_keys.index(inferred_category) if inferred_category in category_keys else 0
        category_key = st.selectbox(
            "Kategorija", category_keys, index=category_index,
            format_func=lambda key: categories()[key]["label"],
        )
        area_index = _infer_area(query, options) if query else 0
        area_index = min(area_index, len(options) - 1)
        selected_area = st.selectbox("Područje", range(len(options)), index=area_index,
                                     format_func=lambda index: options[index][0])
        estimate_revenue = st.checkbox(
            "Dopusti web procjenu prometa ako nema javnog podatka (moguć dodatni trošak)",
            value=False,
        )
        lookup_obrti = st.checkbox(
            "Provjeri potencijalne obrte u službenom Obrtnom registru (do 15 pojedinačnih pretraga)",
            value=True,
            help="Koristi javno dostupnu pretragu službene stranice. Ako portal vrati CAPTCHA ili odbijanje pristupa, provjera se zaustavlja.",
        )
        update_sheet = st.checkbox("Ažuriraj i zajedničku Google tablicu", value=False)
        submitted = st.form_submit_button("Pokreni pretragu", type="primary")

    if not submitted:
        st.caption("Rezultati će uključiti kontakte, promet, broj zaposlenih, izvore i datum obrade gdje su podaci dostupni.")
        return

    _, zone_id, county = options[selected_area]
    processed_at = datetime.now().astimezone().isoformat(timespec="minutes")
    logs: list[str] = []
    log_area = st.empty()
    status = st.status("Pretraga je pokrenuta…", expanded=True)

    def log(message: str) -> None:
        logs.append(message)
        log_area.code("\n".join(logs[-12:]))

    try:
        prospects = pipeline.run_zone(
            zone_id,
            category_key,
            counties_filter=[county] if county else None,
            use_web_revenue=estimate_revenue,
            lookup_obrti=lookup_obrti,
            log=log,
        )
        for prospect in prospects:
            prospect["processed_at"] = processed_at
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"upit_{category_key}_zona{zone_id}_{stamp}"
        csv_path = Path(output.write_csv(prospects, base_name))
        xlsx_path = Path(output.write_xlsx(prospects, base_name))
        output.write_json(prospects, base_name)
        sheet_url = None
        if update_sheet:
            customer_keys = {output.row(p)["Ključ"] for p in prospects if p.get("status") != "New"}
            sheet_url = output.write_sheet(prospects, zones()[zone_id]["sheet_tab"], customer_keys)
        status.update(label=f"Gotovo — obrađeno {len(prospects)} tvrtki", state="complete", expanded=False)
    except Exception as error:
        status.update(label="Pretraga nije dovršena", state="error", expanded=True)
        st.exception(error)
        return

    st.success(f"Datum obrade: {processed_at}")
    if sheet_url:
        st.markdown(f"Google tablica: [otvori rezultate]({sheet_url.split(' (')[0]})")
    st.dataframe([output.row(p) for p in prospects], use_container_width=True, hide_index=True)
    csv_col, excel_col = st.columns(2)
    csv_col.download_button("Preuzmi CSV", csv_path.read_bytes(), file_name=csv_path.name, mime="text/csv")
    excel_col.download_button(
        "Preuzmi Excel", xlsx_path.read_bytes(), file_name=xlsx_path.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    st.caption(f"Datoteke su spremljene i u mapi output/: {csv_path.name} i {xlsx_path.name}")


if __name__ == "__main__":
    main()
