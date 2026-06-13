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

import json
import os
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


def _teur(v):
    """EUR -> Tausend-EUR, ganzzahlig; None/leer bleibt None."""
    try:
        return int(round(float(v) / 1000)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

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

    client = get_client("calvoran")
    scores = {s["company_id"]: s for s in _fetch_all(
        client, "scores", "company_id,score_klasse,score_total,cluster_branche,cluster_key,begruendung")}
    comp = {c["id"]: c for c in _fetch_all(
        client, "companies",
        "id,name,plz,ort,branche_wz,gf_alter,umsatz_eur,bilanzsumme_eur,mitarbeiterzahl,website,"
        "holding_flag,excluded,dup_of")}
    dossiers = {d["company_id"]: (d.get("dossier") or {}) for d in _fetch_all(
        client, "dossiers", "company_id,dossier")}

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
        rows.append({
            "company_id": cid,
            "name": c.get("name") or "",
            "plz": plz, "plz2": plz2,
            "region": REGION_LABELS.get(plz2, plz2 or "?"),
            "ort": c.get("ort") or "",
            "branche_wz": wz,
            "bwl_affinitaet": aff.get(wz2, "mittel"),
            "berater": wz2 in blacklist,
            "cluster": s.get("cluster_branche") or "rest",
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
        nf_geregelt_aus = st.checkbox("Nachfolge geregelt ausblenden", value=True,
                                      help="Firmen, bei denen die nächste Generation laut Website bereitsteht (kein Verkaufsanlass).")
        alter_unbekannt = st.checkbox("GF-Alter unbekannt einschließen", value=False)
        groesse_unbekannt = st.checkbox("ohne Umsatz/Bilanz einschließen", value=True)

    suche = st.text_input("Volltextsuche (Name/Ort)", "")

# --- Filter anwenden ---
f = df.copy()
f = f[f["plz2"].isin(plz_sel)]
f = f[f["klasse"].isin(klassen)]
f = f[f["bwl_affinitaet"].isin(bwl_sel)]
f = f[f["cluster"].isin(cluster_sel)]
if ohne_berater:
    f = f[~f["berater"]]
if nf_geregelt_aus:
    f = f[~f["nachfolge_geregelt"]]
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
        st.markdown(f"<div style='font-size:1.35rem;font-weight:700;margin:0.1rem 0'>{row['name']}</div>",
                    unsafe_allow_html=True)
        st.caption(
            f"Klasse **{row['klasse']}** (Score {row['score']})  |  "
            f"{row['region']} · {row['ort']} · {row['plz']}  |  "
            f"WZ {row['branche_wz']} · {row['cluster']} · BWL {row['bwl_affinitaet']}")

        k = st.columns(4)
        k[0].metric("GF-Alter", int(row["gf_alter"]) if pd.notna(row["gf_alter"]) else "—")
        k[1].metric("Umsatz T€", _fmt_teur(row["umsatz_teur"]))
        k[2].metric("Bilanz T€", _fmt_teur(row["bilanz_teur"]))
        k[3].metric("Mitarbeiter", int(row["mitarbeiter"]) if pd.notna(row["mitarbeiter"]) else "—")

        flags = []
        if row["familie"]:
            flags.append("Familienunternehmen")
        if row["nachfolge_geregelt"]:
            flags.append("⚠ Nachfolge intern geregelt")
        if row["berater"]:
            flags.append("Berater-Branche")
        if flags:
            st.markdown(" · ".join(flags))
        if row["website"]:
            st.markdown(f"🔗 [{row['website']}]({row['website']})")
        if row["naechste_generation"]:
            st.info(f"**Nächste Generation:** {row['naechste_generation']}")
        if row["nachfolge_signale"]:
            st.markdown(f"**Nachfolge-Signale:** {row['nachfolge_signale']}")

        with st.expander("Anruf-Briefing (Score-Begründung)", expanded=True):
            st.text(row["begruendung"] or "—")

        st.divider()

        # --- Entscheidung + Notizen (Widget-State je Firma initialisieren) ---
        st.session_state.setdefault(dkey, _STORE_TO_DEC.get(reviews.get(cur_cid, {}).get("decision")))
        st.session_state.setdefault(skey, reviews.get(cur_cid, {}).get("note_self", ""))
        st.session_state.setdefault(kkey, reviews.get(cur_cid, {}).get("note_ki", ""))

        st.radio("Entscheidung", DECISIONS, horizontal=True, key=dkey)
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
