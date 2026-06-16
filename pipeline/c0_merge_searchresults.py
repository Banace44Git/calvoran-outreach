"""Phase 0: rohe North-Data-Such-Exporte (searchresults*.csv) -> Master-Zielliste.

North Data exportiert je PLZ-Suche eine CSV (Semikolon-getrennt, Latin1, max. 500
Zeilen, ~49 Spalten ohne die berechneten Master-Spalten). Dieses Tool:
- liest alle angegebenen searchresults zusammen,
- dedupt über die North Data URL (stabiler Firmen-Schlüssel),
- leitet "Anzahl Geschäftsführer" aus den drei Ges.-Vertreter-Spalten ab,
- schreibt eine UTF-8/Komma-CSV im Master-Format, die c1_import_zielliste direkt frisst
  und die die hr-engine im 00-inbox als Zielliste erkennt.

    .venv/bin/python pipeline/c0_merge_searchresults.py OUT.csv INPUT1.csv [INPUT2.csv ...]
"""

from __future__ import annotations

import csv
import sys


def read_searchresults(path: str) -> list[dict]:
    # North-Data-Export: Latin1, Semikolon. Bei kaputten Zeilen tolerant bleiben.
    with open(path, encoding="latin-1", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def anzahl_gf(row: dict) -> str:
    n = sum(1 for i in (1, 2, 3) if (row.get(f"Ges. Vertreter {i}") or "").strip())
    return str(n) if n else ""


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    out_path, inputs = sys.argv[1], sys.argv[2:]

    rows: dict[str, dict] = {}  # north_data_url -> row (erste Sichtung gewinnt)
    total = dups = no_url = 0
    cols: list[str] = []
    for p in inputs:
        for row in read_searchresults(p):
            total += 1
            if not cols:
                cols = list(row.keys())
            url = (row.get("North Data URL") or "").strip()
            if not url:
                no_url += 1
                continue
            if url in rows:
                dups += 1
                continue
            rows[url] = row

    # Berechnete Master-Spalte ergänzen, falls die Quelle sie nicht liefert.
    if "Anzahl Geschäftsführer" not in cols:
        cols.append("Anzahl Geschäftsführer")

    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows.values():
            row = dict(row)
            row.setdefault("Anzahl Geschäftsführer", "")
            if not row["Anzahl Geschäftsführer"]:
                row["Anzahl Geschäftsführer"] = anzahl_gf(row)
            w.writerow(row)

    print(f"Eingelesen: {total} Zeilen aus {len(inputs)} Dateien")
    print(f"  doppelt (gleiche URL): {dups} | ohne URL verworfen: {no_url}")
    print(f"  geschrieben (distinct): {len(rows)} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
