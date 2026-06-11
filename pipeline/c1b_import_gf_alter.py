"""Phase 1: GF-Geburtsjahr aus der hr-engine-Anreicherung -> calvoran.companies.

Quelle: 01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv (Langform, eine Zeile
je GF). Join über norm(firma)+plz. Pro Firma der ÄLTESTE GF (kleinstes Geburtsjahr) als
Nachfolge-Indikator. Idempotent; partielle Abdeckung ist erwartet (hr-engine läuft noch).

    .venv/bin/python pipeline/c1b_import_gf_alter.py [--gf PATH]
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone

from _common import norm

from calvoran.db import get_client
from calvoran.logging import JsonLogger

DEFAULT_GF = "/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv"


def year_of(iso: str):
    iso = (iso or "").strip()
    if len(iso) >= 4 and iso[:4].isdigit():
        return int(iso[:4])
    return None


def fetch_all(client, columns: str) -> list[dict]:
    out, step, start = [], 1000, 0
    while True:
        r = client.table("companies").select(columns).range(start, start + step - 1).execute()
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gf", default=DEFAULT_GF)
    args = ap.parse_args()
    log = JsonLogger("import.log")
    client = get_client("calvoran")
    now_year = datetime.now(timezone.utc).year

    gf_rows = list(csv.DictReader(open(args.gf, encoding="utf-8")))
    # key -> (geburtsjahr, fetched_at) des ältesten GF
    by_key: dict = {}
    for r in gf_rows:
        jahr = year_of(r.get("gf_geburtsdatum"))
        if not jahr:
            continue
        key = f"{norm(r.get('firma'))}|{(r.get('plz') or '').strip()}"
        prev = by_key.get(key)
        if prev is None or jahr < prev[0]:
            by_key[key] = (jahr, r.get("fetched_at", ""))

    companies = fetch_all(client, "id,name,plz")
    matched = 0
    for c in companies:
        key = f"{norm(c.get('name'))}|{(c.get('plz') or '').strip()}"
        hit = by_key.get(key)
        if not hit:
            continue
        jahr, fetched = hit
        client.table("companies").update({
            "gf_geburtsjahr": jahr,
            "gf_alter": now_year - jahr,
            "gf_quelle": f"hr-engine/AD/{(fetched or '')[:10]}",
        }).eq("id", c["id"]).execute()
        matched += 1

    log.log("gf_alter_done", gf_zeilen=len(gf_rows), gf_firmen=len(by_key),
            companies=len(companies), gematcht=matched)
    print(f"GF-Alter: {matched} Firmen angereichert (von {len(by_key)} GF-Firmen, "
          f"{len(companies)} companies gesamt).")


if __name__ == "__main__":
    main()
