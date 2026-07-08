"""Brücke calvoran -> hr-engine: Unternehmensgröße (Umsatz EUR) in die Abruf-Queue.

Die hr-engine kennt nur Register-Stammdaten (Name/PLZ/Register), nicht die Größe.
Für die Größen-Sekundärsortierung der Queue (nach der PLZ-Leiter) schreibt dieses Script
``companies.umsatz_eur`` (Fallback ``bilanzsumme_eur``) als ``jobs.prio_groesse`` in die
hr-engine-SQLite. Match über norm(name)+plz, identisch zu c1b. Idempotent; kein Match ->
prio_groesse bleibt NULL -> sortiert ans Ende des jeweiligen PLZ-Bands.

Vor dem Lauf den Daemon stoppen (sonst SQLite-Schreibkonflikt):
    launchctl unload ~/Library/LaunchAgents/com.jbreuers.hr-engine.plist
    .venv/bin/python pipeline/c0c_export_groesse_hrengine.py
    launchctl load   ~/Library/LaunchAgents/com.jbreuers.hr-engine.plist
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from _common import norm

from calvoran.db import get_client

HR_DB = Path.home() / ".local/state/hr-engine/state.db"


def load_groesse(client) -> dict:
    """key = norm(name)|plz -> Größe (Umsatz EUR, sonst Bilanzsumme EUR)."""
    out, step, start = {}, 1000, 0
    while True:
        r = (client.table("companies")
             .select("name,plz,umsatz_eur,bilanzsumme_eur")
             .order("id").range(start, start + step - 1).execute())
        for c in r.data:
            groesse = c.get("umsatz_eur")
            if groesse is None:
                groesse = c.get("bilanzsumme_eur")
            if groesse is None:
                continue
            key = f"{norm(c.get('name'))}|{(c.get('plz') or '').strip()}"
            # Bei Dubletten den größeren Wert behalten (konservativ Richtung Priorität).
            if key not in out or groesse > out[key]:
                out[key] = float(groesse)
        if len(r.data) < step:
            break
        start += step
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HR_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = get_client("calvoran")
    groesse = load_groesse(client)
    print(f"calvoran: {len(groesse)} Firmen mit Größe geladen.")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "prio_groesse" not in cols:
        if args.dry_run:
            print("Spalte prio_groesse fehlt (würde angelegt).")
        else:
            conn.execute("ALTER TABLE jobs ADD COLUMN prio_groesse REAL")

    jobs = conn.execute("SELECT job_key, name, plz FROM jobs").fetchall()
    matched = 0
    updates = []
    for j in jobs:
        key = f"{norm(j['name'])}|{(j['plz'] or '').strip()}"
        g = groesse.get(key)
        if g is not None:
            updates.append((g, j["job_key"]))
            matched += 1

    if not args.dry_run:
        conn.executemany("UPDATE jobs SET prio_groesse=? WHERE job_key=?", updates)
        conn.commit()
    conn.close()
    print(f"hr-engine: {matched}/{len(jobs)} Jobs mit Größe angereichert"
          f"{' (dry-run, nicht geschrieben)' if args.dry_run else ''}.")


if __name__ == "__main__":
    main()
