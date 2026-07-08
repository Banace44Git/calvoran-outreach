"""Phase 4: Bedarfs-Scoring + Clusterung -> calvoran.scores.

Deterministisch, config-getrieben (config/scoring.yaml, config/clusters.yaml),
kein LLM. Liest Stammdaten aus companies und die strukturierten Dossier-Felder
als Source-of-Truth (familienunternehmen, fuehrungsstruktur, karriere,
negativ_filter, nachfolge_signale). Die signals-Tabelle ist NUR Beleg-/Briefing-
Schicht fuer die Klartext-begruendung, NICHT Score-Input -- Belege sind bei
Haiku/temp 0 lauflabil, die Dossier-Felder sind stabil (Handoff 2026-06-11).
Deshalb weicht der Code bewusst von den scoring.yaml-Kommentaren ab, die noch
auf `signals(...)` verweisen (familienhinweis, offene_kaufm_stelle).

    .venv/bin/python pipeline/c4_score_cluster.py --report           # Dossiers ohne Score
    .venv/bin/python pipeline/c4_score_cluster.py --force --report   # alle neu scoren
    .venv/bin/python pipeline/c4_score_cluster.py --ids <uuid,...>   # gezielter Re-Run

Pro Firma: scores (upsert on company_id) mit score_total, score_klasse
(A>=9, B>=5, C>=0; KO bei Holding/Konzerntochter/Insolvenz), breakdown
(auditierbar je Kriterium), begruendung (= Anruf-Briefing aus Kennzahlen +
Dossier + Belegen), cluster_branche/groessenband/cluster_key.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from _common import OUTPUT_DIR, norm, wz2

from calvoran import config
from calvoran.db import get_client
from calvoran.logging import JsonLogger
from calvoran.schemas import Dossier
from config import keywords

# kaufm. Stellen-Begriffe als Backstop ueber dossier.karriere.offene_stellen,
# falls der Extraktor sie nicht in kaufm_stellen einsortiert hat.
KAUFM_TERMS = tuple(t.lower() for t in keywords.EXCLUDE_TERMS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    """Config-Schwellen robust nach float (YAML-Underscores '1_500_000' tolerieren)."""
    if v is None:
        return None
    return float(str(v).replace("_", ""))


def _entry(punkte, hit, wert=None) -> dict:
    return {"hit": bool(hit), "punkte": (punkte if hit else 0), "wert": wert}


def wz2_of(company) -> str:
    return company.get("wz2") or wz2(company.get("branche_wz") or "")


def gf_alter_at(company, scored_year):
    """GF-Alter zum scored_at neu rechnen (aus Geburtsjahr), sonst Import-Alter, sonst unbekannt."""
    gj = company.get("gf_geburtsjahr")
    if gj:
        return scored_year - int(gj), "geburtsjahr"
    a = company.get("gf_alter")
    if a is not None:
        return int(a), "import"
    return None, "unbekannt"


# hr-engine-Anreicherung, eine Zeile je Person (Nachname + Geburtsjahr + ist_gf).
# Personen-Ebene liegt NICHT in companies (nur die Aggregate gf_alter/anzahl_gf), und
# der Supabase-Client kann kein DDL — deshalb wird das vollzogene-Generationswechsel-
# Signal hier aus der CSV gerechnet, identisch zur Dashboard-Logik (kuratierung.py).
GF_PERSONEN_CSV = Path(
    "/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv")


def load_gf_personen(path: Path = GF_PERSONEN_CSV) -> dict:
    """key = norm(firma)|plz -> Liste {nachname, geburtsjahr, ist_gf}."""
    import csv
    out: dict = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            nachname = (r.get("gf_nachname") or "").strip()
            bj = (r.get("gf_geburtsdatum") or "").strip()[:4]
            key = f"{norm(r.get('firma'))}|{(r.get('plz') or '').strip()}"
            out.setdefault(key, []).append({
                "nachname": nachname,
                "geburtsjahr": int(bj) if bj.isdigit() else None,
                "ist_gf": (r.get("ist_gf") or "").strip() == "1",
            })
    return out


def generationswechsel_vollzogen(personen: list, ref_year: int) -> bool:
    """Hartes Negativsignal: >=2 GF teilen den Nachnamen und mindestens einer ist jünger
    als 50 -> praktisch immer ein bereits vollzogener Generationswechsel, kein Verkaufs-
    anlass. Strenger als das weiche Dossier-Signal nachfolge_intern_geregelt, weil rein
    aus den Register-Stammdaten ableitbar. K.o.-Kriterium (s. score_company)."""
    by_sn: dict = {}
    for p in personen or []:
        if not p.get("ist_gf") or not p.get("nachname"):
            continue
        gj = p.get("geburtsjahr")
        by_sn.setdefault(p["nachname"].lower(), []).append(
            ref_year - gj if gj else None)
    return any(len(ages) >= 2 and any(a is not None and a < 50 for a in ages)
               for ages in by_sn.values())


# --------------------------------------------------------------------------- #
# Scoring (rein deterministisch; Source-of-Truth-Felder s. Modul-Docstring)
# --------------------------------------------------------------------------- #
def score_company(company, dossier: Dossier, scfg, scored_year, gen_vollzogen=False) -> dict:
    a = scfg["anker"]
    nf = scfg["nachfolge"]
    wb = scfg["web_bedarf"]
    ab = scfg["abzuege_ko"]

    # --- Anker (Stammdaten aus companies) ---
    bb = a["bilanzsumme_band"]
    bilanz = company.get("bilanzsumme_eur")
    ek = company.get("ek_quote_pct")
    min_ek = bb.get("min_ek_pct")
    bilanz_ok = (bilanz is not None and ek is not None
                 and _num(bb["min_eur"]) <= bilanz <= _num(bb["max_eur"])
                 and (min_ek is None or ek >= _num(min_ek)))
    mb = a["mitarbeiter_band"]
    ma = company.get("mitarbeiterzahl")
    ma_ok = ma is not None and _num(mb["min"]) <= ma <= _num(mb["max"])
    cagr = company.get("gewinn_cagr_pct")
    cagr_ok = cagr is not None and cagr > 0
    w = wz2_of(company)
    fokus_ok = w in [str(x) for x in scfg["fokus_wz_liste"]]

    anker = {
        "bilanzsumme_band": _entry(bb["punkte"], bilanz_ok,
                                   f"{bilanz} EUR, EK {ek} %" if bilanz is not None else "—"),
        "mitarbeiter_band": _entry(mb["punkte"], ma_ok, ma),
        "gewinn_cagr_positiv": _entry(a["gewinn_cagr_positiv"]["punkte"], cagr_ok, cagr),
        "fokus_wz": _entry(a["fokus_wz"]["punkte"], fokus_ok, w),
    }

    # --- Nachfolge ---
    alter, alter_q = gf_alter_at(company, scored_year)
    schwelle = _num(nf["gf_alter_min"]["schwelle_jahre"])
    intern = bool(getattr(dossier, "nachfolge_intern_geregelt", False))  # Dossier = SoT
    neutralisiert = intern and bool(nf["gf_alter_min"].get("neutralisiert_bei_intern_geregelt"))
    alter_ueber = alter is not None and alter >= schwelle
    alter_ok = alter_ueber and not neutralisiert    # bereitstehende Nachfolge -> kein Nachfolgedruck
    fam_ok = bool(dossier.familienunternehmen.hinweis)          # Dossier = SoT
    anz = company.get("anzahl_gf")
    alter_wert = f"{alter} ({alter_q})" if alter is not None else "unbekannt"
    if alter_ueber and neutralisiert:
        alter_wert += " — neutralisiert: Nachfolge intern geregelt"
    nachfolge = {
        "gf_alter_min": _entry(nf["gf_alter_min"]["punkte"], alter_ok, alter_wert),
        "gf_name_in_firmenname": _entry(nf["gf_name_in_firmenname"]["punkte"],
                                        bool(company.get("gf_name_in_firmenname"))),
        "familienhinweis": _entry(nf["familienhinweis"]["punkte"], fam_ok,
                                  dossier.familienunternehmen.generation),
        "nur_ein_gf": _entry(nf["nur_ein_gf"]["punkte"], anz == 1, anz),
        "nachfolge_intern_geregelt": _entry(nf["nachfolge_intern_geregelt"]["punkte"], intern,
                                            getattr(dossier, "naechste_generation", None) or ("ja" if intern else None)),
    }

    # --- Web-Bedarf (Dossier = SoT) ---
    fs = dossier.fuehrungsstruktur
    keine_kaufm_ok = fs.kaufmaennische_funktion_besetzt is False     # nur explizit False
    kaufm_stellen = dossier.karriere.kaufm_stellen or []
    offene = dossier.karriere.offene_stellen or []
    offene_kaufm_ok = bool(kaufm_stellen) or any(
        any(term in (s or "").lower() for term in KAUFM_TERMS) for s in offene)
    zweite_unsichtbar_ok = fs.zweite_ebene_sichtbar is False
    web_bedarf = {
        "keine_kaufm_funktion": _entry(wb["keine_kaufm_funktion"]["punkte"], keine_kaufm_ok),
        "offene_kaufm_stelle": _entry(wb["offene_kaufm_stelle"]["punkte"], offene_kaufm_ok,
                                      "; ".join(kaufm_stellen)[:80] or None),
        "zweite_ebene_unsichtbar": _entry(wb["zweite_ebene_unsichtbar"]["punkte"], zweite_unsichtbar_ok),
    }

    # --- Abzuege / K.o. ---
    nfilt = dossier.negativ_filter
    holding = bool(company.get("holding_flag"))
    konzern = bool(nfilt.tochter_eines_konzerns)
    insol = bool(nfilt.insolvenz_hinweis)
    shop = bool(nfilt.reiner_onlineshop)
    abzuege = {
        "holding_flag": {"hit": holding, "ko": True, "punkte": 0},
        "konzerntochter": {"hit": konzern, "ko": True, "punkte": 0},
        "insolvenz": {"hit": insol, "ko": True, "punkte": 0},
        # Vollzogener Generationswechsel (>=2 GF gleichen Nachnamens, einer <50): kein
        # Verkaufsanlass, strukturell wie Holding/Konzerntochter. Datensatz bleibt (KO-Klasse),
        # im Dashboard per Toggle einblendbar.
        "generationswechsel_vollzogen": {"hit": gen_vollzogen, "ko": True, "punkte": 0},
        "reiner_onlineshop": _entry(ab["reiner_onlineshop"]["punkte"], shop),
    }
    ko = holding or konzern or insol or gen_vollzogen

    breakdown = {"anker": anker, "nachfolge": nachfolge,
                 "web_bedarf": web_bedarf, "abzuege": abzuege}
    total = int(sum(e.get("punkte", 0) for grp in breakdown.values() for e in grp.values()))

    kl = scfg["klassen"]
    if ko:
        klasse = "KO"
    elif total >= _num(kl["A"]["min_punkte"]):
        klasse = "A"
    elif total >= _num(kl["B"]["min_punkte"]):
        klasse = "B"
    else:
        klasse = "C"

    return {"total": total, "klasse": klasse, "ko": ko, "breakdown": breakdown,
            "gf_alter": alter, "gf_alter_quelle": alter_q}


# --------------------------------------------------------------------------- #
# Clusterung
# --------------------------------------------------------------------------- #
def groessenband(bilanz, ma, cfg):
    order = ["klein", "kern", "oberes_band"]

    def band_for(value, key):
        if value is None:
            return None
        for b in order:
            if value <= _num(cfg[b][key]):
                return b
        return order[-1]   # ueber alle Schwellen -> oberes_band (Obergrenze offen)

    cands = [x for x in (band_for(bilanz, "bilanz_max_eur"), band_for(ma, "ma_max")) if x]
    if not cands:
        return None
    return max(cands, key=order.index)   # groesseres Band gewinnt (sicherere Einordnung)


def cluster_for(company, ccfg):
    w = wz2_of(company)
    branche = "rest"
    for name, codes in ccfg["makrocluster"].items():
        if w in [str(x) for x in codes]:
            branche = name
            break
    band = groessenband(company.get("bilanzsumme_eur"), company.get("mitarbeiterzahl"),
                         ccfg["groessenband"])
    key = f"{branche}__{band or 'unbekannt'}"
    return branche, band, key


# --------------------------------------------------------------------------- #
# Begruendung (= Anruf-Briefing; nutzt signals als Beleg-Schicht)
# --------------------------------------------------------------------------- #
def build_begruendung(company, dossier: Dossier, sigs, res, cluster, ccfg) -> str:
    branche, band, key = cluster
    bd = res["breakdown"]
    L = []
    L.append(f"{company.get('name')} — Score {res['total']} (Klasse {res['klasse']})"
             + (f"  [K.o.: {_ko_reason(bd)}]" if res["ko"] else ""))
    ort = company.get("ort") or ""
    L.append(f"Standort {ort or '—'} · WZ {wz2_of(company)} · Cluster {key}")
    L.append(f"Schmerzpunkt (Brief): {ccfg['schmerzpunkt'].get(branche, ccfg['schmerzpunkt']['rest'])}")
    L.append("")

    bilanz = company.get("bilanzsumme_eur")
    L.append("Kennzahlen: "
             f"Bilanzsumme {f'{bilanz:,.0f} EUR'.replace(',', '.') if bilanz is not None else '—'}, "
             f"EK-Quote {company.get('ek_quote_pct') if company.get('ek_quote_pct') is not None else '—'} %, "
             f"MA {company.get('mitarbeiterzahl') if company.get('mitarbeiterzahl') is not None else '—'}, "
             f"Gewinn-CAGR {company.get('gewinn_cagr_pct') if company.get('gewinn_cagr_pct') is not None else '—'} %")

    alter = res["gf_alter"]
    L.append("Nachfolge: "
             f"GF-Alter {alter if alter is not None else 'unbekannt'}, "
             f"GF-Name im Firmennamen: {'ja' if company.get('gf_name_in_firmenname') else 'nein'}, "
             f"Familienunternehmen: {'ja' if dossier.familienunternehmen.hinweis else 'nein'}, "
             f"Anzahl GF: {company.get('anzahl_gf')}")
    if dossier.nachfolge_signale:
        L.append("  Signale: " + "; ".join(dossier.nachfolge_signale))
    if getattr(dossier, "nachfolge_intern_geregelt", False):
        L.append("  ACHTUNG Nachfolge intern geregelt: "
                 + (getattr(dossier, "naechste_generation", None) or "nächste Generation steht bereit")
                 + " (GF-Alter-Bonus neutralisiert + 3 Punkte Abzug, vermutlich kein Verkaufsanlass)")

    fs = dossier.fuehrungsstruktur
    L.append("Web-Bedarf: "
             f"kaufm. Funktion besetzt: {_tri(fs.kaufmaennische_funktion_besetzt)}, "
             f"2. Ebene sichtbar: {_tri(fs.zweite_ebene_sichtbar)}, "
             f"offene kaufm. Stellen: {'; '.join(dossier.karriere.kaufm_stellen) or '—'}")

    # Belege aus der signals-Tabelle (ein Zitat je Signal-Typ).
    if sigs:
        L.append("")
        L.append("Belege:")
        seen = set()
        for s in sigs:
            st = s.get("signal_type") or "sonstiges"
            if st in seen:
                continue
            seen.add(st)
            L.append(f"  [{st}] \"{(s.get('beleg_zitat') or '').strip()}\" — {s.get('beleg_url') or ''}")

    if dossier.ansprache_hooks:
        L.append("")
        L.append("Hooks: " + " | ".join(dossier.ansprache_hooks))
    return "\n".join(L)


def _tri(v):
    return "ja" if v is True else "nein" if v is False else "unbekannt"


def _ko_reason(bd) -> str:
    return ", ".join(k for k, e in bd["abzuege"].items() if e.get("ko") and e.get("hit")) or "—"


# --------------------------------------------------------------------------- #
# Laden (resume: Firmen mit Score ueberspringen, ausser --force/--ids)
# --------------------------------------------------------------------------- #
def _paginate_ids(client, table, col):
    ids, step, start = set(), 1000, 0
    while True:
        r = client.table(table).select(col).range(start, start + step - 1).execute()
        ids.update(x[col] for x in r.data)
        if len(r.data) < step:
            break
        start += step
    return ids


def load_dossiers(client, only_ids=None) -> dict:
    out = {}
    if only_ids:
        step = 200
        for i in range(0, len(only_ids), step):
            r = (client.table("dossiers").select("company_id,dossier")
                 .in_("company_id", only_ids[i:i + step]).execute())
            for d in r.data:
                out[d["company_id"]] = d["dossier"]
        return out
    step, start = 1000, 0
    while True:
        r = client.table("dossiers").select("company_id,dossier").range(start, start + step - 1).execute()
        for d in r.data:
            out[d["company_id"]] = d["dossier"]
        if len(r.data) < step:
            break
        start += step
    return out


COLS = ("id,name,ort,plz,wz2,branche_wz,bilanzsumme_eur,ek_quote_pct,gewinn_cagr_pct,"
        "mitarbeiterzahl,anzahl_gf,gf_name_in_firmenname,gf_geburtsjahr,gf_alter,"
        "holding_flag,prioritaets_score")


def load_companies(client, ids) -> dict:
    out, step = {}, 200
    for i in range(0, len(ids), step):
        r = client.table("companies").select(COLS).in_("id", ids[i:i + step]).execute()
        for c in r.data:
            out[c["id"]] = c
    return out


def load_signals(client, ids) -> dict:
    out, step = defaultdict(list), 200
    for i in range(0, len(ids), step):
        r = (client.table("signals").select("company_id,signal_type,value,beleg_zitat,beleg_url")
             .in_("company_id", ids[i:i + step]).execute())
        for s in r.data:
            out[s["company_id"]].append(s)
    return out


# --------------------------------------------------------------------------- #
# Persistenz
# --------------------------------------------------------------------------- #
def persist(client, company_id, res, cluster, begruendung, version, now_iso):
    branche, band, key = cluster
    row = {
        "company_id": company_id,
        "score_total": res["total"],
        "score_klasse": res["klasse"],
        "breakdown": res["breakdown"],
        "begruendung": begruendung,
        "scoring_version": version,
        "cluster_branche": branche,
        "groessenband": band,
        "cluster_key": key,
        "scored_at": now_iso,
    }
    client.table("scores").upsert(row, on_conflict="company_id").execute()


# --------------------------------------------------------------------------- #
# Report (Abnahme: Verteilung plausibel, je Cluster 5 gegengelesen, Determinismus)
# --------------------------------------------------------------------------- #
def write_report(audit, args, version, determ) -> Path:
    out = Path(OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "phase4-score.jsonl", "w", encoding="utf-8") as f:
        for a in audit:
            f.write(json.dumps(a, ensure_ascii=False, default=str) + "\n")

    ok = [a for a in audit if a.get("ok")]
    n, nk = len(audit), len(ok)
    pct = lambda k, d: f"{(100 * k / d):.1f} %" if d else "—"

    kl = Counter(a["klasse"] for a in ok)
    hist = Counter(a["total"] for a in ok)
    clusters = Counter(a["cluster_key"] for a in ok)
    alter_known = sum(1 for a in ok if a.get("gf_alter") is not None)

    # Kriterien-Trefferquote (Plausibilitaet)
    crit_hits = Counter()
    for a in ok:
        for grp in a["breakdown"].values():
            for name, e in grp.items():
                if e.get("hit"):
                    crit_hits[name] += 1

    L = []
    L.append(f"# Phase 4 — Score- & Cluster-Report ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    L.append("")
    sel = ("gezielter Re-Run (IDs)" if (args.ids or args.ids_file)
           else "alle Firmen mit Dossier" + ("" if args.force else " ohne Score"))
    L.append(f"Selektion: {sel}; Limit {args.limit or '—'}; scoring_version `{version}`.")
    L.append("")
    L.append("## Durchsatz")
    L.append("")
    L.append(f"- Gescort: **{nk}** / {n} verarbeitet" + (f" ({n - nk} Fehler)" if n - nk else ""))
    L.append(f"- Determinismus (Doppelberechnung identisch): **{determ[0]}/{determ[1]}**")
    L.append("")
    L.append("## Klassen-Verteilung")
    L.append("")
    L.append("| Klasse | Firmen | Anteil |")
    L.append("|---|---|---|")
    for k in ("A", "B", "C", "KO"):
        L.append(f"| {k} | {kl.get(k, 0)} | {pct(kl.get(k, 0), nk)} |")
    L.append("")
    L.append("## Score-Histogramm")
    L.append("")
    L.append("| Score | Firmen |")
    L.append("|---|---|")
    for s in sorted(hist, reverse=True):
        L.append(f"| {s} | {hist[s]} |")
    L.append("")
    L.append("## Kriterien-Trefferquote")
    L.append("")
    L.append("| Kriterium | Treffer | Anteil |")
    L.append("|---|---|---|")
    order = ["bilanzsumme_band", "mitarbeiter_band", "gewinn_cagr_positiv", "fokus_wz",
             "gf_alter_min", "gf_name_in_firmenname", "familienhinweis", "nur_ein_gf",
             "nachfolge_intern_geregelt",
             "keine_kaufm_funktion", "offene_kaufm_stelle", "zweite_ebene_unsichtbar",
             "holding_flag", "konzerntochter", "insolvenz",
             "generationswechsel_vollzogen", "reiner_onlineshop"]
    for c in order:
        L.append(f"| {c} | {crit_hits.get(c, 0)} | {pct(crit_hits.get(c, 0), nk)} |")
    L.append("")
    L.append(f"> Daten-Coverage: GF-Alter bekannt für **{alter_known}/{nk}** "
             f"({pct(alter_known, nk)}). Alter unbekannt zählt nicht als erfüllt "
             "(Caveat: unbekannt =/= jung) — Nachfolge-Score ist hier durch fehlende "
             "GF-Geburtsdaten nach unten verzerrt.")
    L.append("")

    L.append("## Cluster-Verteilung")
    L.append("")
    L.append("| cluster_key | Firmen |")
    L.append("|---|---|")
    for c, cnt in clusters.most_common():
        L.append(f"| {c} | {cnt} |")
    L.append("")

    # Stichprobe: je Cluster bis zu 5, nach Score absteigend.
    by_cluster = defaultdict(list)
    for a in ok:
        by_cluster[a["cluster_key"]].append(a)
    L.append("## Stichprobe je Cluster (bis 5, Score desc)")
    L.append("")
    L.append("| Cluster | Firma | Score | Klasse | GF-Alter | Nachfolge / Web kurz |")
    L.append("|---|---|---|---|---|---|")
    for ckey in sorted(by_cluster):
        rows = sorted(by_cluster[ckey], key=lambda a: -a["total"])[:5]
        for a in rows:
            hit_names = [name for grp in ("nachfolge", "web_bedarf")
                         for name, e in a["breakdown"][grp].items() if e.get("hit")]
            L.append(f"| {ckey} | {(a.get('name') or '')[:26]} | {a['total']} | {a['klasse']} | "
                     f"{a.get('gf_alter') if a.get('gf_alter') is not None else '—'} | "
                     f"{', '.join(hit_names) or '—'} |")
    L.append("")

    # Detailbeispiele: hoechstes A, ein B, ein KO (volle begruendung).
    L.append("## Detailbeispiele (volle Begründung = Anruf-Briefing)")
    L.append("")
    picks = []
    for kls in ("A", "B", "C", "KO"):
        cand = sorted([a for a in ok if a["klasse"] == kls], key=lambda a: -a["total"])
        if cand:
            picks.append(cand[0])
    for a in picks[:4]:
        L.append(f"### {a.get('name')} — Klasse {a['klasse']}, Score {a['total']}")
        L.append("")
        L.append("```")
        L.append(a.get("begruendung") or "")
        L.append("```")
        L.append("")

    path = out / "phase4-score-report.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Lauf
# --------------------------------------------------------------------------- #
def resolve_ids(args):
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


def run(args):
    log = JsonLogger("pipeline.log")
    client = get_client("calvoran")
    scfg = config.load("scoring")
    ccfg = config.load("clusters")
    version = scfg.get("version", "unversioned")
    scored_year = datetime.now(timezone.utc).year

    only_ids = resolve_ids(args)
    dossiers = load_dossiers(client, only_ids)
    cand_ids = list(dossiers.keys())
    if not only_ids and not args.force:
        done = _paginate_ids(client, "scores", "company_id")
        cand_ids = [i for i in cand_ids if i not in done]
    if args.limit:
        cand_ids = cand_ids[:args.limit]
    log.log("c4_selected", n=len(cand_ids), force=args.force,
            mode="ids" if only_ids else "scan")
    print(f"Selektiert: {len(cand_ids)} Firmen mit Dossier.")
    if not cand_ids:
        print("Nichts zu scoren (alles erledigt oder leere Auswahl).")
        return

    companies = load_companies(client, cand_ids)
    sigs_by = load_signals(client, cand_ids)
    gf_personen = load_gf_personen()  # Personen-Ebene für vollzogenen Generationswechsel

    audit, determ_ok, determ_tot = [], 0, 0
    for idx, cid in enumerate(cand_ids):
        company = companies.get(cid)
        rec = {"id": cid, "ok": False}
        if not company:
            rec["error"] = "company_fehlt"
            audit.append(rec)
            continue
        rec["name"] = company.get("name")
        gen_key = f"{norm(company.get('name'))}|{(company.get('plz') or '').strip()}"
        gen_vollzogen = generationswechsel_vollzogen(gf_personen.get(gen_key, []), scored_year)
        try:
            dossier = Dossier.model_validate(dossiers[cid])
            res = score_company(company, dossier, scfg, scored_year, gen_vollzogen)
            cluster = cluster_for(company, ccfg)
            begruendung = build_begruendung(company, dossier, sigs_by.get(cid, []), res, cluster, ccfg)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}:{str(e)[:140]}"
            log.log("c4_error", id=cid, error=str(e)[:200])
            audit.append(rec)
            continue

        # Determinismus-Selbstcheck: zweite Berechnung muss identisch sein.
        res2 = score_company(company, dossier, scfg, scored_year, gen_vollzogen)
        determ_tot += 1
        if (res2["total"], res2["klasse"], res2["breakdown"]) == (res["total"], res["klasse"], res["breakdown"]):
            determ_ok += 1

        persist(client, cid, res, cluster, begruendung, version, _now_iso())
        rec.update({
            "ok": True, "total": res["total"], "klasse": res["klasse"], "ko": res["ko"],
            "gf_alter": res["gf_alter"], "gf_alter_quelle": res["gf_alter_quelle"],
            "cluster_key": cluster[2], "cluster_branche": cluster[0], "groessenband": cluster[1],
            "breakdown": res["breakdown"], "begruendung": begruendung,
            "prioritaets_score": company.get("prioritaets_score"),
        })
        audit.append(rec)
        if (idx + 1) % 5000 == 0:
            # Supabase-Gateway kappt HTTP/2 nach ~10.000 Requests je Verbindung
            # (ConnectionTerminated). Vor der Grenze frisch verbinden; get_client()
            # liefert jeweils einen neuen Client (kein Caching).
            client = get_client("calvoran")
            log.log("client_reconnect", done=idx + 1)
        if (idx + 1) % 50 == 0:
            log.log("c4_progress", done=idx + 1, total=len(cand_ids))
            print(f"  {idx + 1}/{len(cand_ids)} gescort")

    if args.report:
        path = write_report(audit, args, version, (determ_ok, determ_tot))
        print(f"Report: {path}")
    log.log("c4_done", n=len(audit), ok=sum(1 for a in audit if a.get("ok")),
            determ=f"{determ_ok}/{determ_tot}")
    print(f"Fertig: {sum(1 for a in audit if a.get('ok'))}/{len(audit)} gescort. "
          f"Determinismus {determ_ok}/{determ_tot}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Max. Firmenzahl (0 = alle)")
    ap.add_argument("--ids", default=None, help="Kommagetrennte company-UUIDs (gezielt)")
    ap.add_argument("--ids-file", dest="ids_file", default=None,
                    help="Datei mit company-UUIDs je Zeile ODER JSONL mit 'id'-Feld")
    ap.add_argument("--force", action="store_true",
                    help="Auch Firmen mit bestehendem Score neu scoren")
    ap.add_argument("--report", action="store_true",
                    help="Markdown+JSONL-Report nach OUTPUT_DIR schreiben")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
