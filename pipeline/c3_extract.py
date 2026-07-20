"""Phase 3: Dossier-Extraktion -> calvoran.dossiers + signals.

Selektiert gecrawlte, erreichbare Firmen (tech_signals gesetzt, reachable=true,
kein Holding/Dublette/excluded) ohne bestehendes Dossier, lädt ihre Seiten aus
calvoran.pages, extrahiert über den Modell-Router (calvoran.extractor) ein
belegtes Dossier und persistiert es plus die belegpflichtigen Einzelsignale.
Resumebar: Firmen mit Dossier werden übersprungen, außer --force.

    .venv/bin/python pipeline/c3_extract.py --score 3 --limit 100 --report   # Pilot 100
    .venv/bin/python pipeline/c3_extract.py --min-score 2 --report           # Welle 1 (Rest)
    .venv/bin/python pipeline/c3_extract.py --backend sonnet --limit 3       # Backend-Test

Task-Routing (Default per gerundetem prioritaets_score): Score <=1 ->
dossier_score_0_1 (Gemma lokal, Haiku-Eskalation), Score >=2 -> dossier_score_2_3
(Haiku). Optional 5 %-Sonnet-Stichprobe (--sonnet-sample-pct) als QA-Quergegenlesen.

Persistenz je Firma:
  dossiers (upsert on company_id): dossier-JSON, konfidenz, model_backend,
  repair_count, escalated; signals (delete+insert je company_id): ein Eintrag je
  belegtem Signal mit NOT-NULL beleg_zitat/beleg_url. Belege ohne Zitat oder URL
  werden verworfen (Belegpflicht) und gezählt.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from _common import OUTPUT_DIR, norm
from benchmark_p0 import belegtreue, fields_filled

from calvoran import config, extractor
from calvoran.db import fetch_all, get_client
from calvoran.logging import JsonLogger
from calvoran.model_router import ModelRouter


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_for(score, override) -> str:
    if override:
        return override
    s = round(score) if score is not None else 2
    return "dossier_score_0_1" if s <= 1 else "dossier_score_2_3"


# --------------------------------------------------------------------------- #
# Selektion (resume: Firmen mit bestehendem Dossier überspringen)
# --------------------------------------------------------------------------- #
def existing_dossier_ids(client) -> set:
    rows = fetch_all(lambda: client.table("dossiers").select("id,company_id"))
    return {x["company_id"] for x in rows}


COLS = ("id,name,website,domain,prioritaets_score,bilanzsumme_eur,"
        "plz,ort,branche_wz,ges_vertreter,raw")


def _sort_prio(rows):
    def prio(x):
        return x.get("prioritaets_score")
    rows.sort(key=lambda x: (
        -(prio(x) if prio(x) is not None else -1),
        -(x.get("bilanzsumme_eur") if x.get("bilanzsumme_eur") is not None else -1),
    ))
    return rows


def select_by_ids(client, ids, limit):
    """Gezielter Re-Run über exakte company-UUIDs (impliziert force, ohne Score-Filter)."""
    rows, step = [], 200
    for i in range(0, len(ids), step):
        chunk = ids[i:i + step]
        r = client.table("companies").select(COLS).in_("id", chunk).execute()
        rows.extend(r.data)
    rows = _sort_prio(rows)
    return rows[:limit] if limit else rows


def select_companies(client, *, score, min_score, limit, force):
    """Erreichbar gecrawlte Firmen ohne Dossier, sortiert prioritaets_score desc, dann bilanzsumme desc."""
    out = fetch_all(lambda: (
        client.table("companies").select(COLS)
        .not_.is_("tech_signals", "null")
        .filter("tech_signals->>reachable", "eq", "true")
        .eq("holding_flag", False)
        .is_("dup_of", "null")
        .eq("excluded", False)))

    if not force:
        done = existing_dossier_ids(client)
        out = [x for x in out if x["id"] not in done]

    def prio(x):
        return x.get("prioritaets_score")

    cands = out
    if score is not None:
        cands = [x for x in cands if prio(x) is not None and round(prio(x)) == score]
    if min_score is not None:
        cands = [x for x in cands if prio(x) is not None and prio(x) >= min_score]
    return _sort_prio(cands)[:limit] if limit else _sort_prio(cands)


def load_pages(client, ids) -> dict:
    """Seiten mit Text je company_id, geformt wie der Crawler-Output (text statt text_content)."""
    pages_by: dict = {}
    step = 200
    for i in range(0, len(ids), step):
        chunk = ids[i:i + step]
        r = (client.table("pages")
             .select("company_id,url,page_type,text_content")
             .in_("company_id", chunk)
             .not_.is_("text_content", "null")
             .execute())
        for p in r.data:
            pages_by.setdefault(p["company_id"], []).append({
                "url": p["url"], "page_type": p["page_type"], "text": p["text_content"],
            })
    return pages_by


# --------------------------------------------------------------------------- #
# Persistenz
# --------------------------------------------------------------------------- #
def persist(client, company, dossier, meta, now_iso) -> dict:
    drow = {
        "company_id": company["id"],
        "dossier": dossier.model_dump(),
        "konfidenz": dossier.konfidenz,
        "model_backend": meta.get("backend"),
        "repair_count": int(meta.get("repair_count") or 0),
        "escalated": bool(meta.get("escalated")),
        "extracted_at": now_iso,
    }
    res = client.table("dossiers").upsert(drow, on_conflict="company_id").execute()
    dossier_id = res.data[0]["id"] if res.data else None

    # signals neu aufbauen (idempotent bei Re-Runs)
    client.table("signals").delete().eq("company_id", company["id"]).execute()
    srows, dropped = [], 0
    for b in dossier.belege:
        zit = (b.zitat or "").strip()
        url = (b.quelle_url or "").strip()
        if not zit or not url:  # Belegpflicht: ohne Zitat+URL kein Signal
            dropped += 1
            continue
        srows.append({
            "company_id": company["id"],
            "dossier_id": dossier_id,
            "signal_type": (b.signal_type or "sonstiges").strip() or "sonstiges",
            "value": b.aussage,
            "beleg_zitat": zit,
            "beleg_url": url,
        })
    if srows:
        client.table("signals").insert(srows).execute()
    return {"n_signals": len(srows), "dropped_belege": dropped}


# --------------------------------------------------------------------------- #
# Report (Abnahme Phase 3: <10 % Feldfehler, jedes Signal mit Beleg)
# --------------------------------------------------------------------------- #
def write_report(audit, args) -> Path:
    out = Path(OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "phase3-extract.jsonl", "w", encoding="utf-8") as f:
        for a in audit:
            f.write(json.dumps(a, ensure_ascii=False, default=str) + "\n")

    n = len(audit)
    ok = [a for a in audit if a.get("ok")]
    nk = len(ok)
    pct = lambda k, d: f"{(100 * k / d):.1f} %" if d else "—"

    avg = lambda key: (round(sum(a[key] for a in ok) / nk, 2) if nk else 0)
    bt_ok = sum(a["belegtreu"] for a in ok)
    bt_tot = sum(a["n_belege"] for a in ok)
    no_beleg = sum(1 for a in ok if a["n_belege"] == 0)
    backends = Counter(a["backend"] for a in ok if a.get("backend"))
    escalated = sum(1 for a in ok if a.get("escalated"))
    repairs = sum(1 for a in ok if (a.get("repair_count") or 0) > 0)
    in_tok = sum(a.get("input_tokens") or 0 for a in ok)
    out_tok = sum(a.get("output_tokens") or 0 for a in ok)
    n_signals = sum(a.get("n_signals", 0) for a in ok)
    dropped = sum(a.get("dropped_belege", 0) for a in ok)

    sel = ("gezielter Re-Run (IDs)" if (args.ids or args.ids_file) else
           f"score == {args.score}" if args.score is not None else
           f"prioritaets_score >= {args.min_score}" if args.min_score is not None else
           "alle erreichbar-gecrawlten ohne Dossier")

    L = []
    L.append(f"# Phase 3 — Extraktions-Report ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    L.append("")
    L.append(f"Selektion: {sel}; Limit {args.limit or '—'}; "
             f"Task {args.task or 'auto (Score)'}{'; Backend ' + args.backend if args.backend else ''}"
             f"{f'; Sonnet-Stichprobe {args.sonnet_sample_pct} %' if args.sonnet_sample_pct else ''}.")
    L.append("")
    L.append("## Durchsatz")
    L.append("")
    L.append(f"- Verarbeitet: **{n}**")
    L.append(f"- Erfolgreich extrahiert: **{nk}** ({pct(nk, n)})")
    L.append(f"- Fehlgeschlagen/übersprungen: **{n - nk}**")
    L.append(f"- Backend-Verteilung: " + (", ".join(f"{b}→{c}" for b, c in backends.most_common()) or "—"))
    L.append(f"- Repair-Retries nötig: {repairs}; eskaliert: {escalated}")
    L.append(f"- Tokens: Σ in {in_tok}, Σ out {out_tok}; Ø Laufzeit {avg('elapsed_s')} s/Firma")
    L.append("")
    L.append("## Qualität (Abnahme)")
    L.append("")
    L.append(f"- Felder gefüllt (Ø von 17): **{avg('fields_filled')}**")
    L.append(f"- Belege je Dossier (Ø): **{avg('n_belege')}**")
    L.append(f"- **Belegtreue gesamt: {pct(bt_ok, bt_tot)}** "
             f"({bt_ok}/{bt_tot} Zitate wörtlich im gecrawlten Text)")
    L.append(f"- Dossiers ohne Beleg: **{no_beleg}** ({pct(no_beleg, nk)})")
    L.append(f"- Signale persistiert: **{n_signals}**; verworfene Belege (kein Zitat/URL): {dropped}")
    L.append("")
    L.append("> Belegtreue = Anteil der Belege, deren wörtliches Zitat (normalisiert, "
             "6-Token-Fenster) im gecrawlten Seitentext der Firma vorkommt.")
    L.append("")

    # Stichprobe: 20 über das Score-Spektrum, nach Score absteigend gestreut.
    rs = sorted(ok, key=lambda a: -(a.get("prioritaets_score") or 0))
    sample = []
    if rs:
        k = min(20, len(rs))
        idxs = ([round(j * (len(rs) - 1) / (k - 1)) for j in range(k)] if k > 1 else [0])
        seen = set()
        for j in idxs:
            if j not in seen:
                seen.add(j)
                sample.append(rs[j])
    L.append(f"## Stichprobe ({len(sample)} Dossiers zur Gegenkontrolle)")
    L.append("")
    L.append("| Firma | Score | Backend | Felder | Belege (treu) | Nachfolge-Signale | Geschäftsmodell |")
    L.append("|---|---|---|---|---|---|---|")
    for a in sample:
        name = (a.get("name") or "")[:30]
        nach = "; ".join(a.get("nachfolge_signale") or [])[:48] or "—"
        gm = (a.get("geschaeftsmodell") or "—")[:60]
        be = (a.get("backend") or "—").replace("anthropic:", "").replace("ollama:", "")[:18]
        L.append(f"| {name} | {a.get('prioritaets_score')} | {be} | {a['fields_filled']} | "
                 f"{a['n_belege']} ({a['belegtreu']}) | {nach} | {gm} |")
    L.append("")

    path = out / "phase3-extract-report.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Lauf
# --------------------------------------------------------------------------- #
def resolve_ids(args) -> list | None:
    """company-UUIDs aus --ids (kommagetrennt) oder --ids-file (Zeilen ODER JSONL mit 'id')."""
    if args.ids:
        return [x.strip() for x in args.ids.split(",") if x.strip()]
    if args.ids_file:
        ids = []
        for line in Path(args.ids_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                v = json.loads(line).get("id")
                if v:
                    ids.append(v)
            else:
                ids.append(line)
        return ids
    return None


def run(args):
    log = JsonLogger("pipeline.log")
    client = get_client("calvoran")
    crawl_cfg = config.load("crawl")
    router = ModelRouter(config.load("models"))

    ids = resolve_ids(args)
    if ids:
        cands = select_by_ids(client, ids, args.limit)
        # Resume: bereits dossierte Firmen überspringen, damit ein Neustart eines langen
        # --ids-file-Laufs (z.B. Nachfolge-Crawl) die teuren Gemma-Dossiers nicht neu rechnet.
        # --force erzwingt das Neu-Extrahieren (bewusster Re-Run).
        if not args.force:
            done = existing_dossier_ids(client)
            cands = [x for x in cands if x["id"] not in done]
        log.log("c3_selected", n=len(cands), mode="ids", n_ids=len(ids),
                limit=args.limit, force=args.force)
    else:
        cands = select_companies(client, score=args.score, min_score=args.min_score,
                                 limit=args.limit, force=args.force)
        log.log("c3_selected", n=len(cands), score=args.score, min_score=args.min_score,
                limit=args.limit, force=args.force)
    print(f"Selektiert: {len(cands)} Firmen.")
    if not cands:
        print("Nichts zu extrahieren (alles erledigt oder leere Auswahl).")
        return

    pages_by = load_pages(client, [c["id"] for c in cands])
    sample_every = (round(100 / args.sonnet_sample_pct)
                    if args.sonnet_sample_pct and args.sonnet_sample_pct > 0 else 0)

    audit, done = [], 0
    for idx, company in enumerate(cands):
        pages = pages_by.get(company["id"]) or []
        rec = {
            "id": company["id"], "name": company["name"],
            "website": company["website"], "domain": company.get("domain"),
            "prioritaets_score": company.get("prioritaets_score"),
            "n_pages": len(pages), "ok": False,
        }
        if not pages:
            rec["error"] = "keine_seiten_mit_text"
            log.log("c3_skip", id=company["id"], reason="no_pages")
            audit.append(rec)
            continue

        task = task_for(company.get("prioritaets_score"), args.task)
        backend = args.backend or ("sonnet" if sample_every and idx % sample_every == 0 else None)
        # build_user_text erwartet ges_vertreter_1 (Register-GF) + gegenstand
        # (Handelsregister-Unternehmensgegenstand aus dem North-Data-Rohsatz).
        comp = {**company, "ges_vertreter_1": company.get("ges_vertreter") or "",
                "gegenstand": (company.get("raw") or {}).get("Gegenstand")}
        try:
            dossier, meta = extractor.extract_dossier(
                router, comp, pages, crawl_cfg, task=task, backend=backend, logger=log)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}:{str(e)[:140]}"
            log.log("c3_extract_error", id=company["id"], task=task, backend=backend,
                    error=str(e)[:200])
            audit.append(rec)
            continue

        pres = persist(client, company, dossier, meta, _now_iso())

        fulltext = norm(" ".join(p["text"] for p in pages))
        bok, btot = belegtreue(dossier.belege, fulltext)
        rec.update({
            "ok": True, "task": task, "backend": meta.get("backend"),
            "repair_count": meta.get("repair_count"), "escalated": meta.get("escalated"),
            "elapsed_s": meta.get("elapsed_s"),
            "input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
            "fields_filled": fields_filled(dossier),
            "n_belege": btot, "belegtreu": bok,
            "n_signals": pres["n_signals"], "dropped_belege": pres["dropped_belege"],
            "konfidenz": dossier.konfidenz,
            "geschaeftsmodell": dossier.geschaeftsmodell,
            "nachfolge_signale": dossier.nachfolge_signale,
            "ansprache_hooks": dossier.ansprache_hooks,
        })
        audit.append(rec)
        done += 1
        # Supabase-Gateway kappt HTTP/2 nach ~10.000 Requests je Verbindung; persist()
        # macht 3 Writes/Firma (Dossier + Signals delete/insert). Alle 3.000 Firmen
        # (~9.000 Requests) neu verbinden. get_client() liefert einen frischen Client.
        if done % 3000 == 0:
            client = get_client("calvoran")
            log.log("client_reconnect", done=done)
        if done % 10 == 0 or done == len(cands):
            log.log("c3_progress", done=done, total=len(cands))
            print(f"  {done}/{len(cands)} extrahiert")

    if args.report:
        path = write_report(audit, args)
        print(f"Report: {path}")
    log.log("c3_done", n=len(audit), ok=sum(1 for a in audit if a.get("ok")))
    print(f"Fertig: {sum(1 for a in audit if a.get('ok'))}/{len(audit)} Dossiers extrahiert.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", type=int, default=None,
                    help="Nur Firmen mit gerundetem prioritaets_score == N (Pilot: 3)")
    ap.add_argument("--min-score", type=float, default=None,
                    help="Nur Firmen mit prioritaets_score >= N (Welle 1: 2)")
    ap.add_argument("--limit", type=int, default=0, help="Max. Firmenzahl (0 = alle)")
    ap.add_argument("--task", default=None,
                    help="Task-Override (dossier_score_2_3 | dossier_score_0_1); Default: auto per Score")
    ap.add_argument("--backend", default=None,
                    help="Backend erzwingen (haiku|sonnet|gemma_local), umgeht Task-Routing")
    ap.add_argument("--sonnet-sample-pct", type=float, default=0.0,
                    help="QA: dieser Prozentsatz (deterministisch) über Sonnet statt Task-Backend")
    ap.add_argument("--ids", default=None,
                    help="Kommagetrennte company-UUIDs (gezielter Re-Run, ohne Score-Filter)")
    ap.add_argument("--ids-file", dest="ids_file", default=None,
                    help="Datei mit company-UUIDs (eine je Zeile) ODER JSONL mit 'id'-Feld")
    ap.add_argument("--force", action="store_true",
                    help="Auch Firmen mit bestehendem Dossier erneut extrahieren")
    ap.add_argument("--report", action="store_true",
                    help="Markdown+JSONL-Report nach OUTPUT_DIR schreiben")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
