"""Blind-Qualitätscheck Gemma vs. Haiku auf der Welle-1-Dossier-Aufgabe.

Substitutionsfrage (Memory `project_gemma_substitution_check`): Reicht lokales
Gemma 4 qualitativ, um Haiku in `dossier_score_2_3` zu ersetzen? Liefergegenstand
ist ein menschlich beurteilbares Side-by-Side, KEIN Metrik-Report.

Vorgehen:
  - Stratifizierte Stichprobe (~3 je Makrocluster) aus den vorhandenen
    Haiku-Dossiers (alle Welle-1-Dossiers sind Haiku).
  - Haiku-Dossier kommt aus calvoran.dossiers (bereits extrahiert, temp 0).
  - Gemma rechnet dieselben Seiten in-memory neu, OHNE DB-Persistenz.
  - Pro Firma zwei anonymisierte Blöcke (Modell A / Modell B); die A/B-Zuordnung
    ist deterministisch per sha256(company_id) -> blind für Jo, reproduzierbar.
  - Belegtreue/Felder/Zeit werden gemessen, aber NUR in die Auflösung geschrieben
    (das Blind-Dokument bleibt frei von objektiven Tells).

    PYTHONPATH=pipeline .venv/bin/python pipeline/gemma_blind_dossier.py --per-cluster 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from _common import OUTPUT_DIR, norm
from benchmark_p0 import belegtreue, fields_filled
from c3_extract import COLS, load_pages

from calvoran import config, extractor
from calvoran.db import get_client
from calvoran.logging import JsonLogger
from calvoran.model_router import ModelRouter
from calvoran.schemas import Dossier

STAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def branche(cluster_key) -> str:
    return (cluster_key or "rest__?").split("__")[0]


# --------------------------------------------------------------------------- #
# Selektion: stratifiziert je Makrocluster, Score-Klassen-Mix, reproduzierbar
# --------------------------------------------------------------------------- #
def fetch_all(client, tbl, cols):
    out, step, start = [], 1000, 0
    while True:
        r = client.table(tbl).select(cols).order("id").range(start, start + step - 1).execute()
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step
    return out


def select_candidates(client, per_cluster: int) -> list[dict]:
    dos_ids = {d["company_id"] for d in fetch_all(client, "dossiers", "company_id")}
    scores = {s["company_id"]: s for s in fetch_all(
        client, "scores", "company_id,score_klasse,cluster_key,score_total")}

    # nur Firmen mit Haiku-Dossier UND Score, KO raus
    pool = [scores[i] for i in dos_ids
            if i in scores and scores[i]["score_klasse"] != "KO"]

    by_cluster: dict[str, list] = defaultdict(list)
    for s in pool:
        by_cluster[branche(s["cluster_key"])].append(s)

    picked: list[dict] = []
    for cl in sorted(by_cluster):
        rows = by_cluster[cl]
        # Score-Klassen-Buckets, je deterministisch nach company_id-Hash sortiert
        buckets: dict[str, list] = defaultdict(list)
        for r in sorted(rows, key=lambda r: _h(r["company_id"])):
            buckets[r["score_klasse"]].append(r)
        # Round-Robin A,B,C -> Klassen-Mix je Cluster
        order, i = ["A", "B", "C"], 0
        chosen = []
        while len(chosen) < min(per_cluster, len(rows)):
            k = order[i % len(order)]
            if buckets[k]:
                chosen.append(buckets[k].pop(0))
            i += 1
            if i > len(order) * (per_cluster + 2):  # Sicherung gegen Endlosschleife
                for k in order:
                    while buckets[k] and len(chosen) < per_cluster:
                        chosen.append(buckets[k].pop(0))
                break
        picked.extend(chosen)

    ids = [p["company_id"] for p in picked]
    comp = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        r = client.table("companies").select(COLS).in_("id", chunk).execute()
        for c in r.data:
            comp[c["id"]] = c

    out = []
    for p in picked:
        c = comp.get(p["company_id"])
        if not c:
            continue
        out.append({**c, "score_klasse": p["score_klasse"],
                    "cluster_key": p["cluster_key"], "score_total": p["score_total"],
                    "cluster": branche(p["cluster_key"])})
    return out


def load_haiku_dossiers(client, ids) -> dict:
    out, step = {}, 200
    for i in range(0, len(ids), step):
        chunk = ids[i:i + step]
        r = (client.table("dossiers")
             .select("company_id,dossier,model_backend")
             .in_("company_id", chunk).execute())
        for d in r.data:
            out[d["company_id"]] = d
    return out


# --------------------------------------------------------------------------- #
# Messung (nur in die Auflösung)
# --------------------------------------------------------------------------- #
def belegtreue_any(belege_raw, fulltext_norm) -> tuple[int, int]:
    """belegtreue() erwartet .zitat; Haiku-Belege sind Dicts, Gemma-Belege Objekte."""
    objs = [b if hasattr(b, "zitat") else SimpleNamespace(zitat=(b or {}).get("zitat", ""))
            for b in belege_raw]
    return belegtreue(objs, fulltext_norm)


def measure(dossier_dict: dict, fulltext_norm: str) -> dict:
    bok, btot = belegtreue_any(dossier_dict.get("belege") or [], fulltext_norm)
    try:
        ff = fields_filled(Dossier(**dossier_dict))
    except Exception:
        ff = None
    return {"fields_filled": ff, "belege": btot, "belegtreu": bok}


# --------------------------------------------------------------------------- #
# Rendering (judge-freundlich, gleiche Form für beide Modelle)
# --------------------------------------------------------------------------- #
def render_dossier(d: dict) -> str:
    def lst(x):
        return ", ".join(x) if x else "—"

    fam = d.get("familienunternehmen") or {}
    fs = d.get("fuehrungsstruktur") or {}
    kar = d.get("karriere") or {}
    nf = d.get("negativ_filter") or {}
    L = []
    L.append(f"- **Geschäftsmodell:** {d.get('geschaeftsmodell') or '—'}")
    L.append(f"- **Produkte/Leistungen:** {lst(d.get('produkte_leistungen'))}")
    L.append(f"- **Kundentyp:** {d.get('kundentyp') or '—'}  ·  "
             f"**Gründungsjahr:** {d.get('gruendungsjahr') or '—'}")
    L.append(f"- **Familienunternehmen:** hinweis={fam.get('hinweis')}, "
             f"generation={fam.get('generation') or '—'}, beleg={fam.get('beleg') or '—'}")
    L.append(f"- **Führungsstruktur:** GF: {lst(fs.get('gf_auf_website'))}; "
             f"2. Ebene sichtbar={fs.get('zweite_ebene_sichtbar')}; "
             f"kaufm. Funktion besetzt={fs.get('kaufmaennische_funktion_besetzt')}")
    L.append(f"- **Karriere:** offene Stellen: {lst(kar.get('offene_stellen'))}; "
             f"kaufm. Stellen: {lst(kar.get('kaufm_stellen'))}; Stand: {kar.get('stand') or '—'}")
    L.append(f"- **Nachfolge-Signale:** {'; '.join(d.get('nachfolge_signale') or []) or '—'}")
    L.append(f"- **Digitalisierung:** {d.get('digitalisierung') or '—'}")
    L.append(f"- **Besonderheiten:** {d.get('besonderheiten') or '—'}")
    L.append(f"- **Tonalität:** {d.get('tonalitaet_website') or '—'}  ·  "
             f"**Konfidenz:** {d.get('konfidenz') or '—'}")
    L.append("- **Ansprache-Hooks:**")
    for h in (d.get("ansprache_hooks") or []) or ["—"]:
        L.append(f"    - {h}")
    L.append(f"- **Negativ-Filter:** insolvenz={nf.get('insolvenz_hinweis')}, "
             f"onlineshop={nf.get('reiner_onlineshop')}, konzerntochter={nf.get('tochter_eines_konzerns')}")
    bel = d.get("belege") or []
    L.append(f"- **Belege ({len(bel)}):**")
    for b in bel:
        z = (b.get("zitat") or "").strip()
        L.append(f"    - [{b.get('signal_type')}] {b.get('aussage')}  ")
        L.append(f"      Zitat: „{z}“ — {b.get('quelle_url') or '—'}")
    if not bel:
        L.append("    - —")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Lauf
# --------------------------------------------------------------------------- #
def run(args):
    log = JsonLogger("pipeline.log")
    client = get_client("calvoran")
    crawl_cfg = config.load("crawl")
    router = ModelRouter(config.load("models"))

    cands = select_candidates(client, args.per_cluster)
    print(f"Selektiert: {len(cands)} Firmen "
          f"({dict(Counter(c['cluster'] for c in cands))}).")
    log.log("gemma_blind_selected", n=len(cands))

    ids = [c["id"] for c in cands]
    pages_by = load_pages(client, ids)
    haiku_by = load_haiku_dossiers(client, ids)

    records = []
    for idx, c in enumerate(cands, 1):
        cid = c["id"]
        pages = pages_by.get(cid) or []
        hd = (haiku_by.get(cid) or {}).get("dossier")
        if not pages or not hd:
            print(f"  [{idx}/{len(cands)}] {c['name'][:40]}: übersprungen "
                  f"(pages={len(pages)}, haiku={'ja' if hd else 'nein'})")
            log.log("gemma_blind_skip", id=cid, n_pages=len(pages), has_haiku=bool(hd))
            continue

        comp = {**c, "ges_vertreter_1": c.get("ges_vertreter") or ""}
        print(f"  [{idx}/{len(cands)}] {c['name'][:40]} (Gemma rechnet …)", flush=True)
        try:
            gdoss, gmeta = extractor.extract_dossier(
                router, comp, pages, crawl_cfg, backend="gemma_local", logger=log)
            gd = gdoss.model_dump()
        except Exception as e:
            print(f"      Gemma-Fehler: {type(e).__name__}: {str(e)[:120]}")
            log.log("gemma_blind_error", id=cid, error=str(e)[:200])
            continue

        fulltext = norm(" ".join(p["text"] for p in pages))
        m_haiku = measure(hd, fulltext)
        m_gemma = measure(gd, fulltext)

        gemma_is_A = (_h(cid) % 2 == 0)
        records.append({
            "n": len(records) + 1, "id": cid, "name": c["name"],
            "website": c.get("website"), "ort": f"{c.get('plz','')} {c.get('ort','')}".strip(),
            "branche_wz": c.get("branche_wz"), "ges_vertreter": c.get("ges_vertreter"),
            "cluster": c["cluster"], "score_klasse": c["score_klasse"],
            "score_total": c["score_total"], "n_pages": len(pages),
            "gemma_is_A": gemma_is_A,
            "haiku_dossier": hd, "gemma_dossier": gd,
            "haiku_measure": m_haiku, "gemma_measure": m_gemma,
            "gemma_meta": {k: gmeta.get(k) for k in
                           ("elapsed_s", "input_tokens", "output_tokens", "tokens_per_s")},
        })
        log.log("gemma_blind_done", id=cid, gemma_elapsed_s=gmeta.get("elapsed_s"),
                gemma_belegtreu=m_gemma, haiku_belegtreu=m_haiku)

    write_outputs(records)
    log.log("gemma_blind_complete", n=len(records))
    print(f"\nFertig: {len(records)} Side-by-Sides geschrieben.")


# --------------------------------------------------------------------------- #
# Output: Blind-Dokument + Auflösung + JSONL
# --------------------------------------------------------------------------- #
def write_outputs(records: list[dict]):
    out = Path(OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)

    # 1) Roh-JSONL (reproduzierbar)
    with open(out / "gemma-blind-dossier.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # 2) Blind-Dokument
    B = []
    B.append(f"# Gemma vs. Haiku — Blind-Vergleich Dossier-Aufgabe ({STAMP})")
    B.append("")
    B.append(f"{len(records)} reale Welle-1-Firmen, stratifiziert über die Makrocluster. "
             "Je Firma zwei Dossiers aus identischem Website-Input: **Modell A** und **Modell B**. "
             "Welches Modell A bzw. B ist, steht in der separaten Auflösung — erst nach deinem Urteil öffnen.")
    B.append("")
    B.append("**Beurteile je Firma:** Treffsicherheit Geschäftsmodell/Nachfolge-Signale, "
             "Brauchbarkeit der Ansprache-Hooks, Belegtreue (steht das Zitat plausibel für die Aussage?), "
             "keine Halluzination. Trag dein Urteil in die Urteilszeile.")
    B.append("")
    B.append("---")
    B.append("")
    for r in records:
        a_doss = r["gemma_dossier"] if r["gemma_is_A"] else r["haiku_dossier"]
        b_doss = r["haiku_dossier"] if r["gemma_is_A"] else r["gemma_dossier"]
        B.append(f"## {r['n']}. {r['name']}")
        B.append("")
        B.append(f"{r['ort']} · WZ {r['branche_wz']} · GF (Register): "
                 f"{r['ges_vertreter'] or '—'} · {r['website'] or '—'}")
        B.append(f"_Cluster {r['cluster']}, Score-Klasse {r['score_klasse']}, "
                 f"{r['n_pages']} Seiten gecrawlt_")
        B.append("")
        B.append("### Modell A")
        B.append("")
        B.append(render_dossier(a_doss))
        B.append("")
        B.append("### Modell B")
        B.append("")
        B.append(render_dossier(b_doss))
        B.append("")
        B.append("**Urteil:** ☐ A besser ☐ B besser ☐ gleichwertig ☐ beide schwach — "
                 "Notiz: ____________________")
        B.append("")
        B.append("---")
        B.append("")
    (out / "gemma-blind-dossier.md").write_text("\n".join(B), encoding="utf-8")

    # 3) Auflösung (Schlüssel + objektive Kennzahlen)
    K = []
    K.append(f"# Auflösung — Gemma vs. Haiku Blind-Vergleich ({STAMP})")
    K.append("")
    K.append("**Erst öffnen, nachdem du im Blind-Dokument je Firma geurteilt hast.**")
    K.append("")
    K.append("Belegtreue = wörtliche Zitate, die (6-Token-Fenster) im gecrawlten Text vorkommen "
             "/ Belege gesamt. Felder = gefüllte von 17. Zeit/Tokens nur Gemma (lokal).")
    K.append("")
    K.append("| # | Firma | Modell A | Modell B | Belegtreue Gemma | Belegtreue Haiku | "
             "Felder G/H | Gemma s | Gemma tok/s |")
    K.append("|---|---|---|---|---|---|---|---|---|")
    for r in records:
        a = "Gemma" if r["gemma_is_A"] else "Haiku"
        b = "Haiku" if r["gemma_is_A"] else "Gemma"
        mg, mh, gm = r["gemma_measure"], r["haiku_measure"], r["gemma_meta"]
        bt = lambda m: f"{m['belegtreu']}/{m['belege']}"
        K.append(f"| {r['n']} | {r['name'][:30]} | **{a}** | **{b}** | {bt(mg)} | {bt(mh)} | "
                 f"{mg['fields_filled']}/{mh['fields_filled']} | "
                 f"{gm.get('elapsed_s')} | {gm.get('tokens_per_s') or '—'} |")
    K.append("")

    # Aggregat
    n = len(records)
    g_bt = sum(r["gemma_measure"]["belegtreu"] for r in records)
    g_bn = sum(r["gemma_measure"]["belege"] for r in records)
    h_bt = sum(r["haiku_measure"]["belegtreu"] for r in records)
    h_bn = sum(r["haiku_measure"]["belege"] for r in records)
    g_ff = [r["gemma_measure"]["fields_filled"] for r in records if r["gemma_measure"]["fields_filled"] is not None]
    h_ff = [r["haiku_measure"]["fields_filled"] for r in records if r["haiku_measure"]["fields_filled"] is not None]
    g_s = [r["gemma_meta"]["elapsed_s"] for r in records if r["gemma_meta"].get("elapsed_s")]
    g_tps = [r["gemma_meta"]["tokens_per_s"] for r in records if r["gemma_meta"].get("tokens_per_s")]
    pct = lambda a, b: f"{100*a/b:.1f} %" if b else "—"
    avg = lambda xs: round(sum(xs)/len(xs), 1) if xs else "—"
    K.append("## Aggregat (objektiv, nicht das Urteil)")
    K.append("")
    K.append(f"- Firmen: **{n}**")
    K.append(f"- Belegtreue **Gemma {pct(g_bt, g_bn)}** ({g_bt}/{g_bn}) vs. "
             f"**Haiku {pct(h_bt, h_bn)}** ({h_bt}/{h_bn})")
    K.append(f"- Felder Ø (von 17): **Gemma {avg(g_ff)}** vs. **Haiku {avg(h_ff)}**")
    K.append(f"- Belege Ø/Firma: Gemma {avg([r['gemma_measure']['belege'] for r in records])}, "
             f"Haiku {avg([r['haiku_measure']['belege'] for r in records])}")
    K.append(f"- Gemma Laufzeit: Ø {avg(g_s)} s/Firma (Σ {round(sum(g_s),1) if g_s else '—'} s), "
             f"Ø {avg(g_tps)} tok/s — lokal, kein Cloud-Token")
    K.append("")
    K.append("> Belegtreue/Felder sind Hilfsindikatoren. Maßgeblich ist dein Side-by-Side-Urteil.")
    K.append("")
    (out / "gemma-blind-dossier-aufloesung.md").write_text("\n".join(K), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-cluster", type=int, default=3,
                    help="Firmen je Makrocluster (Default 3 -> ~21)")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
