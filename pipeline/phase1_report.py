"""Phase-1-Abnahme/Reporting: Verteilungen aus calvoran.companies.

    .venv/bin/python pipeline/phase1_report.py
Schreibt 01-projects/fractional-cfo/outreach/phase1-import-report.md und druckt ihn.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

from _common import OUTPUT_DIR

from calvoran.db import get_client


def fetch_all(client, cols: str) -> list[dict]:
    out, step, start = [], 1000, 0
    while True:
        r = client.table("companies").select(cols).range(start, start + step - 1).execute()
        out += r.data
        if len(r.data) < step:
            break
        start += step
    return out


def main() -> None:
    c = get_client("calvoran")
    yr = datetime.now(timezone.utc).year
    rows = fetch_all(c, "wz2,prioritaets_score,holding_flag,holding_reason,dup_of,"
                        "gf_geburtsjahr,website,bilanzsumme_eur,mitarbeiterzahl")
    n = len(rows)
    prio = Counter(int(r["prioritaets_score"]) if r["prioritaets_score"] is not None else -1 for r in rows)
    holding = sum(1 for r in rows if r["holding_flag"])
    # Reason auf 2-Steller gruppieren (kompakter)
    hreason = Counter((r["holding_reason"] or "")[:6] for r in rows if r["holding_flag"])
    dup = sum(1 for r in rows if r["dup_of"])
    gf = sum(1 for r in rows if r["gf_geburtsjahr"])
    gf58 = sum(1 for r in rows if r["gf_geburtsjahr"] and (yr - r["gf_geburtsjahr"]) >= 58)
    web = sum(1 for r in rows if r["website"])
    bil = sum(1 for r in rows if r["bilanzsumme_eur"] is not None)
    ma = sum(1 for r in rows if r["mitarbeiterzahl"] is not None)
    wz = Counter(r["wz2"] for r in rows if r["wz2"]).most_common(8)

    L = []
    L.append("# Phase-1-Abnahme: Import + Stufe-0\n")
    L.append(f"Stand {datetime.now(timezone.utc).date()}. Quelle: data/zielliste_2026-06-07.csv.\n")
    L.append(f"- **Firmen importiert:** {n}")
    L.append(f"- **Holding-/Mantel-Flag:** {holding} ({holding/n*100:.0f}%) — "
             + ", ".join(f"{k}: {v}" for k, v in hreason.most_common(8)))
    L.append(f"- **Dubletten (Adresse+GF) markiert:** {dup}")
    L.append(f"- **Website vorhanden:** {web} ({web/n*100:.0f}%)")
    L.append(f"- **Finanz-Coverage:** Bilanzsumme {bil} ({bil/n*100:.0f}%), Mitarbeiter {ma} ({ma/n*100:.0f}%)")
    L.append(f"- **GF-Geburtsjahr (hr-engine):** {gf} angereichert, davon {gf58} ab 58 Jahre")
    L.append("\n## Prioritäts-Score-Verteilung")
    for k in sorted(prio, reverse=True):
        L.append(f"- Score {'ohne' if k == -1 else k}: {prio[k]}")
    L.append(f"\nWelle 1 (Score 2+3): {prio.get(2,0)+prio.get(3,0)} Firmen.")
    L.append("\n## Top-Branchen (WZ-2-Steller)")
    for k, v in wz:
        L.append(f"- WZ {k}: {v}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "phase1-import-report.md")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))
    print("\nReport:", path)


if __name__ == "__main__":
    main()
