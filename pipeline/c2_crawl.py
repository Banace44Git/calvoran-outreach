"""Phase 2: Crawler + Modernität -> calvoran.pages + companies.{tech_signals, modernity}.

Selektiert crawlbare Firmen (Website vorhanden, kein Holding/Dublette/excluded),
crawlt async über calvoran.crawler.crawl_many (httpx, Welle 1), rechnet den
deterministischen Website-Modernitäts-Score (calvoran.modernity) und persistiert
Seiten + Signale nach Supabase. Resumebar: was bereits tech_signals trägt, wird
übersprungen (Marker auf companies.tech_signals), außer --force.

    .venv/bin/python pipeline/c2_crawl.py --score 3 --limit 100 --report   # Pilot 100
    .venv/bin/python pipeline/c2_crawl.py --min-score 2                     # Welle 1 (Rest)
    .venv/bin/python pipeline/c2_crawl.py                                   # alles Übrige

Persistenz je Firma:
  companies.tech_signals (ohne home_html), .website_modernity_score (0-10 | NULL),
  .modernity_breakdown; pages-Zeilen je gecrawlter URL (upsert on company_id,url).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from _common import OUTPUT_DIR

from calvoran import config, modernity
from calvoran.crawler import crawl_many
from calvoran.db import get_client
from calvoran.logging import JsonLogger

WAVE = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Selektion
# --------------------------------------------------------------------------- #
def select_companies(client, *, score, min_score, limit, force):
    """Crawlbare Firmen, sortiert prioritaets_score desc, dann bilanzsumme desc."""
    cols = "id,name,website,domain,prioritaets_score,bilanzsumme_eur"
    out, step, start = [], 1000, 0
    while True:
        q = (client.table("companies").select(cols)
             .not_.is_("website", "null")
             .eq("holding_flag", False)
             .is_("dup_of", "null")
             .eq("excluded", False))
        if not force:
            q = q.is_("tech_signals", "null")
        r = q.range(start, start + step - 1).execute()
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step

    def prio(x):
        return x.get("prioritaets_score")

    cands = out
    if score is not None:
        cands = [x for x in cands if prio(x) is not None and round(prio(x)) == score]
    if min_score is not None:
        cands = [x for x in cands if prio(x) is not None and prio(x) >= min_score]
    cands.sort(key=lambda x: (
        -(prio(x) if prio(x) is not None else -1),
        -(x.get("bilanzsumme_eur") if x.get("bilanzsumme_eur") is not None else -1),
    ))
    return cands[:limit] if limit else cands


def select_by_ids(client, ids, limit):
    """Gezielter Re-Crawl über exakte company-UUIDs (ohne Score-/tech_signals-Filter)."""
    cols = "id,name,website,domain,prioritaets_score,bilanzsumme_eur"
    rows, step = [], 200
    for i in range(0, len(ids), step):
        r = client.table("companies").select(cols).in_("id", ids[i:i + step]).execute()
        rows.extend(r.data)
    rows = [x for x in rows if x.get("website")]
    rows.sort(key=lambda x: -(x.get("prioritaets_score") or -1))
    return rows[:limit] if limit else rows


def resolve_ids(args):
    """company-UUIDs aus --ids (kommagetrennt) oder --ids-file (Zeilen ODER JSONL mit 'id')."""
    import json
    from pathlib import Path
    if args.ids:
        return [x.strip() for x in args.ids.split(",") if x.strip()]
    if args.ids_file:
        ids = []
        for line in Path(args.ids_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            ids.append(json.loads(line).get("id") if line.startswith("{") else line)
        return [x for x in ids if x]
    return None


# --------------------------------------------------------------------------- #
# Persistenz
# --------------------------------------------------------------------------- #
def page_status(p: dict) -> str:
    err = p.get("error")
    if err == "robots":
        return "skipped_robots"
    if err:
        return "error"
    return "extracted_text" if (p.get("text") or "").strip() else "fetched"


def persist(client, company, result, mod_cfg, now_year, now_iso) -> dict:
    tech = dict(result.get("tech_signals") or {"reachable": False})
    if result.get("error") and "error" not in tech:
        tech["error"] = result["error"]
    score, breakdown = modernity.compute(tech, mod_cfg, now_year=now_year)
    # home_html (bis 200 KB) ist nur Rohstoff für den Score; nicht persistieren.
    tech_slim = {k: v for k, v in tech.items() if k != "home_html"}

    client.table("companies").update({
        "tech_signals": tech_slim,
        "website_modernity_score": score,
        "modernity_breakdown": breakdown,
        "updated_at": now_iso,
    }).eq("id", company["id"]).execute()

    rows, seen = [], set()
    for p in result.get("pages", []):
        url = p.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        row = {
            "company_id": company["id"],
            "url": url,
            "page_type": p.get("page_type"),
            "fetch_status": page_status(p),
            "http_status": p.get("http_status"),
            "text_content": (p.get("text") or None),
            "error_reason": p.get("error"),
            "crawl_wave": WAVE,
            "fetched_at": now_iso,
        }
        if p.get("page_type") == "home" and tech.get("reachable"):
            row["http_protocol"] = tech.get("http_version")
            row["response_headers"] = tech.get("headers")
            row["generator_tag"] = tech.get("generator")
            row["tech_features"] = {k: tech_slim.get(k) for k in (
                "scheme", "http_to_https_redirect", "viewport",
                "copyright_year", "last_modified_year", "generator")}
        rows.append(row)
    if rows:
        client.table("pages").upsert(rows, on_conflict="company_id,url").execute()

    return {"score": score, "breakdown": breakdown, "tech": tech_slim}


# --------------------------------------------------------------------------- #
# Report (Abnahme Phase 2)
# --------------------------------------------------------------------------- #
def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 1)


def write_report(audit, args) -> Path:
    out = Path(OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "phase2-crawl.jsonl", "w", encoding="utf-8") as f:
        for a in audit:
            f.write(json.dumps(a, ensure_ascii=False, default=str) + "\n")

    n = len(audit)
    reachable = [a for a in audit if a["reachable"]]
    nr = len(reachable)
    multi = [a for a in reachable if a["n_pages"] >= 3]
    keypage = [a for a in reachable if set(a["page_types"]) & {"about", "team"}]
    err = Counter(a["error"] or "unbekannt" for a in audit if not a["reachable"])
    page_err = Counter()
    for a in audit:
        for pe in a.get("page_errors", []):
            page_err[pe] += 1

    scores = [a["modernity_score"] for a in reachable if a["modernity_score"] is not None]
    hist = Counter(scores)
    none_n = sum(1 for a in reachable if a["modernity_score"] is None)

    pct = lambda k, d: f"{(100 * k / d):.1f} %" if d else "—"

    # 10-Site-Stichprobe über das Score-Spektrum für die Plausibilisierung.
    rs = sorted([a for a in reachable if a["modernity_score"] is not None],
                key=lambda a: a["modernity_score"])
    sample, seen = [], set()
    if rs:
        idxs = ([round(k * (len(rs) - 1) / 9) for k in range(10)]
                if len(rs) >= 10 else list(range(len(rs))))
        for j in idxs:
            if j not in seen:
                seen.add(j)
                sample.append(rs[j])

    L = []
    sel = (f"score == {args.score}" if args.score is not None else
           f"prioritaets_score >= {args.min_score}" if args.min_score is not None else
           "alle crawlbaren (Rest)")
    L.append(f"# Phase 2 — Crawl-Report ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    L.append("")
    L.append(f"Selektion: {sel}; Limit {args.limit or '—'}; Welle {WAVE} (httpx).")
    L.append("")
    L.append("## Erreichbarkeit")
    L.append("")
    L.append(f"- Gecrawlt: **{n}**")
    L.append(f"- Erreichbar: **{nr}** ({pct(nr, n)})")
    L.append(f"- Nicht erreichbar: **{n - nr}** ({pct(n - nr, n)})")
    L.append("")
    L.append("## Seitenauswahl (Nav-Heuristik)")
    L.append("")
    avg_pages = round(sum(a["n_pages"] for a in reachable) / nr, 2) if nr else 0
    L.append(f"- Seiten je erreichbarer Domain (Ø): **{avg_pages}**")
    L.append(f"- Domains mit ≥3 Seiten: **{len(multi)}** ({pct(len(multi), nr)})")
    L.append(f"- Domains mit About-/Team-Seite gefunden: **{len(keypage)}** ({pct(len(keypage), nr)})")
    pc = Counter(a["n_pages"] for a in reachable)
    L.append("- Verteilung Seitenzahl: " +
             ", ".join(f"{k}→{pc[k]}" for k in sorted(pc)))
    L.append("")
    L.append("## Fehlerliste (klassifiziert)")
    L.append("")
    if err:
        L.append("| Fehler (Domain-Ebene) | Anzahl |")
        L.append("|---|---|")
        for k, v in err.most_common():
            L.append(f"| {k} | {v} |")
    else:
        L.append("Keine Domain-Fehler.")
    if page_err:
        L.append("")
        L.append("Unterseiten-Fehler (erreichbare Domains): " +
                 ", ".join(f"{k}→{v}" for k, v in page_err.most_common()))
    L.append("")
    L.append("## Website-Modernität (0–10, deterministisch)")
    L.append("")
    if scores:
        L.append(f"- Mittelwert: **{round(sum(scores) / len(scores), 2)}**, Median: **{_median(scores)}**")
    L.append(f"- Ohne Score trotz erreichbar (NULL): {none_n}")
    L.append("- Histogramm: " + ", ".join(f"{s}→{hist[s]}" for s in range(0, 11) if hist[s]))
    L.append("")
    L.append("### Stichprobe (10 Sites zur Plausibilisierung)")
    L.append("")
    L.append("| Domain | Final-URL | Score | HTTP | Generator | Evidenz |")
    L.append("|---|---|---|---|---|---|")
    for a in sample:
        ev = ", ".join(a.get("evidenz") or [])
        gen = (a.get("generator") or "—")[:24]
        L.append(f"| {a.get('domain') or '—'} | {a.get('final_url') or '—'} | "
                 f"{a['modernity_score']} | {a.get('http_version') or '—'} | {gen} | {ev} |")
    L.append("")

    path = out / "phase2-crawl-report.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Lauf
# --------------------------------------------------------------------------- #
async def run(args):
    log = JsonLogger("pipeline.log")
    client = get_client("calvoran")
    crawl_cfg = config.load("crawl")
    mod_cfg = config.load("modernity")
    now_year = datetime.now(timezone.utc).year

    ids = resolve_ids(args)
    if ids:
        cands = select_by_ids(client, ids, args.limit)
        log.log("c2_selected", n=len(cands), mode="ids", n_ids=len(ids), limit=args.limit)
    else:
        cands = select_companies(client, score=args.score, min_score=args.min_score,
                                 limit=args.limit, force=args.force)
        log.log("c2_selected", n=len(cands), score=args.score, min_score=args.min_score,
                limit=args.limit, force=args.force)
    print(f"Selektiert: {len(cands)} Firmen.")
    if not cands:
        print("Nichts zu crawlen (alles erledigt oder leere Auswahl).")
        return

    audit, done = [], 0
    for i in range(0, len(cands), args.batch):
        batch = cands[i:i + args.batch]
        results = await crawl_many([c["website"] for c in batch], crawl_cfg, logger=log)
        now_iso = _now_iso()
        for company, result in zip(batch, results):
            rec = persist(client, company, result, mod_cfg, now_year, now_iso)
            tech, pages = rec["tech"], result.get("pages", [])
            audit.append({
                "id": company["id"], "name": company["name"],
                "website": company["website"], "domain": company.get("domain"),
                "prioritaets_score": company.get("prioritaets_score"),
                "reachable": bool(tech.get("reachable")),
                "error": tech.get("error") or result.get("error"),
                "n_pages": len(pages),
                "page_types": [p.get("page_type") for p in pages],
                "page_errors": [p.get("error") for p in pages if p.get("error")],
                "modernity_score": rec["score"],
                "evidenz": (rec["breakdown"] or {}).get("evidenz"),
                "final_url": tech.get("final_url"),
                "http_version": tech.get("http_version"),
                "generator": tech.get("generator"),
            })
        done += len(batch)
        log.log("c2_progress", done=done, total=len(cands))
        print(f"  {done}/{len(cands)} gecrawlt")

    if args.report:
        path = write_report(audit, args)
        print(f"Report: {path}")
    log.log("c2_done", n=len(audit))
    print(f"Fertig: {len(audit)} Firmen gecrawlt.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", type=int, default=None,
                    help="Nur Firmen mit gerundetem prioritaets_score == N (Pilot: 3)")
    ap.add_argument("--min-score", type=float, default=None,
                    help="Nur Firmen mit prioritaets_score >= N (Welle 1: 2)")
    ap.add_argument("--limit", type=int, default=0, help="Max. Firmenzahl (0 = alle)")
    ap.add_argument("--ids", default=None,
                    help="Kommagetrennte company-UUIDs (gezielter Re-Crawl, ohne Score-Filter)")
    ap.add_argument("--ids-file", dest="ids_file", default=None,
                    help="Datei mit company-UUIDs je Zeile ODER JSONL mit 'id'-Feld")
    ap.add_argument("--batch", type=int, default=30, help="Persistenz-Chunk")
    ap.add_argument("--force", action="store_true",
                    help="Auch bereits gecrawlte (tech_signals gesetzt) erneut crawlen")
    ap.add_argument("--report", action="store_true",
                    help="Markdown+JSONL-Report nach OUTPUT_DIR schreiben")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
