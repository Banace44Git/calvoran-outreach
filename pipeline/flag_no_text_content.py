#!/usr/bin/env python3
"""Flaggt gecrawlte, erreichbare Firmen ohne eine einzige Text-Seite:
`tech_signals.no_text_content = true`.

Hintergrund: c2 markiert eine Firma als reachable, sobald die Website antwortet —
aber trafilatura extrahiert bei Framesets, JS-only-Seiten und Platzhaltern keinen
Text (`pages.text_content` bleibt NULL). Solche Firmen bekommen nie ein Dossier
(c3-Skip `keine_seiten_mit_text`) und nie einen Score, haengen also dauerhaft im
GF>=58-Vorrat (materialize_gf58_ids) und belegen Tail-Slots in der c3-Selektion.

Das Flag ist der persistente Marker "wohl keine verwertbare Website":
  - materialize_gf58_ids.py schliesst geflaggte Firmen aus dem Vorrat aus,
  - c3 select_companies ueberspringt sie in der Tail-Selektion,
  - c2 stempelt es bei kuenftigen Crawls direkt in persist().

Idempotent und rerunnbar (bereits geflaggte werden uebersprungen). Ein spaeterer
Re-Crawl mit Text ueberschreibt tech_signals komplett — das Flag verschwindet dann
von selbst.

    .venv/bin/python pipeline/flag_no_text_content.py --dry-run   # nur zaehlen
    .venv/bin/python pipeline/flag_no_text_content.py             # Flags schreiben
"""
from __future__ import annotations

import argparse
import sys

import _common  # noqa: F401  (sys.path-Bootstrap für `import calvoran`)

from calvoran.db import fetch_all, get_client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nur zaehlen, nichts schreiben")
    args = ap.parse_args()

    client = get_client("calvoran")

    print("Lade erreichbar gecrawlte Firmen ...", file=sys.stderr)
    reachable = fetch_all(lambda: (
        client.table("companies").select("id,name,tech_signals")
        .filter("tech_signals->>reachable", "eq", "true")))

    print("Lade company_ids mit mindestens einer Text-Seite ...", file=sys.stderr)
    text_pages = fetch_all(lambda: (
        client.table("pages").select("id,company_id")
        .not_.is_("text_content", "null")))
    has_text = {p["company_id"] for p in text_pages}

    no_text = [c for c in reachable if c["id"] not in has_text]
    flagged = [c for c in no_text if (c.get("tech_signals") or {}).get("no_text_content") is True]
    todo = [c for c in no_text if c not in flagged]

    print(f"Erreichbar gecrawlt: {len(reachable)} | davon ohne Text-Seite: {len(no_text)} "
          f"| bereits geflaggt: {len(flagged)} | zu flaggen: {len(todo)}")
    for c in todo[:10]:
        print(f"  z.B. {c['name']}")

    if args.dry_run:
        print("Dry-Run: nichts geschrieben.")
        return

    for i, c in enumerate(todo, 1):
        ts = dict(c.get("tech_signals") or {})
        ts["no_text_content"] = True
        client.table("companies").update({"tech_signals": ts}).eq("id", c["id"]).execute()
        if i % 500 == 0:
            print(f"  {i}/{len(todo)} geflaggt ...", file=sys.stderr)
    print(f"Fertig: {len(todo)} Firmen geflaggt (tech_signals.no_text_content=true).")


if __name__ == "__main__":
    main()
