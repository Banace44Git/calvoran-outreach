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
            "nachfolge_signale": "; ".join(nachfolge)[:140],
            "begruendung": (s.get("begruendung") or "")[:300],
            "umsatz_teur": _teur(c.get("umsatz_eur")),
            "bilanz_teur": _teur(c.get("bilanzsumme_eur")),
            "mitarbeiter": c.get("mitarbeiterzahl"),
            "website": c.get("website") or "",
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Auswahl-Persistenz (JSONL je Welle)
# --------------------------------------------------------------------------- #
def load_selection(wave: int) -> set:
    if not SELECTION_FILE.exists():
        return set()
    sel = set()
    for line in SELECTION_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("wave") == wave and r.get("selected"):
            sel.add(r.get("company_id"))
    return sel


def save_selection(wave: int, selected: set, frame: pd.DataFrame) -> int:
    """Überschreibt die Einträge dieser Welle, lässt andere Wellen unberührt."""
    other = []
    if SELECTION_FILE.exists():
        for line in SELECTION_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("wave") != wave:
                other.append(r)
    ts = datetime.now(timezone.utc).isoformat()
    by_id = frame.set_index("company_id")
    new = []
    for cid in selected:
        name = by_id.loc[cid, "name"] if cid in by_id.index else ""
        new.append({"company_id": cid, "name": name, "wave": wave,
                    "selected": True, "selected_at": ts})
    SELECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SELECTION_FILE, "w", encoding="utf-8") as f:
        for r in other + new:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return len(new)


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

# Auswahl-State je Welle in der Session halten (überlebt Reruns)
state_key = f"selected_w{wave}"
if state_key not in st.session_state:
    st.session_state[state_key] = load_selection(int(wave))
selected: set = st.session_state[state_key]

# --- Filterleiste über der Tabelle ---
plz_opts = sorted(df["plz2"].dropna().unique().tolist())
cluster_opts = sorted(df["cluster"].dropna().unique().tolist())
plz_fmt = lambda p: f"{p} · {REGION_LABELS.get(p, '')}".strip(" ·")
umax = int(df["umsatz_teur"].dropna().max()) if df["umsatz_teur"].notna().any() else 0
bmax = int(df["bilanz_teur"].dropna().max()) if df["bilanz_teur"].notna().any() else 0

with st.container(border=True):
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
c1, c2, c3, c4 = st.columns(4)
c1.metric("Treffer im Filter", len(f))
c2.metric("davon markiert", int(f["company_id"].isin(selected).sum()))
c3.metric("markiert gesamt (Welle)", len(selected))
gespeichert = load_selection(int(wave))
c4.metric("gespeichert", len(gespeichert),
          delta=(len(selected) - len(gespeichert)) or None,
          delta_color="off")

# --- Tabelle mit Auswahl-Checkbox ---
view = f.copy()
view.insert(0, "wählen", view["company_id"].isin(selected))
cols = ["wählen", "name", "region", "ort", "branche_wz", "bwl_affinitaet", "cluster",
        "klasse", "score", "gf_alter", "familie", "umsatz_teur", "bilanz_teur", "mitarbeiter",
        "nachfolge_signale", "begruendung", "website"]
edited = st.data_editor(
    view[cols],
    hide_index=True,
    width="stretch",
    height=560,
    column_config={
        "wählen": st.column_config.CheckboxColumn("wählen", help="für Ansprache markieren", width="small"),
        "name": st.column_config.TextColumn("Firma", width="medium"),
        "branche_wz": st.column_config.TextColumn("WZ", width="small"),
        "bwl_affinitaet": st.column_config.TextColumn("BWL", width="small"),
        "klasse": st.column_config.TextColumn("Kl.", width="small"),
        "gf_alter": st.column_config.NumberColumn("GF-Alter", width="small"),
        "familie": st.column_config.CheckboxColumn("Fam.", width="small"),
        "nachfolge_signale": st.column_config.TextColumn("Nachfolge-Signale", width="large"),
        "begruendung": st.column_config.TextColumn("Anruf-Briefing (Score)", width="large"),
        "umsatz_teur": st.column_config.NumberColumn("Umsatz T€", format="localized", width="small"),
        "bilanz_teur": st.column_config.NumberColumn("Bilanz T€", format="localized", width="small"),
        "website": st.column_config.LinkColumn("Web", width="small"),
    },
    disabled=[c for c in cols if c != "wählen"],
    key=f"editor_w{wave}",
)

# --- Markierungen aus den sichtbaren Zeilen ins globale Set mergen ---
vis = view["company_id"].tolist()
for cid, want in zip(vis, edited["wählen"].tolist()):
    if want:
        selected.add(cid)
    else:
        selected.discard(cid)

# --- Aktionen ---
a1, a2, a3 = st.columns([1, 1, 4])
if a1.button("Auswahl speichern", type="primary"):
    n = save_selection(int(wave), selected, df)
    st.success(f"{n} Firmen für Welle {wave} gespeichert nach selection.jsonl")
    st.rerun()
if a2.button("Sichtbare alle markieren"):
    for cid in vis:
        selected.add(cid)
    st.rerun()

st.caption(
    f"Auswahl: {SELECTION_FILE}  ·  c5_export liest selected==true für die Welle.  "
    f"Default Welle 1: Köln-Bonn, BWL-fern/mittel, ohne Berater, GF-Alter ≥ 58."
)
