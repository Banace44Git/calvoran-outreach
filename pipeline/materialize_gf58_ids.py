#!/usr/bin/env python3
"""Materialisiert den priorisierten Crawl-Vorrat: GF>=58-Firmen, die die Chain
(c2 Crawl -> c3 Dossier -> c4 Score) noch nicht durchlaufen haben.

Der korrekte "noch nicht fertig"-Marker ist ein fehlender c4-Score, nicht ein
fehlender Crawl. Denn c2 (leicht) laeuft im Daemon durch, waehrend c3 (Gemma) bei
RAM-Druck der hr-engine ausweicht -- eine Firma kann also gecrawlt sein, aber noch
kein Dossier/keinen Score haben. Wuerde der Vorrat ueber `tech_signals IS NULL`
definiert, gingen genau diese verloren.

Vorrat-Definition: eine GF>=58-Firma gehoert in den Vorrat, wenn sie
  - eine website hat, kein Holding/Dublette/excluded ist,
  - noch KEINEN Score in `scores` hat, UND
  - entweder ungecrawlt (tech_signals IS NULL) oder erreichbar gecrawlt
    (tech_signals->>reachable = 'true') ist.
Nicht erreichbare Firmen (gecrawlt, aber reachable != true) bekommen nie ein
Dossier/Score und werden ausgeschlossen, damit der Vorrat sauber auf 0 laeuft und
der Daemon in den Tail wechseln kann.

    .venv/bin/python pipeline/materialize_gf58_ids.py --out queue.txt --limit 200
    .venv/bin/python pipeline/materialize_gf58_ids.py --count   # nur zaehlen

Schreib-Modus: bis --limit company-UUIDs je Zeile nach --out; druckt die Anzahl
geschriebener IDs als einzige Zeile auf stdout (Rest auf stderr) fuer den Wrapper.
--count: druckt GF>=58-Vorrat + ungecrawlten Tail lesbar auf stdout, schreibt nichts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from calvoran.db import get_client


def _base(client):
    return (client.table("companies").select("id,tech_signals")
            .not_.is_("website", "null")
            .eq("holding_flag", False)
            .is_("dup_of", "null")
            .eq("excluded", False))


def _scored_ids(client) -> set:
    ids, step, start = set(), 1000, 0
    while True:
        r = (client.table("scores").select("company_id")
             .order("company_id").range(start, start + step - 1).execute())
        ids.update(x["company_id"] for x in r.data)
        if len(r.data) < step:
            break
        start += step
    return ids


def gf58_backlog(client, min_alter) -> list:
    """GF>=58-Firmen ohne Score, ungecrawlt oder erreichbar gecrawlt."""
    scored = _scored_ids(client)
    out, step, start = [], 1000, 0
    while True:
        r = (_base(client).gte("gf_alter", min_alter)
             .order("id").range(start, start + step - 1).execute())
        for x in r.data:
            ts = x.get("tech_signals")
            reachable = isinstance(ts, dict) and ts.get("reachable") is True
            if (ts is None or reachable) and x["id"] not in scored:
                out.append(x["id"])
        if len(r.data) < step:
            break
        start += step
    return out


def tail_uncrawled_count(client) -> int:
    r = (_base(client).is_("tech_signals", "null")
         .limit(1).execute())
    # select mit count='exact' liefert die Gesamtzahl ohne alle Zeilen zu ziehen.
    r2 = (client.table("companies").select("id", count="exact")
          .not_.is_("website", "null").eq("holding_flag", False)
          .is_("dup_of", "null").eq("excluded", False)
          .is_("tech_signals", "null").limit(1).execute())
    return r2.count or 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="Zieldatei fuer die company-UUIDs (je Zeile)")
    ap.add_argument("--limit", type=int, default=0, help="Max. IDs schreiben (0 = alle)")
    ap.add_argument("--min-alter", type=int, default=58, help="GF-Alter-Schwelle (Default 58)")
    ap.add_argument("--count", action="store_true",
                    help="Nur zaehlen (GF>=58-Vorrat + Tail), nichts schreiben")
    args = ap.parse_args()

    client = get_client("calvoran")
    backlog = gf58_backlog(client, args.min_alter)

    if args.count:
        tail = tail_uncrawled_count(client)
        print(f"gf58_backlog={len(backlog)}")
        print(f"tail_uncrawled={tail}")
        return

    if not args.out:
        print("Fehler: --out fehlt (oder --count nutzen)", file=sys.stderr)
        sys.exit(2)

    ids = backlog[:args.limit] if args.limit else backlog
    Path(args.out).write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
    print(f"GF>={args.min_alter}-Vorrat: {len(backlog)} offen, {len(ids)} geschrieben -> {args.out}",
          file=sys.stderr)
    # stdout: nur die Zahl geschriebener IDs (Daemon-Wrapper liest das).
    print(len(ids))


if __name__ == "__main__":
    main()
