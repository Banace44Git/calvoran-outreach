#!/usr/bin/env python3
"""Einmaliger Backfill: bereits erzeugte Briefe / kuratierte Leads -> calvoran.outreach.

Die Vorselektion lebt weiter in selection.jsonl (System of Record der Kuration). Die DB
wird System of Record AB der Versand-Menge: je Brief eine Zeile in calvoran.outreach
(channel='letter', status='queued'), damit der Funnel den Brief-Bestand zeigt. Versand-
Status bleibt 'queued', bis er im Dashboard ('Welle als versandt markieren') gesetzt wird.

Zwei Quellen (--source):
  merge-data  (Default): die TATSÄCHLICH erzeugten Briefe aus <briefe>/_merge-data.json
              (Schlüssel = Firmennamen -> per DB-Lookup auf company_id). Spiegelt den realen
              Brief-Bestand (z.B. die 88 der Charge), nicht alle kuratierten Leads.
  selection : ALLE als Lead markierten Firmen einer Welle aus selection.jsonl (auch ohne
              erzeugten Brief). Nur wählen, wenn der Funnel "Lead" = "Brief" gleichsetzen soll.

Idempotent: bestehende (company_id, channel='letter', wave)-Zeilen werden nicht doppelt
angelegt (fetch-existing-then-insert) — läuft auch ohne den Unique-Index aus Migration 0006.

Aufruf:
  .venv/bin/python pipeline/backfill_outreach_from_selection.py --dry-run        # 88 aus merge-data
  .venv/bin/python pipeline/backfill_outreach_from_selection.py                  # schreibt sie
  .venv/bin/python pipeline/backfill_outreach_from_selection.py --source selection --wave 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calvoran.db import get_client  # noqa: E402

OUTREACH_DIR = "/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/outreach"
SELECTION_DEFAULT = f"{OUTREACH_DIR}/selection.jsonl"
MERGE_DATA_DEFAULT = f"{OUTREACH_DIR}/briefe-2026-06-25/_merge-data.json"
CHUNK = 50


def leads_from_selection(path: str, only_wave: int | None) -> dict[int, list[str]]:
    """wave -> [company_id, …] für alle als Lead markierten Firmen."""
    by_wave: dict[int, list[str]] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            is_lead = r.get("decision") == "lead" or (r.get("decision") is None and r.get("selected"))
            if not is_lead or not r.get("company_id"):
                continue
            wave = r.get("wave")
            if only_wave is not None and wave != only_wave:
                continue
            by_wave[int(wave)].append(r["company_id"])
    return by_wave


def leads_from_merge_data(cl, path: str, wave: int) -> dict[int, list[str]]:
    """Firmennamen aus _merge-data.json -> company_id (exakter Name-Lookup in companies)."""
    names = list(json.load(open(path, encoding="utf-8")).keys())
    name_to_id: dict[str, str] = {}
    for i in range(0, len(names), CHUNK):
        for c in (cl.table("companies").select("id,name")
                  .in_("name", names[i:i + CHUNK]).execute().data):
            name_to_id[c["name"]] = c["id"]
    matched = [name_to_id[n] for n in names if n in name_to_id]
    unmatched = [n for n in names if n not in name_to_id]
    if unmatched:
        print(f"  ⚠ {len(unmatched)} Briefe ohne companies-Match (Namensabweichung), übersprungen:")
        for n in unmatched:
            print(f"      - {n}")
    return {wave: matched}


def existing_letter_ids(cl, wave: int, company_ids: list[str]) -> set[str]:
    seen: set[str] = set()
    for i in range(0, len(company_ids), CHUNK):
        rows = (cl.table("outreach").select("company_id")
                .eq("channel", "letter").eq("wave", wave)
                .in_("company_id", company_ids[i:i + CHUNK]).execute().data)
        seen.update(r["company_id"] for r in rows)
    return seen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["merge-data", "selection"], default="merge-data",
                    help="merge-data = erzeugte Briefe (Default); selection = alle Leads der Welle")
    ap.add_argument("--merge-data", default=MERGE_DATA_DEFAULT, help="Pfad zu _merge-data.json")
    ap.add_argument("--selection", default=SELECTION_DEFAULT, help="Pfad zu selection.jsonl")
    ap.add_argument("--wave", type=int, default=1, help="Versandwelle (Default 1)")
    ap.add_argument("--dry-run", action="store_true", help="nur zählen, nichts schreiben")
    args = ap.parse_args()

    cl = get_client()
    if args.source == "merge-data":
        by_wave = leads_from_merge_data(cl, args.merge_data, args.wave)
        # Nur erzeugte Briefe nehmen, die in selection.jsonl AUCH Lead sind. Ein Brief kann
        # für einen seither deselektierten Lead existieren (z.B. Biermann Verlag) — der soll
        # nicht ins Versand-Tracking. selection.jsonl ist die Lead-Wahrheit.
        lead_ids = {cid for ids in leads_from_selection(args.selection, None).values()
                    for cid in ids}
        for w in list(by_wave):
            keep = [cid for cid in by_wave[w] if cid in lead_ids]
            dropped = len(by_wave[w]) - len(keep)
            if dropped:
                print(f"  ⚠ Welle {w}: {dropped} erzeugte Briefe sind in selection.jsonl "
                      "kein Lead → übersprungen.")
            by_wave[w] = keep
    else:
        by_wave = leads_from_selection(args.selection, args.wave)

    if not by_wave or not any(by_wave.values()):
        print("Keine Firmen gefunden — nichts zu tun.")
        return

    total_new = 0
    for wave in sorted(by_wave):
        ids = by_wave[wave]
        present = existing_letter_ids(cl, wave, ids)
        missing = [cid for cid in ids if cid not in present]
        print(f"Welle {wave}: {len(ids)} Briefe · {len(present)} bereits in outreach · "
              f"{len(missing)} neu{' (dry-run)' if args.dry_run else ''}")
        if args.dry_run or not missing:
            continue
        rows = [{"company_id": cid, "channel": "letter", "status": "queued", "wave": wave}
                for cid in missing]
        for i in range(0, len(rows), CHUNK):
            cl.table("outreach").insert(rows[i:i + CHUNK]).execute()
        total_new += len(missing)

    print(f"\nFertig: {total_new} neue outreach-Zeilen (status='queued', channel='letter').")


if __name__ == "__main__":
    main()
