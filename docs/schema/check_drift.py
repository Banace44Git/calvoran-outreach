#!/usr/bin/env python3
"""Drift-Check: dokumentierte Spalten (docs/schema/*.md) vs. Live-Schema calvoran.

Kein direkter Postgres-Zugang vorhanden — die Ist-Spalten kommen aus dem PostgREST-
OpenAPI-Spec (`GET {SUPABASE_URL}/rest/v1/` mit `Accept-Profile: calvoran`), der jede
Tabelle mit ihren Spalten liefert, auch leere. Verglichen wird nur die Spalten-MENGE
je Tabelle (Namen) — Typ-/Constraint-Änderungen stehen weiter in den Migrationen und
sind hier bewusst nicht abgedeckt (sie sind selten und laut).

Lauf:  .venv/bin/python docs/schema/check_drift.py
Exit:  0 = deckungsgleich, 1 = Drift (CI-/pre-commit-tauglich).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_SCHEMA_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCHEMA_DIR.parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from calvoran.db import ensure_env  # noqa: E402  (nach sys.path-Insert)

# Alt-Bestand (tote Apify-Pipeline): existiert in der DB, wird bewusst nicht dokumentiert.
IGNORE_TABLES = {"raw_jobs", "leads"}
# Dateien ohne Tabellen-Bezug.
IGNORE_DOCS = {"index"}

_CELL_TOKEN = re.compile(r"`([^`]+)`")


def live_columns() -> dict[str, set[str]]:
    """Spalten je calvoran-Tabelle aus dem PostgREST-OpenAPI-Spec."""
    ensure_env()
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    req = urllib.request.Request(url, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Accept-Profile": "calvoran", "Accept": "application/openapi+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        spec = json.load(resp)
    return {t: set(d.get("properties", {}))
            for t, d in spec.get("definitions", {}).items()}


def documented_columns(md: Path) -> set[str]:
    """Spaltennamen aus der `## Spalten`-Tabelle: erstes `backtick`-Token je Datenzeile."""
    cols: set[str] = set()
    in_block = False
    for line in md.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_block = line.strip().lower() == "## spalten"
            continue
        if not in_block:
            continue
        cells = line.split("|")
        if len(cells) < 3:                      # keine Tabellenzeile
            continue
        m = _CELL_TOKEN.search(cells[1])        # erste Zelle = Spaltenname
        if m:
            cols.add(m.group(1))
    return cols


def main() -> int:
    live = live_columns()
    docs = {p.stem: p for p in sorted(_SCHEMA_DIR.glob("*.md"))
            if p.stem not in IGNORE_DOCS}

    problems = 0
    for table, path in docs.items():
        if table not in live:
            print(f"[DRIFT] {table}: dokumentiert, aber nicht im Live-Schema.")
            problems += 1
            continue
        documented = documented_columns(path)
        actual = live[table]
        if not documented:
            print(f"[WARN ] {table}: keine `## Spalten`-Tabelle gefunden (Parsing?).")
            problems += 1
            continue
        missing = actual - documented           # in DB, fehlt in Doku
        stale = documented - actual             # in Doku, nicht (mehr) in DB
        if missing or stale:
            problems += 1
            print(f"[DRIFT] {table}:")
            if missing:
                print(f"         + in DB, undokumentiert: {sorted(missing)}")
            if stale:
                print(f"         - dokumentiert, nicht in DB: {sorted(stale)}")
        else:
            print(f"[ ok  ] {table}: {len(actual)} Spalten deckungsgleich.")

    # Live-Tabellen ganz ohne Doku (außer Alt-Bestand).
    undocumented = set(live) - set(docs) - IGNORE_TABLES
    for table in sorted(undocumented):
        print(f"[DRIFT] {table}: im Live-Schema, aber keine docs/schema/{table}.md.")
        problems += 1

    print("-" * 60)
    if problems:
        print(f"{problems} Abweichung(en) — Doku aktualisieren.")
        return 1
    print(f"Deckungsgleich: {len(docs)} Tabellen dokumentiert, Alt-Bestand ignoriert "
          f"({', '.join(sorted(IGNORE_TABLES))}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
