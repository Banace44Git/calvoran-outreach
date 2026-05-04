"""Exportiert verwertbare Leads als CSV.

Default-Filter: in_target_cities=true, excluded=false.
Felder: Firmenname, Stellentitel, Standort, Remote-Flag, Link zur Anzeige, Ansprechpartner, Email, Telefon, Quelle, Datum.
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._db import get_client

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR.mkdir(exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Auch excluded und non-target")
    parser.add_argument("--remote-only", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    db = get_client()
    q = db.table("leads").select("*").order("firmenname")
    if not args.all:
        q = q.eq("in_target_cities", True).eq("excluded", False)
    if args.remote_only:
        q = q.eq("remote", True)

    leads = q.execute().data
    print(f"{len(leads)} Leads zum Export.")

    out = args.out or OUT_DIR / f"{date.today().isoformat()}_leads.csv"

    fields = [
        "firmenname", "stellentitel", "standort", "remote",
        "link", "ansprechpartner", "ansprechpartner_email", "ansprechpartner_phone",
        "source", "posted_date", "in_target_cities", "excluded",
    ]

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead)

    print(f"→ {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
