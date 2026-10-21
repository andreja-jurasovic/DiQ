"""Command line: python -m prospector <command>

  sync-register                         download the court register (weekly is plenty)
  run --zone 1 --category rolete        find, qualify and score prospects for a zone
  company "Windor d.o.o." --town Buk    research one company right now
  import-fina                           load FINA CSV files from data/fina/
"""
import argparse
import json
from datetime import datetime

from . import output, pipeline, qualify
from .config import categories, zones
from .sources import fina, sudreg, web_revenue


def main() -> None:
    ap = argparse.ArgumentParser(prog="prospector", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-register")
    sub.add_parser("import-fina")

    run = sub.add_parser("run")
    run.add_argument("--zone", type=int, required=True, choices=sorted(zones()))
    run.add_argument("--category", required=True, choices=sorted(categories()))
    run.add_argument("--towns", type=int, help="only the first N towns per county (faster test runs)")
    run.add_argument("--limit", type=int, help="qualify at most N companies with Claude")
    run.add_argument("--sources", default="places,emajstor,register")
    run.add_argument("--no-sheet", action="store_true", help="write CSV only")
    run.add_argument("--no-web-revenue", action="store_true", help="skip web-search revenue estimates")

    one = sub.add_parser("company")
    one.add_argument("name")
    one.add_argument("--town")
    one.add_argument("--website")
    one.add_argument("--category", choices=sorted(categories()))
    one.add_argument("--zone", type=int, choices=sorted(zones()), help="also write the row to this zone's tab")
    one.add_argument("--no-web-revenue", action="store_true")

    args = ap.parse_args()

    if args.cmd == "sync-register":
        sudreg.sync()
    elif args.cmd == "import-fina":
        print(f"{fina.import_files()} rows imported")
    elif args.cmd == "run":
        prospects = pipeline.run_zone(
            args.zone, args.category, max_towns=args.towns, limit=args.limit,
            use_web_revenue=not args.no_web_revenue, sources=set(args.sources.split(",")),
        )
        _publish(prospects, args.zone, f"zona{args.zone}_{args.category}", args.no_sheet)
    elif args.cmd == "company":
        p = pipeline.run_company(args.name, args.town, args.website, args.category, not args.no_web_revenue)
        r = output.row(p)
        print(json.dumps({k: v for k, v in r.items() if v not in ("", None)}, ensure_ascii=False, indent=2))
        if args.zone:
            _publish([p], args.zone, None, False)
        else:
            _print_cost()


def _publish(prospects: list[dict], zone_id: int, csv_name: str | None, no_sheet: bool) -> None:
    if csv_name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        print("CSV:", output.write_csv(prospects, f"{csv_name}_{stamp}"))
        output.write_json(prospects, f"{csv_name}_{stamp}")
    if not no_sheet:
        customer_keys = {output.row(p)["Ključ"] for p in prospects if p.get("status") != "New"}
        try:
            print("Sheet:", output.write_sheet(prospects, zones()[zone_id]["sheet_tab"], customer_keys))
        except RuntimeError as e:
            print("Sheet not updated:", e)
    top = [p for p in prospects if p.get("score") is not None][:10]
    print("\nTop prospects:")
    for p in top:
        print(f"  {p['score']:>3}  P{p['tier']}  {p.get('legal_name') or p['name']}  ({p.get('town') or ''})  {p.get('status')}")
    _print_cost()


def _print_cost() -> None:
    u, w = qualify.usage, web_revenue.usage
    print(
        f"\nClaude: {u['calls']} qualifications (${qualify.cost_usd():.2f}) + "
        f"{w['calls']} revenue lookups, {w['searches']} searches (${web_revenue.cost_usd():.2f}); "
        f"total about ${qualify.cost_usd() + web_revenue.cost_usd():.2f}"
    )


if __name__ == "__main__":
    main()
