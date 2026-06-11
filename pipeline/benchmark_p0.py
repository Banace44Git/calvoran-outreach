"""Phase-0-Benchmark: Dossier-Extraktion lokale Gemma vs. Anthropic Haiku.

Wählt ein Goldset (Firmen mit Website, über Makrocluster gestreut), crawlt sie,
fährt die Extraktion mit beiden Backends und misst Feldfüllung, Belegtreue
(Zitate wörtlich im Seitentext), Tokens/Sekunde und Laufzeit. Ergebnis als
Markdown-Tabelle nach 01-projects/fractional-cfo/outreach/benchmark-p0.md.

    .venv/bin/python pipeline/benchmark_p0.py --n 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime, timezone

from _common import CSV_DEFAULT, OUTPUT_DIR, norm, wz2

from calvoran import config, crawler, extractor
from calvoran.logging import JsonLogger
from calvoran.model_router import ModelRouter

# WZ-2-Steller -> Makrocluster (zur Streuung des Goldsets).
_CLUSTER = {}
for _name, _codes in config.load("clusters")["makrocluster"].items():
    for _c in _codes:
        _CLUSTER[_c] = _name


def makrocluster(branche_wz: str) -> str:
    return _CLUSTER.get(wz2(branche_wz), "rest")


def select_goldset(csv_path: str, n: int) -> list[dict]:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    cand = [r for r in rows if (r.get("Website") or "").strip()]
    # Priorität: Score 3, dann 2 (dort zählt Dossier-Qualität am meisten).
    cand.sort(key=lambda r: -int(r.get("Prioritäts-Score") or 0))
    # Round-Robin über Makrocluster für Streuung.
    buckets: dict = {}
    for r in cand:
        buckets.setdefault(makrocluster(r.get("Branche (WZ)", "")), []).append(r)
    order = sorted(buckets, key=lambda k: -len(buckets[k]))
    picked: list[dict] = []
    i = 0
    while len(picked) < n and any(buckets.values()):
        b = order[i % len(order)]
        if buckets[b]:
            picked.append(buckets[b].pop(0))
        i += 1
    return picked[:n]


def belegtreue(belege, fulltext_norm: str) -> tuple[int, int]:
    """Belegtreu, wenn ein zusammenhängendes 6-Token-Fenster des Zitats im Text vorkommt.

    Fair gegenüber längeren (aber wörtlichen) Zitaten; eine Halluzination hat keine
    n-Gramm-Überlappung. Kurze Zitate (<6 Tokens) verlangen vollen Substring.
    """
    ok = 0
    for b in belege:
        q = norm(b.zitat).strip(' "\'.,;:!?-')
        toks = q.split()
        if len(toks) >= 6:
            hit = any(" ".join(toks[i:i + 6]) in fulltext_norm for i in range(len(toks) - 5))
        else:
            hit = len(q) >= 8 and q in fulltext_norm
        ok += 1 if hit else 0
    return ok, len(belege)


def fields_filled(dossier) -> int:
    d = dossier.model_dump()
    count = 0
    for k, v in d.items():
        if isinstance(v, (list,)):
            count += 1 if v else 0
        elif isinstance(v, dict):
            count += 1 if any(vv not in (None, False, "", []) for vv in v.values()) else 0
        else:
            count += 1 if v not in (None, "", False) else 0
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--csv", default=CSV_DEFAULT)
    ap.add_argument("--backends", default="gemma_local,haiku")
    args = ap.parse_args()

    log = JsonLogger("benchmark.log", echo=True)
    crawl_cfg = config.load("crawl")
    mod_cfg = config.load("modernity")
    router = ModelRouter(config.load("models"))
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    now_year = datetime.now(timezone.utc).year

    goldset = select_goldset(args.csv, args.n)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Goldset transparent ablegen.
    with open(os.path.join(os.path.dirname(__file__), "..", "data", "goldset_30.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Name", "Website", "Branche (WZ)", "Makrocluster", "Prioritäts-Score"])
        for r in goldset:
            w.writerow([r.get("Name"), r.get("Website"), r.get("Branche (WZ)"),
                        makrocluster(r.get("Branche (WZ)", "")), r.get("Prioritäts-Score")])

    log.log("benchmark_start", n=len(goldset), backends=backends)

    # 1) Crawlen (parallel).
    websites = [r["Website"].strip() for r in goldset]
    crawls = asyncio.run(crawler.crawl_many(websites, crawl_cfg, logger=log))

    jsonl_path = os.path.join(OUTPUT_DIR, "benchmark-p0.jsonl")
    results = []
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for r, cr in zip(goldset, crawls):
            company = {
                "name": r.get("Name", ""), "plz": r.get("PLZ", ""), "ort": r.get("Ort", ""),
                "branche_wz": r.get("Branche (WZ)", ""), "ges_vertreter_1": r.get("Ges. Vertreter 1", ""),
            }
            score, _bd = (None, {})
            try:
                from calvoran import modernity
                score, _bd = modernity.compute(cr["tech_signals"], mod_cfg, now_year=now_year)
            except Exception:
                pass
            n_pages = len([p for p in cr["pages"] if (p.get("text") or "").strip()])
            fulltext = norm(" ".join(p.get("text", "") for p in cr["pages"]))
            row = {"name": company["name"], "website": r.get("Website"),
                   "makrocluster": makrocluster(company["branche_wz"]),
                   "crawl_error": cr.get("error"), "pages": n_pages, "modernity": score, "backends": {}}
            if cr.get("error") or n_pages == 0:
                log.log("skip_firm", name=company["name"], reason=cr.get("error") or "no_pages")
            else:
                for backend in backends:
                    try:
                        d, meta = extractor.extract_dossier(router, company, cr["pages"], crawl_cfg,
                                                            backend=backend, logger=log)
                        bok, btot = belegtreue(d.belege, fulltext)
                        row["backends"][backend] = {
                            "ok": True, "elapsed_s": meta["elapsed_s"], "tokens_per_s": meta.get("tokens_per_s"),
                            "input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
                            "fields_filled": fields_filled(d), "n_belege": btot, "belegtreu": bok,
                            "repair_count": meta.get("repair_count"),
                        }
                    except Exception as e:
                        row["backends"][backend] = {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}
                        log.log("extract_error", name=company["name"], backend=backend, error=str(e)[:160])
            results.append(row)
            jf.write(json.dumps(row, ensure_ascii=False) + "\n")
            jf.flush()
            log.log("firm_done", name=company["name"], modernity=score,
                    backends={b: results[-1]["backends"].get(b, {}).get("ok") for b in backends})

    write_report(results, backends, OUTPUT_DIR, now_year)
    log.log("benchmark_done", firms=len(results))


def _agg(results, backend, key):
    vals = [r["backends"][backend][key] for r in results
            if backend in r["backends"] and r["backends"][backend].get("ok") and r["backends"][backend].get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def write_report(results, backends, out_dir, now_year) -> None:
    ok_crawls = [r for r in results if not r["crawl_error"] and r["pages"] > 0]
    lines = []
    lines.append(f"# Phase-0-Benchmark: Dossier-Extraktion (Gemma vs. Haiku)\n")
    lines.append(f"Stand {datetime.now(timezone.utc).date()}. Goldset: {len(results)} Firmen, "
                 f"davon {len(ok_crawls)} erfolgreich gecrawlt.\n")
    # Modernitäts-Verteilung
    mods = [r["modernity"] for r in ok_crawls if r["modernity"] is not None]
    if mods:
        lines.append(f"Website-Modernität (0-10): min {min(mods)}, max {max(mods)}, "
                     f"Mittel {sum(mods)/len(mods):.1f} (n={len(mods)}).\n")

    lines.append("## Aggregat je Backend\n")
    lines.append("| Backend | Erfolg | Ø Felder gefüllt | Ø Belege | Ø Belegtreue | Ø Laufzeit (s) | Ø Tokens/s | Σ In-Tok | Σ Out-Tok |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for b in backends:
        ok = [r for r in results if r["backends"].get(b, {}).get("ok")]
        n_ok = len(ok)
        bt_ok = sum(r["backends"][b]["belegtreu"] for r in ok)
        bt_tot = sum(r["backends"][b]["n_belege"] for r in ok)
        in_tok = sum((r["backends"][b].get("input_tokens") or 0) for r in ok)
        out_tok = sum((r["backends"][b].get("output_tokens") or 0) for r in ok)
        lines.append(
            f"| {b} | {n_ok}/{len(ok_crawls)} | {_agg(results,b,'fields_filled'):.1f} | "
            f"{_agg(results,b,'n_belege'):.1f} | {(bt_ok/bt_tot*100 if bt_tot else 0):.0f}% | "
            f"{_agg(results,b,'elapsed_s'):.1f} | {_agg(results,b,'tokens_per_s'):.0f} | {in_tok} | {out_tok} |"
        )

    lines.append("\n## Je Firma\n")
    lines.append("| Firma | Cluster | Mod | " + " | ".join(f"{b}: Felder/Belege/treu/s" for b in backends) + " |")
    lines.append("|---|---|---|" + "|".join(["---"] * len(backends)) + "|")
    for r in results:
        cells = []
        for b in backends:
            x = r["backends"].get(b, {})
            if x.get("ok"):
                cells.append(f"{x['fields_filled']}/{x['n_belege']}/{x['belegtreu']}/{x['elapsed_s']:.0f}")
            else:
                cells.append("FEHLER" if b in r["backends"] else "-")
        mod = r["modernity"] if r["modernity"] is not None else "-"
        name = (r["name"] or "")[:34]
        lines.append(f"| {name} | {r['makrocluster']} | {mod} | " + " | ".join(cells) + " |")

    lines.append("\n## Hinweise\n")
    lines.append("- Belegtreue = Anteil der Belege, deren wörtliches Zitat (normalisiert) im gecrawlten Seitentext vorkommt.")
    lines.append("- Haiku-Input-Tokens enthalten das Tool-Schema (~3-4k/Aufruf); für den Vollauf lohnt Prompt-Caching von System+Tool.")
    lines.append("- Gemma läuft lokal (Kosten 0), seriell; Haiku/Sonnet über API.")
    lines.append("- Entscheidung Router-Belegung: bei ausreichender Gemma-Belegtreue trägt Gemma die Masse (Score 0/1), "
                 "sonst belegpflichtige Felder über die API.")

    path = os.path.join(out_dir, "benchmark-p0.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("Report:", path)


if __name__ == "__main__":
    main()
