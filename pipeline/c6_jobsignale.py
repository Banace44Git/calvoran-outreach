"""Phase A Job-Signal: BA-Jobsuche -> job_postings + job_matches.

Sucht eine Zielfirma per Stellenanzeige einen GF / eine kaufmännische Leitung /
zweite Führungsebene, ist das bei Inhabern 58+ ein Übergabe-Indikator. Die API
kann nicht nach PLZ filtern -> bundesweiter Scan je Keyword (config/jobsignale.yaml),
Match lokal gegen calvoran.companies (calvoran/matching.py, PLZ-Blocking).

    .venv/bin/python pipeline/c6_jobsignale.py --backfill 28      # Erstlauf (API-Maximum)
    .venv/bin/python pipeline/c6_jobsignale.py --since 7          # Wochenlauf
    .venv/bin/python pipeline/c6_jobsignale.py --since 7 --dry-run
    .venv/bin/python pipeline/c6_jobsignale.py --rematch          # Matching neu, ohne API
    .venv/bin/python pipeline/c6_jobsignale.py --reprio           # Prio aus aktuellem gf_alter
    .venv/bin/python pipeline/c6_jobsignale.py --report           # KPI-Markdown nach OUTPUT_DIR

Idempotent: Dedup über refnr (Anzeige) und (posting_id, company_id) (Match); bestehende
Match-Stati überleben Re-Runs (ignore_duplicates), Reviews werden nie überschrieben.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone

import yaml

from _common import PROJECT_ROOT, OUTPUT_DIR

from calvoran.ba_jobsuche import (ANZEIGE_URL, BaJobsucheClient, lokationen,
                                  parse_posting, snap_veroeffentlichtseit)
from calvoran.db import get_client
from calvoran.logging import JsonLogger
from calvoran.matching import CompanyIndex, norm_text, prio_from_alter
from pathlib import Path

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "jobsignale.yaml")
SELECTION_FILE = os.path.join(OUTPUT_DIR, "selection.jsonl")
_STUFEN_SORT = {"exakt": 0, "fuzzy": 1, "region": 2, "fuzzy_grenzfall": 3}
_PRIO_SORT = {"hoch": 0, "unbekannt": 1, "mittel": 2, "niedrig": 3}


def load_cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_all(client, table: str, columns: str, order: str = "id") -> list[dict]:
    out, step, start = [], 1000, 0
    while True:
        r = (client.table(table).select(columns).order(order)
             .range(start, start + step - 1).execute())
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step
    return out


def load_companies(client) -> list[dict]:
    """Match-Universum: alle Firmen außer Dubletten (die würden doppelt matchen)."""
    rows = fetch_all(client, "companies", "id,name,plz,gf_alter,dup_of")
    return [r for r in rows if not r.get("dup_of")]


def welle1_ids() -> set[str]:
    """company_ids der Welle-1-Kuratierung (Kontext-Flag für Sichtung/Report)."""
    ids = set()
    try:
        with open(SELECTION_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    ids.add(json.loads(line).get("company_id"))
    except FileNotFoundError:
        pass
    return ids


def api_pull(cfg: dict, tage: int) -> tuple[dict[str, dict], Counter, int]:
    """Alle Keywords scannen; Dedup über refnr (erstes Keyword gewinnt).

    `was` matcht den ganzen Anzeigentext — der Titel-Filter hält nur Anzeigen, deren
    Titel/Hauptberuf tatsächlich eine Führungsfunktion nennt (Anzahl Drops im Rückgabewert).
    """
    api = cfg["api"]
    excludes = [e.lower() for e in (cfg.get("exclude_arbeitgeber") or [])]
    tf = cfg.get("titel_filter") or {}
    begriffe = [norm_text(b) for b in tf.get("begriffe", [])] if tf.get("aktiv") else []
    postings: dict[str, dict] = {}
    je_keyword: Counter = Counter()
    titel_drops = 0
    with BaJobsucheClient(drossel_sekunden=api["drossel_sekunden"]) as ba:
        for kw in cfg["keywords"]:
            for item in ba.search_all(
                    kw, veroeffentlichtseit=tage, size=api["size"],
                    max_pages=api["max_pages_je_keyword"],
                    zeitarbeit=api["zeitarbeit"], pav=api["pav"]):
                p = parse_posting(item, kw)
                if p is None:
                    continue
                if any(e in p["arbeitgeber"].lower() for e in excludes):
                    continue
                if begriffe:
                    titel = norm_text(f"{p['titel']} {p['beruf'] or ''}")
                    if not any(b in titel for b in begriffe):
                        titel_drops += 1
                        continue
                je_keyword[kw] += 1
                postings.setdefault(p["refnr"], p)
    return postings, je_keyword, titel_drops


def match_postings(postings: list[dict], idx: CompanyIndex, cfg: dict) -> tuple[dict, int]:
    """refnr -> Liste Match-Dicts; zweiter Wert: Anzeigen ohne verwertbare PLZ."""
    m_cfg = cfg["match"]
    ohne_plz = 0
    by_refnr: dict[str, list[dict]] = {}
    for p in postings:
        loks = lokationen(p["raw"]) if p.get("raw") else []
        if not loks and (p.get("plz") or p.get("ort")):
            loks = [(p.get("plz"), p.get("ort"))]
        if not any(plz for plz, _ in loks):
            ohne_plz += 1
            continue
        ms = idx.match_posting(p["arbeitgeber"], loks,
                               fuzzy_auto=m_cfg["fuzzy_auto"],
                               fuzzy_review=m_cfg["fuzzy_review"])
        if ms:
            by_refnr[p["refnr"]] = ms
    return by_refnr, ohne_plz


def print_match_sample(by_refnr: dict, postings_by_refnr: dict, firmen_by_id: dict,
                       w1: set[str], limit: int = 30) -> None:
    flat = []
    for refnr, ms in by_refnr.items():
        for m in ms:
            flat.append((refnr, m))
    flat.sort(key=lambda x: (_STUFEN_SORT[x[1]["match_stufe"]],
                             _PRIO_SORT[prio_from_alter(x[1]["gf_alter"])],
                             -(x[1]["match_score"] or 0)))
    print(f"\n--- Stichprobe (max {limit} von {len(flat)} Matches) ---")
    for refnr, m in flat[:limit]:
        p = postings_by_refnr[refnr]
        c = firmen_by_id.get(m["company_id"], {})
        w1_flag = " [Welle1]" if m["company_id"] in w1 else ""
        print(f"[{m['match_stufe']:>15} {m['match_score']:>5}] "
              f"prio={prio_from_alter(m['gf_alter']):<9} gf_alter={m['gf_alter'] or '—':<4} "
              f"| {p['arbeitgeber']!r} ({p['plz']} {p['ort']}) "
              f"<-> {c.get('name')!r} (PLZ {c.get('plz')}){w1_flag}\n"
              f"{'':>24}» {p['titel']} | {ANZEIGE_URL.format(refnr=refnr)}")


def upsert_postings(client, postings: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [{**p, "letzte_sichtung": now} for p in postings]  # erste_sichtung: DB-Default
    for chunk in chunked(rows, 200):
        client.table("job_postings").upsert(chunk, on_conflict="refnr").execute()


def posting_ids(client, refnrs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in chunked(refnrs, 150):
        r = client.table("job_postings").select("id,refnr").in_("refnr", chunk).execute()
        out.update({row["refnr"]: row["id"] for row in r.data})
    return out


def insert_matches(client, by_refnr: dict, id_by_refnr: dict) -> int:
    rows = []
    for refnr, ms in by_refnr.items():
        pid = id_by_refnr.get(refnr)
        if not pid:
            continue
        for m in ms:
            rows.append({
                "posting_id": pid,
                "company_id": m["company_id"],
                "match_stufe": m["match_stufe"],
                "match_score": m["match_score"],
                "prio": prio_from_alter(m["gf_alter"]),
            })
    for chunk in chunked(rows, 200):
        # ignore_duplicates: bestehende Matches (inkl. Review-Status) bleiben unberührt.
        client.table("job_matches").upsert(
            chunk, on_conflict="posting_id,company_id", ignore_duplicates=True).execute()
    return len(rows)


def cmd_fetch(args, cfg: dict, log: JsonLogger) -> None:
    gewuenscht = args.backfill if args.backfill else (args.since or cfg["api"]["veroeffentlichtseit"])
    # v6 kennt nur 1/7/14/28 — ungültige Werte würden STILL ungefiltert liefern.
    tage = snap_veroeffentlichtseit(int(gewuenscht))
    print(f"BA-Scan: {len(cfg['keywords'])} Keywords, veroeffentlichtseit={tage} Tage "
          f"(angefragt {gewuenscht}, API kennt nur 1/7/14/28) "
          f"{'(DRY-RUN)' if args.dry_run else ''}")
    postings, je_keyword, titel_drops = api_pull(cfg, tage)
    print(f"Anzeigen (dedupliziert): {len(postings)}  "
          f"| Titel-Filter verworfen: {titel_drops}  | je Keyword: {dict(je_keyword)}")

    client = get_client()
    firmen = load_companies(client)
    idx = CompanyIndex(firmen, plz_praefix_stellen=cfg["match"]["plz_praefix_stellen"])
    firmen_by_id = {r["id"]: r for r in firmen}
    by_refnr, ohne_plz = match_postings(list(postings.values()), idx, cfg)
    n_matches = sum(len(v) for v in by_refnr.values())
    stufen = Counter(m["match_stufe"] for ms in by_refnr.values() for m in ms)
    print(f"Matches: {n_matches} auf {len(by_refnr)} Anzeigen "
          f"| Stufen: {dict(stufen)} | Anzeigen ohne PLZ (übersprungen): {ohne_plz}")

    if args.dry_run:
        print_match_sample(by_refnr, postings, firmen_by_id, welle1_ids())
        print("\nDRY-RUN: nichts geschrieben.")
        return

    upsert_postings(client, list(postings.values()))
    id_by_refnr = posting_ids(client, list(postings.keys()))
    n_rows = insert_matches(client, by_refnr, id_by_refnr)
    log.log("jobsignale_fetch", tage=tage, anzeigen=len(postings),
            matches=n_matches, match_zeilen=n_rows, ohne_plz=ohne_plz,
            stufen=dict(stufen), je_keyword=dict(je_keyword))
    print(f"Geschrieben: {len(postings)} postings (upsert), {n_rows} match-Zeilen "
          f"(Bestand bleibt unberührt).")
    print_match_sample(by_refnr, postings, firmen_by_id, welle1_ids(), limit=15)


def cmd_rematch(args, cfg: dict, log: JsonLogger) -> None:
    """Matching über den Bestand neu rechnen (Schwellwert-Tuning) — ohne API.

    Nur status='neu' wird angepasst/gelöscht; alles Gesichtete bleibt unberührt.
    """
    client = get_client()
    postings = fetch_all(client, "job_postings", "id,refnr,arbeitgeber,plz,ort,raw")
    firmen = load_companies(client)
    idx = CompanyIndex(firmen, plz_praefix_stellen=cfg["match"]["plz_praefix_stellen"])
    by_refnr, ohne_plz = match_postings(postings, idx, cfg)
    neu: dict[tuple[str, str], dict] = {}
    pid_by_refnr = {p["refnr"]: p["id"] for p in postings}
    for refnr, ms in by_refnr.items():
        for m in ms:
            neu[(pid_by_refnr[refnr], m["company_id"])] = m

    bestand = fetch_all(client, "job_matches",
                        "id,posting_id,company_id,match_stufe,match_score,status")
    n_upd = n_del = n_ins = 0
    for b in bestand:
        key = (b["posting_id"], b["company_id"])
        frisch = neu.pop(key, None)
        if b["status"] != "neu":
            continue  # gesichtete Matches nie anfassen
        if frisch is None:
            client.table("job_matches").delete().eq("id", b["id"]).execute()
            n_del += 1
        elif (frisch["match_stufe"] != b["match_stufe"]
              or frisch["match_score"] != b["match_score"]):
            client.table("job_matches").update({
                "match_stufe": frisch["match_stufe"],
                "match_score": frisch["match_score"],
            }).eq("id", b["id"]).execute()
            n_upd += 1
    rows = [{"posting_id": pid, "company_id": cid, "match_stufe": m["match_stufe"],
             "match_score": m["match_score"], "prio": prio_from_alter(m["gf_alter"])}
            for (pid, cid), m in neu.items()]
    for chunk in chunked(rows, 200):
        client.table("job_matches").upsert(
            chunk, on_conflict="posting_id,company_id", ignore_duplicates=True).execute()
    n_ins = len(rows)
    log.log("jobsignale_rematch", aktualisiert=n_upd, geloescht=n_del, neu=n_ins,
            ohne_plz=ohne_plz)
    print(f"Rematch: {n_ins} neu, {n_upd} aktualisiert, {n_del} gelöscht "
          f"(nur status='neu'; {len(bestand)} Bestand).")


def cmd_reprio(args, cfg: dict, log: JsonLogger) -> None:
    """Prio aus aktuellem companies.gf_alter nachziehen (externe GF-Anreicherung läuft)."""
    client = get_client()
    matches = fetch_all(client, "job_matches", "id,company_id,prio,status")
    alter_by_id = {r["id"]: r.get("gf_alter")
                   for r in fetch_all(client, "companies", "id,gf_alter")}
    n = 0
    for m in matches:
        if m["status"] == "irrelevant":
            continue
        soll = prio_from_alter(alter_by_id.get(m["company_id"]))
        if soll != m["prio"]:
            client.table("job_matches").update({"prio": soll}).eq("id", m["id"]).execute()
            n += 1
    log.log("jobsignale_reprio", matches=len(matches), aktualisiert=n)
    print(f"Reprio: {n} von {len(matches)} Matches aktualisiert.")


def cmd_report(args, cfg: dict, log: JsonLogger) -> None:
    client = get_client()
    postings = fetch_all(client, "job_postings",
                         "id,refnr,titel,arbeitgeber,plz,ort,keyword,veroeffentlicht_am")
    matches = fetch_all(client, "job_matches",
                        "id,posting_id,company_id,match_stufe,match_score,prio,status")
    firmen_by_id = {r["id"]: r for r in fetch_all(client, "companies", "id,name,plz,gf_alter")}
    p_by_id = {p["id"]: p for p in postings}
    w1 = welle1_ids()

    def block(counter: Counter, titel: str) -> str:
        zeilen = "\n".join(f"| {k} | {v} |" for k, v in counter.most_common())
        return f"### {titel}\n\n| | Anzahl |\n|---|---|\n{zeilen}\n"

    monat = Counter((m_p.get("veroeffentlicht_am") or "")[:7] or "unbekannt"
                    for m in matches if (m_p := p_by_id.get(m["posting_id"])))
    relevante = [m for m in matches if m["status"] in ("relevant", "outreach")]
    briefe = gespraeche = 0
    if relevante:
        cids = list({m["company_id"] for m in relevante})
        for chunk in chunked(cids, 150):
            r = (client.table("outreach").select("company_id")
                 .in_("company_id", chunk).eq("channel", "letter").execute())
            briefe += len({row["company_id"] for row in r.data})
            r = (client.table("outreach_calls").select("company_id,outcome")
                 .in_("company_id", chunk)
                 .in_("outcome", ["gesprochen", "termin", "rueckruf_vereinbart"]).execute())
            gespraeche += len({row["company_id"] for row in r.data})

    heute = datetime.now().strftime("%Y-%m-%d")
    md = [f"# Job-Signal-Report ({heute})\n",
          f"Anzeigen im Bestand: **{len(postings)}** | Matches: **{len(matches)}** | "
          f"davon Welle-1-Firmen: {sum(1 for m in matches if m['company_id'] in w1)}\n",
          block(Counter(m["status"] for m in matches), "Status"),
          block(Counter(m["prio"] for m in matches), "Priorität (gf_alter)"),
          block(Counter(m["match_stufe"] for m in matches), "Match-Stufe"),
          block(monat, "Matches je Monat (Veröffentlichung)"),
          block(Counter(p["keyword"] for p in postings), "Anzeigen je Keyword"),
          "### KPI\n",
          f"- Listen-Matches gesamt: {len(matches)} (relevant/outreach: {len(relevante)})",
          f"- Briefquote: {briefe}/{len(relevante)} relevante Firmen mit Brief",
          f"- Gesprächsquote: {gespraeche}/{len(relevante)} relevante Firmen mit Gespräch/Termin\n",
          "### Offene relevante Matches\n"]
    offene = sorted((m for m in matches if m["status"] == "relevant"),
                    key=lambda m: _PRIO_SORT[m["prio"]])
    for m in offene[:40]:
        p = p_by_id.get(m["posting_id"], {})
        c = firmen_by_id.get(m["company_id"], {})
        md.append(f"- **{c.get('name')}** (gf_alter {c.get('gf_alter') or '—'}, prio {m['prio']}) "
                  f"— »{p.get('titel')}« | {ANZEIGE_URL.format(refnr=p.get('refnr'))}")

    out = Path(OUTPUT_DIR) / f"jobsignale-report-{heute}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    log.log("jobsignale_report", pfad=str(out), postings=len(postings), matches=len(matches))
    print(f"Report: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--backfill", type=int, metavar="TAGE",
                   help="Erstlauf; API-Maximum sind 28 Tage Rückschau")
    g.add_argument("--since", type=int, metavar="TAGE",
                   help="Lauf über die letzten N Tage (Default aus jobsignale.yaml)")
    g.add_argument("--rematch", action="store_true",
                   help="Matching über den Bestand neu rechnen (ohne API)")
    g.add_argument("--reprio", action="store_true",
                   help="Prio aus aktuellem companies.gf_alter neu setzen")
    g.add_argument("--report", action="store_true", help="KPI-Markdown nach OUTPUT_DIR")
    ap.add_argument("--dry-run", action="store_true", help="nichts schreiben (nur fetch-Modi)")
    args = ap.parse_args()

    cfg = load_cfg()
    log = JsonLogger("jobsignale.log")
    if args.rematch:
        cmd_rematch(args, cfg, log)
    elif args.reprio:
        cmd_reprio(args, cfg, log)
    elif args.report:
        cmd_report(args, cfg, log)
    else:
        cmd_fetch(args, cfg, log)


if __name__ == "__main__":
    main()
