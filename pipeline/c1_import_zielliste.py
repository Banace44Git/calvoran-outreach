"""Phase 1: CSV-Import der Zielliste -> calvoran.companies, plus Stufe-0-Bereinigung.

- 53 Spalten -> getypte Kernspalten + vollständiges raw-jsonb (verlustfrei).
- Domain aus Website, wz2 aus Branche (WZ), robustes EUR-Parsing (deutsches Format).
- Stufe-0: Holding-/Mantel-Flag (WZ 64.20/68/70.10 + Bilanz-Umsatz-Plausibilität),
  Dubletten über Adresse + GF (dup_of auf einen operativen Anker).
- Idempotent: upsert on_conflict north_data_url; mehrfach lauffähig.

    .venv/bin/python pipeline/c1_import_zielliste.py [--csv PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import re

from _common import norm, wz2 as wz2_of

from calvoran.crawler import normalize_host
from calvoran.db import get_client
from calvoran.logging import JsonLogger

DEFAULT_CSV = "/Users/johannesbreuers/projects/calvoran-outreach/data/zielliste_2026-06-07.csv"
# Holding-/Mantel-Verdacht (Konzept §3, Stufe 0): WZ 64.20, alle 68er, 70.10.
HOLDING_PREFIXES = ("68", "64.20", "70.10")
TRUE_TOKENS = {"ja", "true", "1", "x", "y", "yes", "wahr"}


def wz_code(branche: str) -> str:
    """'68.32 Verwaltung ...' -> '68.32'. Voller numerischer WZ-Code ohne Klartext."""
    m = re.match(r"\s*(\d{2}(?:\.\d+)*)", branche or "")
    return m.group(1) if m else ""


def parse_num(s):
    if s is None:
        return None
    s = str(s).strip()
    if s.lower() in ("", "-", "n/a", "k.a.", "na", "none", "null"):
        return None
    s = s.replace(" ", " ").replace("€", "").replace("%", "").replace(" ", "")
    neg = s.startswith("-")
    s = s.lstrip("+-")
    if "," in s:                      # deutsches Format: '.' Tausender, ',' Dezimal
        s = s.replace(".", "").replace(",", ".")
    else:                            # ohne Komma: Punkte sind Tausendertrenner
        s = s.replace(".", "")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def parse_int(s):
    v = parse_num(s)
    return int(round(v)) if v is not None else None


def parse_bool(s):
    if s is None:
        return None
    return str(s).strip().lower() in TRUE_TOKENS


def map_row(row: dict) -> dict:
    website = (row.get("Website") or "").strip()
    branche = (row.get("Branche (WZ)") or "").strip()
    gv = [row.get(f"Ges. Vertreter {i}") for i in (1, 2, 3)]
    gv = [g.strip() for g in gv if g and g.strip()]
    return {
        "north_data_url": (row.get("North Data URL") or "").strip(),
        "name": (row.get("Name") or "").strip(),
        "rechtsform": (row.get("Rechtsform") or "").strip() or None,
        "plz": (row.get("PLZ") or "").strip() or None,
        "ort": (row.get("Ort") or "").strip() or None,
        "strasse": (row.get("Straße") or "").strip() or None,
        "hr_amtsgericht": (row.get("HR Amtsgericht") or "").strip() or None,
        "register_id": (row.get("Register-ID") or "").strip() or None,
        "status": (row.get("Status") or "").strip() or None,
        "website": website or None,
        "domain": normalize_host(website) or None,
        "branche_wz": branche or None,
        "wz2": wz2_of(branche) or None,
        "ges_vertreter": gv or None,
        "anzahl_gf": parse_int(row.get("Anzahl Geschäftsführer")),
        "gf_name_in_firmenname": parse_bool(row.get("GF-Name im Firmennamen")),
        "bilanzsumme_eur": parse_num(row.get("Bilanzsumme EUR")),
        "ek_quote_pct": parse_num(row.get("EK-Quote %")),
        "gewinn_cagr_pct": parse_num(row.get("Gewinn CAGR %")),
        "umsatz_eur": parse_num(row.get("Umsatz EUR")),
        "mitarbeiterzahl": parse_int(row.get("Mitarbeiterzahl")),
        "prioritaets_score": parse_num(row.get("Prioritäts-Score")),
        "raw": row,
    }


def holding_flag(rec: dict) -> tuple[bool, str | None]:
    code = wz_code(rec.get("branche_wz") or "")
    if any(code.startswith(p) for p in HOLDING_PREFIXES):
        return True, f"wz_{code}"
    bil, ums = rec.get("bilanzsumme_eur"), rec.get("umsatz_eur")
    if bil and bil > 1_000_000 and (ums is None or ums < bil * 0.05):
        return True, "bilanz_umsatz_implausibel"
    return False, None


def fetch_all(client, columns: str) -> list[dict]:
    out, step, start = [], 1000, 0
    while True:
        r = client.table("companies").select(columns).order("id").range(start, start + step - 1).execute()
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    log = JsonLogger("import.log")
    client = get_client("calvoran")

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]

    records, skipped = [], 0
    for row in rows:
        rec = map_row(row)
        if not rec["north_data_url"] or not rec["name"]:
            skipped += 1
            continue
        flag, reason = holding_flag(rec)
        rec["holding_flag"] = flag
        rec["holding_reason"] = reason
        records.append(rec)

    # dedupe innerhalb der CSV nach north_data_url (Upsert verträgt keine Doppel im selben Batch)
    by_url = {}
    for rec in records:
        by_url[rec["north_data_url"]] = rec
    records = list(by_url.values())

    # Upsert in Batches.
    inserted = 0
    for i in range(0, len(records), 500):
        batch = records[i:i + 500]
        client.table("companies").upsert(batch, on_conflict="north_data_url").execute()
        inserted += len(batch)
        log.log("import_batch", upserted=inserted, total=len(records))

    holding_n = sum(1 for r in records if r["holding_flag"])
    log.log("import_done", firmen=len(records), uebersprungen=skipped, holding=holding_n)

    # Stufe-0 Dubletten: Adresse + GF -> dup_of auf operativen Anker (max Umsatz/Bilanz).
    dedupe_pass(client, log)


def dedupe_pass(client, log) -> None:
    rows = fetch_all(client, "id,strasse,plz,ort,ges_vertreter,umsatz_eur,bilanzsumme_eur")
    groups: dict = {}
    for r in rows:
        gv = r.get("ges_vertreter") or []
        gf1 = norm(gv[0]) if gv else ""
        strasse = norm(r.get("strasse") or "")
        if not strasse or not gf1:
            continue
        key = f"{strasse}|{norm(r.get('plz') or '')}|{norm(r.get('ort') or '')}|{gf1}"
        groups.setdefault(key, []).append(r)

    dup_updates = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        anchor = max(members, key=lambda m: ((m.get("umsatz_eur") or 0), (m.get("bilanzsumme_eur") or 0)))
        for m in members:
            if m["id"] != anchor["id"]:
                client.table("companies").update({"dup_of": anchor["id"]}).eq("id", m["id"]).execute()
                dup_updates += 1
    log.log("dedupe_done", gruppen=sum(1 for m in groups.values() if len(m) > 1), markiert=dup_updates)


if __name__ == "__main__":
    main()
