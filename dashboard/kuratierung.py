"""Lead-Kuratierungs-Dashboard: Auswahl der Firmen für die Ansprache (vor Phase 5).

Liest scores ⨝ companies ⨝ dossiers aus Supabase, filtert über die kuratierungs-
relevanten Dimensionen (Region/PLZ, BWL-Affinität, Berater-Ausschluss, Score-Klasse,
GF-Alter/Nachfolge-Reife, Makrocluster, Volltext) und lässt Jo je Firma markieren.
Die Auswahl wird je Welle nach OUTPUT_DIR/selection.jsonl persistiert; c5_export liest
später `selected == true` für die jeweilige Welle statt blind alle A/B.

Start:
    cd ~/projects/calvoran-outreach
    .venv/bin/streamlit run dashboard/kuratierung.py
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# Projektwurzel auf den Pfad, damit `import calvoran` aus dashboard/ funktioniert.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from calvoran import config  # noqa: E402
from calvoran.db import get_client  # noqa: E402

OUTPUT_DIR = "/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/outreach"
SELECTION_FILE = Path(OUTPUT_DIR) / "selection.jsonl"
# Einzelpersonen (GF + Prokuristen) aus der hr-engine-Anreicherung. Join über
# norm(firma)+plz — derselbe Schlüssel, mit dem c1b_import_gf_alter die DB befüllt.
HR_PERSONEN_CSV = Path("/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv")
# Roh-PDFs der hr-engine (Aktueller Ausdruck je job_key); Dateiname: "<job_key>+AD-<ts>.pdf".
HR_RAW_DIR = Path.home() / ".local/state/hr-engine/raw"


def _norm(s: str) -> str:
    """Identisch zu pipeline/_common.norm — bewusst repliziert (1 Zeile, stabil)."""
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _generationswechsel(personen: list) -> bool:
    """Hartes 'Nachfolge geregelt'-Signal: zwei oder mehr Geschäftsführer teilen den
    Nachnamen und mindestens einer davon ist jünger als 50 — praktisch immer ein bereits
    vollzogener Generationswechsel (kein Verkaufsanlass). Strenger als das weiche
    Dossier-Signal, weil rein aus den Register-Stammdaten ableitbar."""
    by_sn: dict = {}
    for p in personen or []:
        if not p.get("ist_gf") or not p.get("name"):
            continue
        by_sn.setdefault(p["name"].split()[-1].lower(), []).append(p.get("alter"))
    return any(len(ages) >= 2 and any(a is not None and a < 50 for a in ages)
               for ages in by_sn.values())


def _teur(v):
    """EUR -> Tausend-EUR, ganzzahlig; None/leer bleibt None."""
    try:
        return int(round(float(v) / 1000)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _tri(v):
    return "ja" if v is True else "nein" if v is False else "unbekannt"


def _kv_table(pairs):
    """Kompakte headerlose Key-Value-Tabelle (HTML)."""
    body = "".join(
        f"<tr><td style='padding:1px 10px 1px 0;color:#888;white-space:nowrap'>{k}</td>"
        f"<td style='padding:1px 0;text-align:right'>{v}</td></tr>"
        for k, v in pairs)
    return f"<table style='width:100%;font-size:0.9rem;border-collapse:collapse'>{body}</table>"

# Köln-Bonn-Default-Region (PLZ-2-Steller); weitere Bereiche (Ruhrgebiet etc.)
# tauchen automatisch in der Multiselect auf, sobald der Datenbestand wächst.
REGION_LABELS = {
    "50": "Köln/Rhein-Erft", "51": "Köln/Leverkusen", "53": "Bonn/Rhein-Sieg",
    "52": "Aachen/Düren", "40": "Düsseldorf", "41": "Mönchengladbach",
    "42": "Wuppertal", "44": "Dortmund", "45": "Essen/Ruhr", "46": "Oberhausen/Niederrhein",
    "47": "Duisburg/Krefeld", "48": "Münster", "33": "Bielefeld", "57": "Siegen",
    "58": "Hagen", "59": "Hamm/Soest",
}


# --------------------------------------------------------------------------- #
# Datenlade-Layer (gecached)
# --------------------------------------------------------------------------- #
def _fetch_all(client, tbl, cols):
    out, step, start = [], 1000, 0
    while True:
        r = client.table(tbl).select(cols).range(start, start + step - 1).execute()
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step
    return out


@st.cache_data(ttl=600)
def load_ad_pdf_map() -> dict:
    """key norm(firma)|plz -> Pfad zum jüngsten AD-PDF der hr-engine (oder fehlend).

    Das raw-Verzeichnis wird einmal gescannt (job_key -> jüngstes PDF), dann über den
    job_key der CSV auf den Firmen-Key gejoint. Timestamp steckt im Dateinamen
    (YYYYmmddHHMMSS), also ist lexikografisch == chronologisch.
    """
    out: dict = {}
    if not HR_PERSONEN_CSV.exists() or not HR_RAW_DIR.exists():
        return out
    pdf_by_job: dict = {}
    for p in HR_RAW_DIR.glob("*+AD-*.pdf"):
        jk = p.name.split("+AD-")[0]
        prev = pdf_by_job.get(jk)
        if prev is None or p.name > prev.name:
            pdf_by_job[jk] = p
    with open(HR_PERSONEN_CSV, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            jk = (r.get("job_key") or "").strip()
            key = f"{_norm(r.get('firma'))}|{(r.get('plz') or '').strip()}"
            if jk in pdf_by_job and key not in out:
                out[key] = str(pdf_by_job[jk])
    return out


@st.cache_data(ttl=600)
def load_personen() -> dict:
    """key norm(firma)|plz -> Liste Personen (GF + Prokuristen) aus der hr-engine-CSV.

    Alter wird aus dem Geburtsjahr gegen das aktuelle Jahr gerechnet (nicht aus der
    CSV-Spalte, die das Alter zum Abrufzeitpunkt einfror).
    """
    out: dict = {}
    if not HR_PERSONEN_CSV.exists():
        return out
    now_year = datetime.now(timezone.utc).year
    with open(HR_PERSONEN_CSV, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            bj = (r.get("gf_geburtsdatum") or "").strip()[:4]
            alter = now_year - int(bj) if bj.isdigit() else None
            name = " ".join(p for p in ((r.get("gf_vorname") or "").strip(),
                                        (r.get("gf_nachname") or "").strip()) if p)
            key = f"{_norm(r.get('firma'))}|{(r.get('plz') or '').strip()}"
            out.setdefault(key, []).append({
                "name": name, "alter": alter,
                "rolle": (r.get("rolle") or "").strip(),
                "ist_gf": (r.get("ist_gf") or "").strip() == "1",
            })
    return out


def _affinitaet_lookup(cfg: dict) -> dict:
    out = {}
    for stufe, codes in (cfg.get("affinitaet") or {}).items():
        for code in codes:
            out.setdefault(code, stufe)
    return out


@st.cache_data(ttl=600, show_spinner="Lade Leads aus Supabase …")
def load_frame() -> pd.DataFrame:
    cfg = config.load("bwl_affinitaet")
    aff = _affinitaet_lookup(cfg)
    blacklist = set(cfg.get("berater_blacklist") or [])
    schmerz = config.load("clusters").get("schmerzpunkt") or {}

    personen = load_personen()
    ad_pdf_map = load_ad_pdf_map()
    client = get_client("calvoran")
    scores = {s["company_id"]: s for s in _fetch_all(
        client, "scores", "company_id,score_klasse,score_total,cluster_branche,cluster_key,begruendung")}
    comp = {c["id"]: c for c in _fetch_all(
        client, "companies",
        "id,name,plz,ort,branche_wz,gf_alter,umsatz_eur,bilanzsumme_eur,mitarbeiterzahl,website,"
        "ek_quote_pct,gewinn_cagr_pct,anzahl_gf,gf_name_in_firmenname,holding_flag,excluded,dup_of,"
        "register_id,hr_amtsgericht")}
    dossiers = {d["company_id"]: (d.get("dossier") or {}) for d in _fetch_all(
        client, "dossiers", "company_id,dossier")}
    # Belege je Firma (ein Zitat je Signal-Typ, wie c4)
    sigs_by: dict = {}
    for s in _fetch_all(client, "signals", "company_id,signal_type,beleg_zitat,beleg_url"):
        sigs_by.setdefault(s["company_id"], []).append(s)

    rows = []
    for cid, s in scores.items():
        c = comp.get(cid)
        if not c or c.get("holding_flag") or c.get("excluded") or c.get("dup_of"):
            continue
        wz = (c.get("branche_wz") or "").strip()
        wz2 = wz[:2] if len(wz) >= 2 and wz[:2].isdigit() else ""
        plz = (c.get("plz") or "").strip()
        plz2 = plz[:2] if len(plz) >= 2 and plz[:2].isdigit() else ""
        d = dossiers.get(cid) or {}
        nachfolge = d.get("nachfolge_signale") or []
        fam = (d.get("familienunternehmen") or {}).get("hinweis")
        geregelt = bool(d.get("nachfolge_intern_geregelt"))
        branche = s.get("cluster_branche") or "rest"
        fstruct = d.get("fuehrungsstruktur") or {}
        karriere = d.get("karriere") or {}
        bel, seen_t = [], set()
        for sg in sigs_by.get(cid, []):
            stp = sg.get("signal_type") or "sonstiges"
            if stp in seen_t:
                continue
            seen_t.add(stp)
            bel.append({"type": stp, "zitat": (sg.get("beleg_zitat") or "").strip(),
                        "url": sg.get("beleg_url") or ""})
        rows.append({
            "company_id": cid,
            "name": c.get("name") or "",
            "register_id": c.get("register_id") or "",
            "hr_amtsgericht": c.get("hr_amtsgericht") or "",
            "plz": plz, "plz2": plz2,
            "region": REGION_LABELS.get(plz2, plz2 or "?"),
            "ort": c.get("ort") or "",
            "branche_wz": wz,
            "bwl_affinitaet": aff.get(wz2, "mittel"),
            "berater": wz2 in blacklist,
            "cluster": branche,
            "klasse": s.get("score_klasse"),
            "score": s.get("score_total"),
            "gf_alter": c.get("gf_alter"),
            "familie": bool(fam),
            "nachfolge_signale": "; ".join(nachfolge),
            "nachfolge_geregelt": geregelt,
            "naechste_generation": d.get("naechste_generation") or "",
            "begruendung": s.get("begruendung") or "",
            "umsatz_teur": _teur(c.get("umsatz_eur")),
            "bilanz_teur": _teur(c.get("bilanzsumme_eur")),
            "mitarbeiter": c.get("mitarbeiterzahl"),
            "website": c.get("website") or "",
            "ek_quote": c.get("ek_quote_pct"),
            "cagr": c.get("gewinn_cagr_pct"),
            "anzahl_gf": c.get("anzahl_gf"),
            "gf_name_in_name": bool(c.get("gf_name_in_firmenname")),
            "personen": personen.get(f"{_norm(c.get('name'))}|{plz}", []),
            "generationswechsel": _generationswechsel(personen.get(f"{_norm(c.get('name'))}|{plz}", [])),
            "ad_pdf": ad_pdf_map.get(f"{_norm(c.get('name'))}|{plz}"),
            "schmerzpunkt": schmerz.get(branche, schmerz.get("rest", "")),
            "geschaeftsmodell": d.get("geschaeftsmodell") or "",
            "kaufm_besetzt": fstruct.get("kaufmaennische_funktion_besetzt"),
            "zweite_ebene": fstruct.get("zweite_ebene_sichtbar"),
            "kaufm_stellen": karriere.get("kaufm_stellen") or [],
            "hooks": d.get("ansprache_hooks") or [],
            "belege": bel,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Review-Persistenz (JSONL je Welle): Entscheidung + Notizen
# --------------------------------------------------------------------------- #
# Schema je Zeile: {company_id, name, wave, decision, selected, note_self, note_ki,
#                   decided_at}.  decision ∈ {"lead","uninteressant",null}.
# selected == (decision == "lead") -> c5_export liest weiterhin selected==true.
DECISIONS = ["—", "Lead", "uninteressant"]
_DEC_TO_STORE = {"—": None, "Lead": "lead", "uninteressant": "uninteressant"}
_STORE_TO_DEC = {None: "—", "lead": "Lead", "uninteressant": "uninteressant"}


def _read_lines() -> list:
    if not SELECTION_FILE.exists():
        return []
    out = []
    for line in SELECTION_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_reviews(wave: int) -> dict:
    """company_id -> {decision, note_self, note_ki} für eine Welle."""
    rev = {}
    for r in _read_lines():
        if r.get("wave") != wave:
            continue
        decision = r.get("decision")
        if decision is None and r.get("selected"):       # Alt-Records ohne decision
            decision = "lead"
        rev[r.get("company_id")] = {
            "decision": decision,
            "note_self": r.get("note_self") or "",
            "note_ki": r.get("note_ki") or "",
        }
    return rev


def save_reviews(wave: int, reviews: dict, frame: pd.DataFrame) -> int:
    """Überschreibt die Einträge dieser Welle, lässt andere Wellen unberührt.
    Persistiert nur Firmen mit Entscheidung oder Notiz. Rückgabe: Anzahl Leads."""
    other = [r for r in _read_lines() if r.get("wave") != wave]
    ts = datetime.now(timezone.utc).isoformat()
    by_id = frame.set_index("company_id")
    new = []
    for cid, rv in reviews.items():
        decision = rv.get("decision")
        note_self = (rv.get("note_self") or "").strip()
        note_ki = (rv.get("note_ki") or "").strip()
        if not decision and not note_self and not note_ki:
            continue                                      # nichts zu speichern
        name = by_id.loc[cid, "name"] if cid in by_id.index else ""
        new.append({"company_id": cid, "name": name, "wave": wave,
                    "decision": decision, "selected": decision == "lead",
                    "note_self": note_self, "note_ki": note_ki, "decided_at": ts})
    SELECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SELECTION_FILE, "w", encoding="utf-8") as f:
        for r in other + new:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return sum(1 for r in new if r["selected"])


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Calvoran — Lead-Kuratierung", layout="wide")
st.markdown("<style>.block-container{padding-top:1.8rem;}</style>", unsafe_allow_html=True)
st.title("Calvoran — Lead-Kuratierung für die Ansprache")

df = load_frame()
if df.empty:
    st.warning("Keine gescorten Leads gefunden.")
    st.stop()

sb = st.sidebar
sb.header("Welle")
wave = sb.number_input("Welle-Nummer", min_value=1, max_value=99, value=1, step=1)

# Review-State je Welle in der Session halten (überlebt Reruns)
rev_key = f"reviews_w{wave}"
if rev_key not in st.session_state:
    st.session_state[rev_key] = load_reviews(int(wave))
reviews: dict = st.session_state[rev_key]

# --- Filterleiste über der Tabelle ---
plz_opts = sorted(df["plz2"].dropna().unique().tolist())
cluster_opts = sorted(df["cluster"].dropna().unique().tolist())
plz_fmt = lambda p: f"{p} · {REGION_LABELS.get(p, '')}".strip(" ·")
umax = int(df["umsatz_teur"].dropna().max()) if df["umsatz_teur"].notna().any() else 0
bmax = int(df["bilanz_teur"].dropna().max()) if df["bilanz_teur"].notna().any() else 0

with st.expander("Filter / Suchkriterien", expanded=True):
    r1 = st.columns(4)
    klassen = r1[0].multiselect("Score-Klasse", ["A", "B", "C"], default=["A", "B"])
    bwl_sel = r1[1].multiselect("BWL-Affinität", ["fern", "mittel", "nah"], default=["fern", "mittel"],
                                help="fern = Idealkunde (technischer Inhaber, kaufm. Lücke). nah = depriorisieren.")
    cluster_sel = r1[2].multiselect("Makrocluster", cluster_opts, default=cluster_opts)
    plz_sel = r1[3].multiselect("Region (PLZ)", plz_opts, default=plz_opts, format_func=plz_fmt)

    r2 = st.columns(4)
    alter_min = r2[0].slider("GF-Alter ab", 40, 80, 58, 1)
    umsatz_rng = r2[1].slider("Umsatz T€", 0, umax, (0, umax), step=max(1, umax // 100)) if umax else (0, 0)
    bilanz_rng = r2[2].slider("Bilanz T€", 0, bmax, (0, bmax), step=max(1, bmax // 100)) if bmax else (0, 0)
    with r2[3]:
        ohne_berater = st.checkbox("Berater-Branchen ausschließen", value=True,
                                   help="WZ 69/70/73/74/78 — Berater lassen ungern andere Berater ins Haus.")
        nf_geregelt_zeigen = st.checkbox("auch geregelte Nachfolge anzeigen", value=False,
                                         help="Standard: vollzogener/geregelter Generationswechsel ist K.o. "
                                              "(kein Verkaufsanlass) und ausgeblendet. Anhaken blendet diese Firmen "
                                              "— jetzt Klasse KO — wieder ein. Hart: zwei GF gleichen Nachnamens, "
                                              "einer <50. Weich: nächste Generation steht laut Website bereit.")
        alter_unbekannt = st.checkbox("GF-Alter unbekannt einschließen", value=False)
        groesse_unbekannt = st.checkbox("ohne Umsatz/Bilanz einschließen", value=True)

    suche = st.text_input("Volltextsuche (Name/Ort)", "")

# --- Filter anwenden ---
f = df.copy()
f = f[f["plz2"].isin(plz_sel)]
f = f[f["bwl_affinitaet"].isin(bwl_sel)]
f = f[f["cluster"].isin(cluster_sel)]
if ohne_berater:
    f = f[~f["berater"]]
# Score-Klasse + geregelte Nachfolge: geregelte sind seit c4 K.o. (Klasse KO). Default
# blendet sie aus; der Toggle blendet sie zusätzlich zur Klassen-Auswahl wieder ein.
geregelt = f["nachfolge_geregelt"] | f["generationswechsel"]
if nf_geregelt_zeigen:
    f = f[f["klasse"].isin(klassen) | geregelt]
else:
    f = f[f["klasse"].isin(klassen) & ~geregelt]
alter_ok = f["gf_alter"].fillna(-1) >= alter_min
if alter_unbekannt:
    alter_ok = alter_ok | f["gf_alter"].isna()
f = f[alter_ok]


def _size_mask(col, lo, hi, mx):
    if not mx or (lo <= 0 and hi >= mx):        # Slider unbewegt -> kein Filter
        return pd.Series(True, index=f.index)
    m = f[col].between(lo, hi)
    return (m | f[col].isna()) if groesse_unbekannt else m


f = f[_size_mask("umsatz_teur", umsatz_rng[0], umsatz_rng[1], umax)]
f = f[_size_mask("bilanz_teur", bilanz_rng[0], bilanz_rng[1], bmax)]
if suche.strip():
    q = suche.strip().lower()
    f = f[f["name"].str.lower().str.contains(q) | f["ort"].str.lower().str.contains(q)]

f = f.sort_values(["klasse", "score", "gf_alter"], ascending=[True, False, False])

# --- Kennzahlen ---
leads = [cid for cid, r in reviews.items() if r.get("decision") == "lead"]
unint = [cid for cid, r in reviews.items() if r.get("decision") == "uninteressant"]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Treffer im Filter", len(f))
m2.metric("Leads im Filter", int(f["company_id"].isin(leads).sum()))
m3.metric("Leads gesamt (Welle)", len(leads))
m4.metric("uninteressant (Welle)", len(unint))


def _fmt_teur(v):
    return f"{int(v):,}".replace(",", ".") if pd.notna(v) else "—"


tab_tbl, tab_card = st.tabs(["Tabelle", "Karteikarte"])

# ============================= TABELLE ============================= #
with tab_tbl:
    view = f.copy()
    view.insert(0, "Lead", view["company_id"].isin(leads))
    cols = ["Lead", "name", "region", "ort", "branche_wz", "bwl_affinitaet", "cluster",
            "klasse", "score", "gf_alter", "familie", "nachfolge_geregelt", "umsatz_teur",
            "bilanz_teur", "mitarbeiter", "nachfolge_signale", "website"]
    edited = st.data_editor(
        view[cols],
        hide_index=True,
        width="stretch",
        height=560,
        column_config={
            "Lead": st.column_config.CheckboxColumn("Lead", help="als Lead markieren", width="small"),
            "name": st.column_config.TextColumn("Firma", width="medium"),
            "branche_wz": st.column_config.TextColumn("WZ", width="small"),
            "bwl_affinitaet": st.column_config.TextColumn("BWL", width="small"),
            "klasse": st.column_config.TextColumn("Kl.", width="small"),
            "gf_alter": st.column_config.NumberColumn("GF-Alter", width="small"),
            "familie": st.column_config.CheckboxColumn("Fam.", width="small"),
            "nachfolge_geregelt": st.column_config.CheckboxColumn("Nf ger.", width="small",
                                                                  help="nächste Generation steht laut Website bereit"),
            "nachfolge_signale": st.column_config.TextColumn("Nachfolge-Signale", width="large"),
            "umsatz_teur": st.column_config.NumberColumn("Umsatz T€", format="localized", width="small"),
            "bilanz_teur": st.column_config.NumberColumn("Bilanz T€", format="localized", width="small"),
            "website": st.column_config.LinkColumn("Web", width="small"),
        },
        disabled=[c for c in cols if c != "Lead"],
        key=f"editor_w{wave}",
    )
    # Häkchen -> decision. Anhaken = Lead; Abhaken = zurück auf offen
    # (überschreibt ein bewusstes "uninteressant" aus der Karteikarte nicht).
    for cid, want in zip(view["company_id"].tolist(), edited["Lead"].tolist()):
        cur = reviews.setdefault(cid, {"decision": None, "note_self": "", "note_ki": ""})
        if want:
            cur["decision"] = "lead"
        elif cur.get("decision") == "lead":
            cur["decision"] = None

    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Speichern", type="primary", key="save_tbl"):
        n = save_reviews(int(wave), reviews, df)
        st.success(f"Welle {wave}: {n} Leads gespeichert nach selection.jsonl")
        st.rerun()
    if b2.button("Sichtbare als Lead", key="markall"):
        for cid in view["company_id"].tolist():
            reviews.setdefault(cid, {"note_self": "", "note_ki": ""})["decision"] = "lead"
        save_reviews(int(wave), reviews, df)
        st.rerun()

# =========================== KARTEIKARTE =========================== #
with tab_card:
    ids = f["company_id"].tolist()
    if not ids:
        st.info("Kein Treffer im aktuellen Filter.")
    else:
        pos = max(0, min(int(st.session_state.get("card_pos", 0)), len(ids) - 1))
        cur_cid = ids[pos]
        dkey, skey, kkey = f"dec_{wave}_{cur_cid}", f"self_{wave}_{cur_cid}", f"ki_{wave}_{cur_cid}"

        def _store_current():
            reviews[cur_cid] = {
                "decision": _DEC_TO_STORE.get(st.session_state.get(dkey, "—")),
                "note_self": st.session_state.get(skey, ""),
                "note_ki": st.session_state.get(kkey, ""),
            }

        # --- Navigation ---
        n1, n2, n3 = st.columns([1, 4, 1])
        if n1.button("◀ Zurück", width="stretch", disabled=pos == 0, key="prev"):
            _store_current(); save_reviews(int(wave), reviews, df)
            st.session_state["card_pos"] = pos - 1
            st.rerun()
        if n3.button("Weiter ▶", width="stretch", disabled=pos >= len(ids) - 1, key="next"):
            _store_current(); save_reviews(int(wave), reviews, df)
            st.session_state["card_pos"] = pos + 1
            st.rerun()
        badge = {"lead": "✅ Lead", "uninteressant": "🚫 uninteressant"}.get(
            reviews.get(cur_cid, {}).get("decision"), "· offen")
        n2.markdown(
            f"<div style='text-align:center'>Firma <b>{pos+1}</b> von <b>{len(ids)}</b>"
            f" &nbsp;·&nbsp; {badge}</div>", unsafe_allow_html=True)

        row = f[f["company_id"] == cur_cid].iloc[0]

        # --- Kopf ---
        reg = (row.get("register_id") or "").strip()
        court = (row.get("hr_amtsgericht") or "").strip()
        reg_label = (reg + (f" · AG {court}" if court else "")) if reg else ""
        st.markdown(
            f"<div style='font-size:1.35rem;font-weight:700;margin:0.1rem 0'>{row['name']}"
            f"<span style='font-size:0.85rem;font-weight:400;color:#888;margin-left:0.6rem'>"
            f"{reg_label}</span></div>",
            unsafe_allow_html=True)
        ad_pdf = row.get("ad_pdf")
        if ad_pdf and Path(ad_pdf).exists():
            with open(ad_pdf, "rb") as _fh:
                st.download_button(
                    f"📄 AD-Auszug öffnen{(' · ' + reg) if reg else ''}",
                    data=_fh.read(), file_name=Path(ad_pdf).name,
                    mime="application/pdf", key=f"adpdf_{cur_cid}")
        st.caption(
            f"Klasse **{row['klasse']}** (Score {row['score']})  |  "
            f"{row['region']} · {row['ort']} · {row['plz']}  |  "
            f"WZ {row['branche_wz']} · {row['cluster']} · BWL {row['bwl_affinitaet']}")

        def _pct(v):
            return f"{v:.1f}".replace(".", ",") + " %" if pd.notna(v) else "—"

        def _i(v):
            return str(int(v)) if pd.notna(v) else "—"

        def _teur_cell(v):
            return f"{_fmt_teur(v)} T€" if pd.notna(v) else "—"

        # Kopf-Textzeilen: einfache Zeilenumbrüche statt Absätze
        meta = []
        if row["familie"]:
            meta.append("Familienunternehmen")
        if row["nachfolge_geregelt"]:
            meta.append("⚠ Nachfolge intern geregelt")
        if row["berater"]:
            meta.append("Berater-Branche")
        if row["website"]:
            meta.append(f"[🔗 Website]({row['website']})")
        lines = []
        if row["geschaeftsmodell"]:
            lines.append(f"**Geschäftsmodell:** {row['geschaeftsmodell']}")
        if meta:
            lines.append(" · ".join(meta))
        if row["schmerzpunkt"]:
            lines.append(f"**Schmerzpunkt (Brief):** {row['schmerzpunkt']}")
        if lines:
            st.markdown("<br>".join(lines), unsafe_allow_html=True)
        if row["naechste_generation"]:
            st.info(f"**Nächste Generation:** {row['naechste_generation']}")

        offene = "; ".join(row["kaufm_stellen"]) if row["kaufm_stellen"] else "—"
        ca, cb, cc, cd = st.columns(4)
        ca.markdown(_kv_table([
            ("Umsatz", _teur_cell(row["umsatz_teur"])),
            ("Bilanzsumme", _teur_cell(row["bilanz_teur"])),
            ("Mitarbeiter", _i(row["mitarbeiter"])),
        ]), unsafe_allow_html=True)
        cb.markdown(_kv_table([
            ("EK-Quote", _pct(row["ek_quote"])),
            ("Gewinn-CAGR", _pct(row["cagr"])),
        ]), unsafe_allow_html=True)
        cc.markdown(_kv_table([
            ("GF-Alter (ältester)", _i(row["gf_alter"])),
            ("GF-Name in Firma", "ja" if row["gf_name_in_name"] else "nein"),
        ]), unsafe_allow_html=True)
        cd.markdown(_kv_table([
            ("Kaufm. Funktion besetzt", _tri(row["kaufm_besetzt"])),
            ("2. Ebene sichtbar", _tri(row["zweite_ebene"])),
            ("Offene kaufm. Stellen", offene),
        ]), unsafe_allow_html=True)

        # Geschäftsführung + Prokura aus dem AD (Name + Alter je Person, ältester zuerst).
        pers = row.get("personen") or []
        if pers:
            def _fmt_p(p):
                a = f" ({p['alter']})" if p["alter"] is not None else ""
                return f"{p['name']}{a}"

            def _by_age_desc(ps):
                return sorted(ps, key=lambda x: (x["alter"] is None, -(x["alter"] or 0)))

            gfs = _by_age_desc([p for p in pers if p["ist_gf"]])
            proks = _by_age_desc([p for p in pers if not p["ist_gf"]])
            plines = []
            if gfs:
                plines.append("**Geschäftsführer:** " + " · ".join(_fmt_p(p) for p in gfs))
            if proks:
                plines.append("**Prokura:** " + " · ".join(_fmt_p(p) for p in proks))
            if plines:
                st.markdown("<br>".join(plines), unsafe_allow_html=True)
            # Hartes Nachfolge-Signal direkt neben der GF-Angabe.
            mark = ("<span style='color:#c00;font-weight:700'>ja</span>"
                    if row.get("generationswechsel")
                    else "<span style='color:#000'>—</span>")
            st.markdown(f"**Generationswechsel vollzogen?** {mark}", unsafe_allow_html=True)

        if row["nachfolge_signale"]:
            st.markdown(f"**Nachfolge-Signale:** {row['nachfolge_signale']}")

        if row["belege"]:
            with st.expander(f"Belege ({len(row['belege'])})", expanded=False):
                for b in row["belege"]:
                    line = f"**{b['type']}** — „{b['zitat']}“"
                    if b["url"]:
                        line += f" — [Quelle]({b['url']})"
                    st.markdown(line)
        if row["hooks"]:
            with st.expander(f"Ansprache-Hooks ({len(row['hooks'])})", expanded=False):
                for h in row["hooks"]:
                    st.markdown(f"- {h}")
        with st.expander("Briefing-Rohtext (für Anruf)", expanded=False):
            st.text(row["begruendung"] or "—")

        # --- Entscheidung + Notizen (Widget-State je Firma initialisieren) ---
        st.session_state.setdefault(dkey, _STORE_TO_DEC.get(reviews.get(cur_cid, {}).get("decision")))
        st.session_state.setdefault(skey, reviews.get(cur_cid, {}).get("note_self", ""))
        st.session_state.setdefault(kkey, reviews.get(cur_cid, {}).get("note_ki", ""))

        st.radio("Entscheidung", DECISIONS, horizontal=True, key=dkey, label_visibility="collapsed")
        cc = st.columns(2)
        cc[0].text_area("Mein Kommentar", key=skey, height=120,
                        placeholder="Notiz für mich (Recherche, Timing, Kontakt …)")
        cc[1].text_area("An die KI", key=kkey, height=120,
                        placeholder="z. B. Fehlerkorrektur: GF-Alter falsch, falsches Signal, Dossier nachschärfen …")

        _store_current()   # laufenden Stand in den Review-State spiegeln
        if st.columns([1, 5])[0].button("Karte speichern", type="primary", key="save_card"):
            save_reviews(int(wave), reviews, df)
            st.toast("Gespeichert")

        # Echte Pfeiltasten ←/→ (greift nicht, während in einem Textfeld getippt wird)
        components.html(
            """
            <script>
            const doc = window.parent.document;
            if (!doc.__cardNavBound) {
              doc.__cardNavBound = true;
              doc.addEventListener('keydown', function(e){
                const t = e.target;
                if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) return;
                let label = null;
                if (e.key === 'ArrowRight') label = 'Weiter';
                else if (e.key === 'ArrowLeft') label = 'Zurück';
                if (!label) return;
                for (const b of doc.querySelectorAll('button')) {
                  if (b.innerText && b.innerText.indexOf(label) !== -1 && !b.disabled) { b.click(); break; }
                }
              });
            }
            </script>
            """,
            height=0,
        )

st.caption(
    f"Auswahl: {SELECTION_FILE}  ·  c5_export liest selected==true (decision==lead) je Welle.  "
    f"Default Welle 1: Köln-Bonn, BWL-fern/mittel, ohne Berater, GF-Alter ≥ 58."
)
