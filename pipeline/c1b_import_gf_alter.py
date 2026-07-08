"""Phase 1: GF-Geburtsjahr aus der hr-engine-Anreicherung -> calvoran.companies.

Quelle: 01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv (Langform, eine Zeile
je Person). Join über norm(firma)+plz. Es zählen ausschließlich Personen mit ist_gf=1
(Geschäftsführer) — Prokuristen/Liquidatoren bleiben außen vor. Pro Firma: der ÄLTESTE
GF (kleinstes Geburtsjahr) als Nachfolge-Indikator und die GF-Anzahl laut AD (überschreibt
den North-Data-Zählwert). Idempotent; partielle Abdeckung erwartet (hr-engine läuft noch).

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
        r = client.table("companies").select(columns).order("id").range(start, start + step - 1).execute()
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
    # Nur echte Geschäftsführer (ist_gf=1) zählen. Prokuristen, Liquidatoren etc. dürfen
    # weder das GF-Alter noch die GF-Anzahl bestimmen — sonst kapert z.B. ein 82-jähriger
    # Prokurist das Nachfolge-Signal einer Firma mit zwei GF Anfang 60.
    by_key: dict = {}   # key -> (geburtsjahr, fetched_at) des ältesten GF
    gf_count: dict = {}  # key -> Anzahl GF laut AD (belastbarer als North-Data-Zählwert)
    for r in gf_rows:
        if (r.get("ist_gf") or "").strip() != "1":
            continue
        key = f"{norm(r.get('firma'))}|{(r.get('plz') or '').strip()}"
        gf_count[key] = gf_count.get(key, 0) + 1
        jahr = year_of(r.get("gf_geburtsdatum"))
        if not jahr:
            continue
        prev = by_key.get(key)
        if prev is None or jahr < prev[0]:
            by_key[key] = (jahr, r.get("fetched_at", ""))

    companies = fetch_all(client, "id,name,plz")
    matched = matched_count = writes = 0
    for c in companies:
        key = f"{norm(c.get('name'))}|{(c.get('plz') or '').strip()}"
        upd: dict = {}
        hit = by_key.get(key)
        if hit:
            jahr, fetched = hit
            upd.update({
                "gf_geburtsjahr": jahr,
                "gf_alter": now_year - jahr,
                "gf_quelle": f"hr-engine/AD/{(fetched or '')[:10]}",
            })
            matched += 1
        n_gf = gf_count.get(key)
        if n_gf:
            upd["anzahl_gf"] = n_gf
            matched_count += 1
        if upd:
            client.table("companies").update(upd).eq("id", c["id"]).execute()
            writes += 1
            # Supabase-Gateway kappt HTTP/2 nach ~10.000 Requests je Verbindung
            # (RemoteProtocolError: ConnectionTerminated, last_stream_id 19999).
            # Vor der Grenze eine frische Verbindung holen; get_client() erzeugt
            # jeweils einen neuen Client (kein Caching). Vgl. Memory-Cap-Regel.
            if writes % 5000 == 0:
                client = get_client("calvoran")
                log.log("client_reconnect", writes=writes)

    log.log("gf_alter_done", gf_zeilen=len(gf_rows), gf_firmen=len(by_key),
            companies=len(companies), gematcht=matched, anzahl_gf_gesetzt=matched_count)
    print(f"GF-Alter: {matched} Firmen angereichert (von {len(by_key)} GF-Firmen), "
          f"Anzahl GF aus AD korrigiert: {matched_count} ({len(companies)} companies gesamt).")


if __name__ == "__main__":
    main()
