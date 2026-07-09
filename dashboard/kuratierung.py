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
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import phonenumbers
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


def _tel_href(tel: str) -> str:
    """'+49 2303 301030' -> 'tel:+492303301030' (nur Ziffern und +); leer -> ''."""
    cleaned = re.sub(r"[^\d+]", "", tel or "")
    return f"tel:{cleaned}" if cleaned else ""


def _din_phone(raw: str) -> str:
    """Deutsche Rufnummer DIN-5008-nah: '+49 228648040' -> '+49(0) 228 64 80 40'.
    Vorwahl über libphonenumber getrennt, Teilnehmernummer in Zweiergruppen von rechts.
    Ungültiges/Ausländisches bleibt Rohwert."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        x = phonenumbers.parse(raw, "DE")
        if not phonenumbers.is_valid_number(x):
            return raw
        nat = phonenumbers.format_number(x, phonenumbers.PhoneNumberFormat.NATIONAL)
    except phonenumbers.NumberParseException:
        return raw
    head, _, rest = nat.partition(" ")
    area = head.lstrip("0")
    local = "".join(c for c in rest if c.isdigit())
    groups: list = []
    while len(local) > 2:
        groups.insert(0, local[-2:])
        local = local[:-2]
    groups.insert(0, local)
    return f"+49(0) {area} " + " ".join(g for g in groups if g)


def _localize(iso: str):
    """ISO-String -> datetime; tz-bewusste Werte in Ortszeit (sonst driftet ein als
    timestamptz gespeicherter Termin um den UTC-Offset)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone() if dt.tzinfo is not None else dt


def _de_date(iso: str) -> str:
    """ISO-Datum/Zeitstempel -> 'TT.MM.JJJJ' (europäisch); leer/kaputt -> Rohschnitt."""
    iso = (iso or "").strip()
    if not iso:
        return ""
    try:
        return _localize(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso[:10]


def _de_dt(iso: str) -> str:
    """ISO-Zeitstempel -> 'TT.MM.JJJJ HH:MM' (24h, Ortszeit)."""
    iso = (iso or "").strip()
    if not iso:
        return ""
    try:
        return _localize(iso).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso[:16]


# Absender für E-Mail-Nachfass (Gmail-Compose erzwingt das Konto über authuser).
CALVORAN_SENDER = "johannes.breuers@calvoran.de"


def _gmail_compose(to: str, subject: str = "") -> str:
    """Gmail-Web-Compose-Link, der als Absender CALVORAN_SENDER wählt (mailto: kann den
    Absender nicht setzen)."""
    q = f"authuser={quote(CALVORAN_SENDER)}&view=cm&fs=1&to={quote(to)}"
    if subject:
        q += f"&su={quote(subject)}"
    return f"https://mail.google.com/mail/?{q}"


def _briefing(begr: str, ort: str, plz: str, branche: str) -> str:
    """Kuratiertes Anruf-Briefing aus der Score-Begründung: Titelzeile (Score/Klasse),
    Cluster/WZ und Web-Bedarf-Zeile (inkl. '2. Ebene sichtbar') raus; Standort-Zeile mit
    Branche statt WZ/Cluster; die begründungseigene 'Hooks:'-Zeile raus (wird separat einmal
    gerendert)."""
    loc = " ".join(p for p in [(ort or "").strip(), f"({plz})" if plz else ""] if p)
    out: list = []
    for ln in (begr or "").splitlines():
        s = ln.strip()
        if re.search(r"—\s*Score\s", s):
            continue
        if s.startswith("Standort"):
            out.append(f"Standort {loc} · Branche: {branche}".rstrip(" ··")
                       if branche else (f"Standort {loc}" if loc else ""))
            continue
        if s.startswith("Web-Bedarf"):
            continue
        if s.startswith("Hooks:"):
            continue
        out.append(ln)
    return "\n".join(out).strip()


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
def _fetch_all(client, tbl, cols, order="id"):
    """Paginierter Voll-Abruf MIT stabiler Sortierung. Ohne ORDER BY ist die Seiten-
    Reihenfolge in Postgres nicht garantiert — laufen parallel Updates (z.B. die externe
    GF-Anreicherung auf companies), wandern Zeilen im Heap und ganze Teilmengen fehlen
    still (beobachtet 2026-07-08: 45.317 von 70.511 companies)."""
    out, step, start = [], 1000, 0
    while True:
        r = (client.table(tbl).select(cols).order(order)
             .range(start, start + step - 1).execute())
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


@st.cache_data(ttl=300, show_spinner=False)
def load_contacts(company_ids: tuple) -> dict:
    """company_id -> {tel, email, gf} — on-demand nur für die Leads einer Welle, aus
    companies.raw ('Tel.'/'E-Mail') + ges_vertreter. Bewusst nicht im Full-Frame, um das
    große raw-JSONB nicht für alle gescorten Firmen mitzuladen."""
    if not company_ids:
        return {}
    cl = get_client()
    ids = list(company_ids)
    out: dict = {}
    for i in range(0, len(ids), 50):
        for c in (cl.table("companies").select("id,ges_vertreter,raw")
                  .in_("id", ids[i:i + 50]).execute().data):
            raw = c.get("raw") or {}
            gv = c.get("ges_vertreter") or []
            out[c["id"]] = {
                "tel": (raw.get("Tel.") or "").strip(),
                "email": (raw.get("E-Mail") or "").strip(),
                "gf": ", ".join(gv) if isinstance(gv, list) else str(gv or ""),
            }
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

# Anruf-Ausgänge (Code -> Label) für das Nachverfolgungs-Tab / outreach_calls.outcome.
# Reihenfolge = Dropdown-Reihenfolge. "erreicht" = alles außer nicht_erreicht/falsche_nummer.
OUTCOMES = {
    "nicht_erreicht": "nicht erreicht",
    "gesprochen": "gesprochen",
    "rueckruf_vereinbart": "Rückruf vereinbart",
    "termin": "Termin vereinbart",
    "kein_interesse": "kein Interesse",
    "nicht_zustaendig": "nicht zuständig",
    "falsche_nummer": "falsche Nummer",
}

# Lead-Disposition = Status der Brief-outreach-Zeile jenseits von queued/sent. Vokabular
# stammt aus sql/schema.sql (won/rejected/no_response) — keine Schema-Änderung nötig.
DISPO = {
    "sent": "offen", "opened": "offen", "replied": "offen",
    "no_response": "keine Reaktion", "won": "gewonnen", "rejected": "verloren",
}
DISPO_STATES = ["sent", "won", "rejected", "no_response"]      # manuell wählbare Disposition
DISPO_LABEL = {"sent": "offen", "won": "gewonnen", "rejected": "verloren",
               "no_response": "keine Reaktion"}
# Vorschlag aus dem letzten Anruf-Ausgang -> Disposition.
OUTCOME_TO_DISPO = {"termin": "won", "kein_interesse": "rejected"}


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

# Filter-Widgets laufen über Session-State-Keys (statt default=-Parameter), damit der
# »Alle Filter entfernen«-Button sie per on_click-Callback zurücksetzen kann — ein
# Widget-Key darf nach Instanziierung nicht gesetzt werden, Callbacks laufen aber davor.
_FILTER_START = {
    "flt_klassen": ["A", "B"],
    "flt_bwl": ["fern", "mittel"],
    "flt_cluster": cluster_opts,
    "flt_plz": plz_opts,
    "flt_alter": 58,
    "flt_umsatz": (0, umax),
    "flt_bilanz": (0, bmax),
    "flt_berater": True,
    "flt_nfger": False,
    "flt_alterunbek": False,
    "flt_groesseunbek": True,
    "flt_nurleads": False,
    "flt_bearb": "alle",
}
for _k, _v in _FILTER_START.items():
    st.session_state.setdefault(_k, _v)


def _filter_entfernen():
    """Neutralstellung: alles sichtbar (inkl. KO), nur der Suchtext bleibt stehen."""
    st.session_state.update({
        "flt_klassen": ["A", "B", "C", "KO"],
        "flt_bwl": ["fern", "mittel", "nah"],
        "flt_cluster": cluster_opts,
        "flt_plz": plz_opts,
        "flt_alter": 40,
        "flt_umsatz": (0, umax),
        "flt_bilanz": (0, bmax),
        "flt_berater": False,
        "flt_nfger": True,
        "flt_alterunbek": True,
        "flt_groesseunbek": True,
        "flt_nurleads": False,
        "flt_bearb": "alle",
    })


with st.expander("Filter / Suchkriterien", expanded=True):
    r1 = st.columns(4)
    klassen = r1[0].multiselect("Score-Klasse", ["A", "B", "C", "KO"], key="flt_klassen")
    bwl_sel = r1[1].multiselect("BWL-Affinität", ["fern", "mittel", "nah"], key="flt_bwl",
                                help="fern = Idealkunde (technischer Inhaber, kaufm. Lücke). nah = depriorisieren.")
    cluster_sel = r1[2].multiselect("Makrocluster", cluster_opts, key="flt_cluster")
    plz_sel = r1[3].multiselect("Region (PLZ)", plz_opts, key="flt_plz", format_func=plz_fmt)

    r2 = st.columns(4)
    alter_min = r2[0].slider("GF-Alter ab", 40, 80, step=1, key="flt_alter")
    umsatz_rng = r2[1].slider("Umsatz T€", 0, umax, step=max(1, umax // 100),
                              key="flt_umsatz") if umax else (0, 0)
    bilanz_rng = r2[2].slider("Bilanz T€", 0, bmax, step=max(1, bmax // 100),
                              key="flt_bilanz") if bmax else (0, 0)
    with r2[3]:
        ohne_berater = st.checkbox("Berater-Branchen ausschließen", key="flt_berater",
                                   help="WZ 69/70/73/74/78 — Berater lassen ungern andere Berater ins Haus.")
        nf_geregelt_zeigen = st.checkbox("auch geregelte Nachfolge anzeigen", key="flt_nfger",
                                         help="Standard: vollzogener/geregelter Generationswechsel ist K.o. "
                                              "(kein Verkaufsanlass) und ausgeblendet. Anhaken blendet diese Firmen "
                                              "— jetzt Klasse KO — wieder ein. Hart: zwei GF gleichen Nachnamens, "
                                              "einer <50. Weich: nächste Generation steht laut Website bereit.")
        alter_unbekannt = st.checkbox("GF-Alter unbekannt einschließen", key="flt_alterunbek")
        groesse_unbekannt = st.checkbox("ohne Umsatz/Bilanz einschließen", key="flt_groesseunbek")
        nur_leads = st.checkbox("nur als Lead markierte", key="flt_nurleads",
                                help="Zeigt ausschließlich Firmen mit Entscheidung „Lead\" "
                                     "(selected==true in selection.jsonl).")

    r3 = st.columns([1, 2.6, 0.9])
    bearbeitung = r3[0].selectbox(
        "Bearbeitungsstatus", ["alle", "offen", "bearbeitet"], key="flt_bearb",
        help="offen = noch nicht entschieden · bearbeitet = als Lead oder uninteressant markiert")
    suche = r3[1].text_input("Volltextsuche (Name/Ort)", "")
    with r3[2]:
        st.markdown("<div style='padding-top:1.8rem'></div>", unsafe_allow_html=True)
        st.button("Alle Filter entfernen", on_click=_filter_entfernen,
                  help="Setzt alle Filter auf »alles anzeigen« (inkl. Klasse KO, GF-Alter "
                       "unbekannt, Berater). Der Suchtext bleibt erhalten — so findest du "
                       "jede Firma im Gesamtbestand.")

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

# Bearbeitungsstatus aus dem Review-State ableiten (decision == lead/uninteressant = bearbeitet)
if bearbeitung != "alle":
    bearbeitet_mask = f["company_id"].map(
        lambda cid: reviews.get(cid, {}).get("decision") in ("lead", "uninteressant"))
    f = f[bearbeitet_mask if bearbeitung == "bearbeitet" else ~bearbeitet_mask]

# Lead-Filter: nur Firmen mit Entscheidung „Lead" (decision == lead == selected)
if nur_leads:
    f = f[f["company_id"].map(lambda cid: reviews.get(cid, {}).get("decision") == "lead")]

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


tab_tbl, tab_card, tab_funnel, tab_jobs = st.tabs(
    ["Tabelle", "Karteikarte", "Nachverfolgung", "Job-Signale"])

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

        # --- Navigation + Entscheidung (kompakt, wenig Mausstrecke) ---
        # Zurück/Weiter direkt nebeneinander; Entscheidung unmittelbar darunter.
        n1, n2, n3 = st.columns([1, 1, 4])
        if n1.button("◀ Zurück", width="stretch", disabled=pos == 0, key="prev"):
            _store_current(); save_reviews(int(wave), reviews, df)
            st.session_state["card_pos"] = pos - 1
            st.rerun()
        if n2.button("Weiter ▶", width="stretch", disabled=pos >= len(ids) - 1, key="next"):
            _store_current(); save_reviews(int(wave), reviews, df)
            st.session_state["card_pos"] = pos + 1
            st.rerun()
        badge = {"lead": "✅ Lead", "uninteressant": "🚫 uninteressant"}.get(
            reviews.get(cur_cid, {}).get("decision"), "· offen")
        n3.markdown(
            f"<div style='text-align:center'>Firma <b>{pos+1}</b> von <b>{len(ids)}</b>"
            f" &nbsp;·&nbsp; {badge}</div>", unsafe_allow_html=True)

        # Entscheidung (offen / Lead / uninteressant) direkt unter Zurück/Weiter
        st.session_state.setdefault(dkey, _STORE_TO_DEC.get(reviews.get(cur_cid, {}).get("decision")))
        st.radio("Entscheidung", DECISIONS, horizontal=True, key=dkey, label_visibility="collapsed")

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

        # --- Notizen (Widget-State je Firma initialisieren; Entscheidung steht oben) ---
        st.session_state.setdefault(skey, reviews.get(cur_cid, {}).get("note_self", ""))
        st.session_state.setdefault(kkey, reviews.get(cur_cid, {}).get("note_ki", ""))

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

# =========================== NACHVERFOLGUNG =========================== #
# CRM-Stufe 1: Brief-Versand (calvoran.outreach) + Nachtelefonieren (calvoran.outreach_calls)
# je Lead der aktuellen Welle. Vorselektion bleibt in selection.jsonl; die DB ist System of
# Record ab der Lead-Menge (Backfill / c5 --wave legen die outreach-Zeilen an).
with tab_funnel:
    st.caption("Brief-Versand und Nachtelefonieren je Lead dieser Welle · "
               "Quelle: Supabase calvoran.outreach + outreach_calls.")
    if not leads:
        st.info("Noch keine Leads in dieser Welle markiert — erst im Tab „Tabelle“ / "
                "„Karteikarte“ Firmen als Lead markieren und speichern.")
    else:
        cl = get_client()
        by_id = df.set_index("company_id")

        # --- Brief- + E-Mail-Status laden (outreach, diese Welle) ---
        outreach: dict = {}      # company_id -> Brief-Zeile (channel='letter')
        emails: dict = {}        # company_id -> E-Mail-Nachfass-Zeile (channel='email')
        try:
            for i in range(0, len(leads), 50):
                for r in (cl.table("outreach")
                          .select("id,company_id,channel,status,sent_at,response_at")
                          .in_("channel", ["letter", "email"]).eq("wave", int(wave))
                          .in_("company_id", leads[i:i + 50]).execute().data):
                    (outreach if r["channel"] == "letter" else emails)[r["company_id"]] = r
        except Exception as e:                                # noqa: BLE001
            st.error(f"outreach-Lesefehler: {e}")

        # --- Kontaktdaten (Tel./E-Mail/GF) on-demand nur für die Leads ---
        contact = load_contacts(tuple(sorted(leads)))

        # --- Anrufe laden (outreach_calls; existiert erst nach Migration 0006) ---
        calls: dict = {}
        calls_active = True
        try:
            for i in range(0, len(leads), 50):
                for r in (cl.table("outreach_calls")
                          .select("company_id,called_at,outcome,follow_up_at,notes")
                          .in_("company_id", leads[i:i + 50])
                          .order("called_at", desc=True).execute().data):
                    calls.setdefault(r["company_id"], []).append(r)
        except Exception:                                     # noqa: BLE001
            calls_active = False
            st.warning("Anruf-Log inaktiv — Migration 0006 (outreach_calls) noch nicht im "
                       "Supabase-Studio angewandt. Brief-Status funktioniert bereits.")

        def _reached(cid: str) -> bool:
            return any(c["outcome"] not in ("nicht_erreicht", "falsche_nummer")
                       for c in calls.get(cid, []))

        def _name(cid: str) -> str:
            return by_id.loc[cid, "name"] if cid in by_id.index else cid

        def _score(cid: str):
            return by_id.loc[cid, "score"] if cid in by_id.index else -1

        def _followup(cid: str):
            """Wiedervorlage-Datum des jüngsten Anrufs, der eine gesetzt hat (ISO), sonst None.
            calls sind desc nach called_at sortiert, der erste Treffer ist also der aktuellste."""
            for c in calls.get(cid, []):
                if c.get("follow_up_at"):
                    return c["follow_up_at"][:10]
            return None

        today_iso = date.today().isoformat()

        def _priority(cid: str) -> int:
            """0 überfällig · 1 heute fällig · 2 neu (nie angerufen) · 3 offen ·
            4 keine Reaktion · 5 abgeschlossen (gewonnen/verloren)."""
            lstat = (outreach.get(cid) or {}).get("status")
            if lstat in ("won", "rejected"):
                return 5
            fu = _followup(cid)
            if fu and fu < today_iso:
                return 0
            if fu and fu == today_iso:
                return 1
            if not calls.get(cid):
                return 2
            if lstat == "no_response":
                return 4
            return 3

        # --- Funnel-Kennzahlen: Aktivität + Ergebnis ---
        n_leads = len(leads)
        n_brief = sum(1 for cid in leads if cid in outreach)
        n_sent = sum(1 for cid in leads
                     if outreach.get(cid, {}).get("status") not in (None, "queued"))
        n_mail = sum(1 for cid in leads if emails.get(cid, {}).get("status") == "sent")
        n_called = sum(1 for cid in leads if calls.get(cid))
        n_reached = sum(1 for cid in leads if _reached(cid))
        n_due = sum(1 for cid in leads
                    if (fu := _followup(cid)) and fu <= today_iso
                    and outreach.get(cid, {}).get("status") not in ("won", "rejected"))
        n_won = sum(1 for cid in leads if outreach.get(cid, {}).get("status") == "won")
        n_lost = sum(1 for cid in leads if outreach.get(cid, {}).get("status") == "rejected")
        n_open = n_leads - n_won - n_lost
        a = st.columns(5)
        a[0].metric("Leads", n_leads)
        a[1].metric("versandt", n_sent)
        a[2].metric("E-Mail-Nachfass", n_mail)
        a[3].metric("angerufen", n_called)
        a[4].metric("erreicht", n_reached)
        b = st.columns(5)
        b[0].metric("offen", n_open)
        b[1].metric("gewonnen", n_won)
        b[2].metric("verloren", n_lost)
        b[3].metric("Wiedervorlagen fällig", n_due)
        b[4].metric("Brief erfasst", n_brief)
        if n_brief < n_leads:
            st.caption(f"{n_leads - n_brief} Leads ohne outreach-Zeile — einmalig "
                       "`pipeline/backfill_outreach_from_selection.py` laufen lassen "
                       "oder Briefe mit `c5 --wave` erzeugen.")

        # --- Brief-Versand markieren (queued -> sent für die ganze Welle) ---
        with st.expander("Brief-Versand markieren", expanded=False):
            n_queued = sum(1 for cid in leads
                           if outreach.get(cid, {}).get("status") == "queued")
            cvs = st.columns([2, 1, 1])
            sent_date = cvs[0].date_input("Versanddatum", value=date.today(), key="sent_date")
            cvs[1].markdown(f"<div style='padding-top:1.8rem'>{n_queued} offen (queued)</div>",
                            unsafe_allow_html=True)
            with cvs[2]:
                st.markdown("<div style='padding-top:1.0rem'></div>", unsafe_allow_html=True)
                if st.button(f"Welle {int(wave)} als versandt", type="primary",
                             disabled=n_queued == 0, key="mark_sent"):
                    try:
                        (cl.table("outreach")
                         .update({"status": "sent", "sent_at": sent_date.isoformat()})
                         .eq("channel", "letter").eq("wave", int(wave))
                         .eq("status", "queued").execute())
                        st.toast(f"{n_queued} Briefe als versandt markiert.")
                        st.rerun()
                    except Exception as e:                    # noqa: BLE001
                        st.error(f"Update fehlgeschlagen: {e}")

        # --- Priorisierte Arbeitsliste (wen jetzt anrufen) ---
        st.markdown("##### Arbeitsliste — priorisiert")
        nur_offen = st.checkbox("nur noch nicht abgeschlossene", value=True, key="nf_open",
                                help="blendet gewonnene/verlorene Leads aus")
        PRIO_LABEL = {0: "überfällig", 1: "heute fällig", 2: "neu", 3: "offen",
                      4: "keine Reaktion", 5: "abgeschlossen"}
        ordered = sorted(leads, key=lambda cid: (_priority(cid), -(_score(cid) or -1),
                                                  _name(cid).lower()))
        rows = []
        ordered_shown = []       # cids in Anzeige-Reihenfolge, parallel zu rows
        for cid in ordered:
            prio = _priority(cid)
            if nur_offen and prio == 5:
                continue
            cs = calls.get(cid, [])
            last = cs[0] if cs else None
            fu = _followup(cid)
            ct = contact.get(cid, {})
            faellig = ("überfällig" if fu and fu < today_iso else
                       "heute" if fu and fu == today_iso else "")
            ordered_shown.append(cid)
            rows.append({
                "öffnen": False,
                "Prio": PRIO_LABEL[prio],
                "Firma": _name(cid),
                "Tel.": _din_phone(ct.get("tel", "")),
                "GF": ct.get("gf", ""),
                "Anrufe": len(cs),
                "letzter Ausgang": OUTCOMES.get(last["outcome"], "") if last else "—",
                "Wiedervorlage": _de_date(fu),
                "fällig": faellig,
                "Disposition": DISPO.get((outreach.get(cid) or {}).get("status"), "—"),
                "E-Mail": "gesendet" if emails.get(cid, {}).get("status") == "sent" else "",
            })
        if rows:
            st.caption("Spalte „öffnen“ anhaken → die Firma öffnet unten im Anruf-Cockpit.")
            # data_editor statt dataframe, weil nur so eine anklickbare Spalte möglich ist.
            # Der Editor-Key trägt eine Nonce, damit der Haken nach dem Öffnen zurückgesetzt
            # wird (frischer Editor). Alle übrigen Spalten sind schreibgeschützt.
            wl_nonce = st.session_state.get("wl_nonce", 0)
            edited = st.data_editor(
                pd.DataFrame(rows), hide_index=True, width="stretch", height=360,
                key=f"wl_{wl_nonce}",
                disabled=[c for c in rows[0] if c != "öffnen"],
                column_config={"öffnen": st.column_config.CheckboxColumn(
                    "öffnen", help="im Anruf-Cockpit öffnen", width="small")})
            picked = [i for i, v in enumerate(edited["öffnen"].tolist()) if v]
            if picked:
                st.session_state["call_firma_next"] = ordered_shown[picked[0]]
                st.session_state["wl_nonce"] = wl_nonce + 1
                st.rerun()
        else:
            st.info("Keine offenen Leads — alle abgeschlossen.")

        # --- Anruf-Cockpit: Kontaktkarte + Briefing + Erfassung + Disposition/E-Mail ---
        st.markdown("##### Anruf-Cockpit")
        # Firmen in Priorisierungs-Reihenfolge (cid = stabiler Selectbox-Wert, damit
        # Auto-Advance nach dem Speichern robust auf die nächste Firma springt).
        # Alle Leads wählbar (auch abgeschlossene, damit „öffnen“ aus der Liste jede Zeile
        # trifft); offene stehen durch die Prio-Sortierung ohnehin oben.
        cockpit_order = ordered
        # Auto-Advance einlösen: ein Widget-Key darf nach Instanziierung nicht mehr gesetzt
        # werden, daher der Umweg über den freien Key 'call_firma_next', der VOR dem Selectbox
        # in den Widget-Key übernommen wird.
        _pending = st.session_state.pop("call_firma_next", "__keep__")
        if _pending != "__keep__":
            if _pending in cockpit_order:
                st.session_state["call_firma"] = _pending
            else:
                st.session_state.pop("call_firma", None)
        if st.session_state.get("call_firma") not in cockpit_order:
            st.session_state.pop("call_firma", None)
        sel_cid = st.selectbox(
            "Firma", cockpit_order, key="call_firma",
            format_func=lambda cid: f"{PRIO_LABEL[_priority(cid)]} · {_name(cid)}")
        sel_name = _name(sel_cid)
        ct = contact.get(sel_cid, {})

        # Kontaktkarte: DIN-Telefon als klickbarer tel:-Link, E-Mail als Gmail-Compose
        # (Absender = CALVORAN_SENDER), Website.
        links = []
        if ct.get("tel"):
            links.append(f"[{_din_phone(ct['tel'])}]({_tel_href(ct['tel'])})")
        if ct.get("email"):
            links.append(f"[{ct['email']}]({_gmail_compose(ct['email'])})")
        web = by_id.loc[sel_cid, "website"] if sel_cid in by_id.index else ""
        if web:
            links.append(f"[Website]({web})")
        st.markdown(f"**{sel_name}** · GF: {ct.get('gf') or '—'}")
        st.markdown("Kontakt: " + (" · ".join(links) if links
                    else "keine Kontaktdaten (Nummer manuell/Lusha nachziehen)"))

        # Anruf-Briefing: kuratierte Score-Begründung (ohne Score/Klasse/Cluster/Web-Bedarf,
        # Standort mit Branche) + Ansprache-Hooks einmalig.
        if sel_cid in by_id.index:
            row = by_id.loc[sel_cid]
            brief = _briefing(row.get("begruendung", ""), row.get("ort", ""),
                              row.get("plz", ""), row.get("branche_wz", ""))
            hooks = row.get("hooks")
            with st.expander("Anruf-Briefing", expanded=True):
                if brief:
                    st.markdown(brief.replace("\n", "  \n"))
                if isinstance(hooks, (list, tuple)) and len(hooks):
                    st.markdown("**Hooks:** " + " · ".join(str(h) for h in hooks))

        # --- Anruf erfassen (nicht-Form, damit 'Termin' sofort Datum+Uhrzeit einblendet) ---
        if calls_active:
            st.markdown("**Anruf erfassen**")
            r1 = st.columns([1, 1])
            c_date = r1[0].date_input("Datum des Anrufs", value=date.today(),
                                      format="DD.MM.YYYY", key=f"cdate_{sel_cid}")
            c_outcome = r1[1].selectbox("Ausgang", list(OUTCOMES),
                                        format_func=lambda o: OUTCOMES[o], key=f"cout_{sel_cid}")
            fu_iso = None
            if c_outcome == "termin":
                r2 = st.columns([1, 1])
                t_date = r2[0].date_input("Termin am", value=date.today(),
                                          format="DD.MM.YYYY", key=f"tdate_{sel_cid}")
                t_time = r2[1].time_input("Uhrzeit", value=time(10, 0), key=f"ttime_{sel_cid}")
                # Ortszeit-Offset mitschreiben, sonst driftet der Termin als timestamptz.
                fu_iso = datetime.combine(t_date, t_time).astimezone().isoformat()
            else:
                r2 = st.columns([1, 1])
                set_fu = r2[0].checkbox("Wiedervorlage setzen", key=f"cfu_{sel_cid}")
                fu_date = r2[1].date_input("Wiedervorlage am", value=date.today(),
                                           format="DD.MM.YYYY", key=f"cfud_{sel_cid}")
                if set_fu:
                    fu_iso = fu_date.isoformat()
            c_notes = st.text_area(
                "Notiz", key=f"cnotes_{sel_cid}",
                placeholder="Gesprächsnotiz, Ansprechpartner, nächster Schritt …")
            if st.button("Anruf speichern", type="primary", key=f"csave_{sel_cid}"):
                rec = {"company_id": sel_cid, "called_at": c_date.isoformat(),
                       "outcome": c_outcome, "notes": c_notes.strip() or None}
                oid = outreach.get(sel_cid, {}).get("id")
                if oid:
                    rec["outreach_id"] = oid
                if fu_iso:
                    rec["follow_up_at"] = fu_iso
                try:
                    cl.table("outreach_calls").insert(rec).execute()
                    st.toast("Anruf gespeichert.")
                    # Auto-Advance: nächsten OFFENEN Lead in der Reihenfolge öffnen. Freier
                    # Key (kein Widget-Key) -> im nächsten Run vor dem Selectbox eingelöst.
                    idx = cockpit_order.index(sel_cid)
                    st.session_state["call_firma_next"] = next(
                        (c for c in cockpit_order[idx + 1:] if _priority(c) != 5), None)
                    st.rerun()
                except Exception as e:                        # noqa: BLE001
                    st.error(f"Speichern fehlgeschlagen: {e}")
        else:
            st.info("Anruf-Erfassung inaktiv — Migration 0006 (outreach_calls) fehlt.")

        # --- Disposition + E-Mail-Nachfass für die gewählte Firma ---
        dc = st.columns(2)
        with dc[0]:
            st.markdown("**Disposition**")
            lrow = outreach.get(sel_cid)
            if not lrow:
                st.caption("Keine Brief-Zeile — erst Backfill / `c5 --wave` laufen lassen.")
            else:
                cur = lrow.get("status")
                last = (calls.get(sel_cid) or [None])[0]
                sugg = OUTCOME_TO_DISPO.get(last["outcome"]) if last else None
                if sugg and cur not in ("won", "rejected"):
                    st.caption(f"Vorschlag aus letztem Ausgang: {DISPO_LABEL[sugg]}")
                default = cur if cur in DISPO_STATES else (sugg or "sent")
                new_dispo = st.selectbox(
                    "Status", DISPO_STATES, index=DISPO_STATES.index(default),
                    format_func=lambda s: DISPO_LABEL[s], key=f"dispo_{sel_cid}")
                if st.button("Disposition speichern", key=f"disposave_{sel_cid}"):
                    upd = {"status": new_dispo}
                    if new_dispo in ("won", "rejected", "no_response"):
                        upd["response_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        cl.table("outreach").update(upd).eq("id", lrow["id"]).execute()
                        st.toast(f"Disposition: {DISPO_LABEL[new_dispo]}")
                        st.rerun()
                    except Exception as e:                    # noqa: BLE001
                        st.error(f"Update fehlgeschlagen: {e}")
        with dc[1]:
            st.markdown("**E-Mail-Nachfass**")
            erow = emails.get(sel_cid)
            if erow and erow.get("status") == "sent":
                st.caption(f"verschickt am {_de_date(erow.get('sent_at'))}")
            else:
                if ct.get("email"):
                    st.markdown(f"[E-Mail verfassen (Absender {CALVORAN_SENDER})]"
                                f"({_gmail_compose(ct['email'])})")
                else:
                    st.caption("keine E-Mail-Adresse hinterlegt")
                if st.button("Als verschickt markieren", key=f"mailsent_{sel_cid}",
                             disabled=not ct.get("email")):
                    ts = date.today().isoformat()
                    try:
                        if erow:
                            (cl.table("outreach").update({"status": "sent", "sent_at": ts})
                             .eq("id", erow["id"]).execute())
                        else:
                            cl.table("outreach").insert({
                                "company_id": sel_cid, "channel": "email",
                                "status": "sent", "sent_at": ts, "wave": int(wave)}).execute()
                        st.toast("E-Mail-Nachfass erfasst.")
                        st.rerun()
                    except Exception as e:                    # noqa: BLE001
                        st.error(f"Speichern fehlgeschlagen: {e}")

        hist = calls.get(sel_cid, [])
        if hist:
            st.markdown(f"**Historie {sel_name}** ({len(hist)})")
            for c in hist:
                if c.get("outcome") == "termin" and c.get("follow_up_at"):
                    extra = f" · Termin {_de_dt(c['follow_up_at'])}"
                elif c.get("follow_up_at"):
                    extra = f" · Wiedervorlage {_de_date(c['follow_up_at'])}"
                else:
                    extra = ""
                note = f" — {c['notes']}" if c.get("notes") else ""
                st.markdown(f"- {_de_date(c.get('called_at'))} · "
                            f"**{OUTCOMES.get(c['outcome'], c['outcome'])}**{extra}{note}")

# =========================== JOB-SIGNALE =========================== #
# BA-Stellenanzeigen (c6_jobsignale.py) als Nachfolge-Indikator: Zielfirma sucht
# GF / kaufmännische Leitung / zweite Ebene. Sichtung pflegt job_matches.status
# (neu -> gesichtet/relevant/irrelevant); 'relevant' ist der Outreach-Vorrat für Phase B.

JOB_STATI = ["neu", "gesichtet", "relevant", "irrelevant", "outreach"]
_JS_PRIO_ORD = {"hoch": 0, "unbekannt": 1, "mittel": 2, "niedrig": 3}
_JS_STUFE_ORD = {"exakt": 0, "fuzzy": 1, "region": 2, "fuzzy_grenzfall": 3}


@st.cache_data(ttl=120, show_spinner="Lade Job-Signale …")
def load_job_signale():
    """job_matches ⨝ job_postings ⨝ companies — None, wenn Migration 0007 fehlt.

    Firmen-Stammdaten on-demand nur für die gematchten company_ids (wie load_contacts),
    nicht für alle ~70k Firmen."""
    cl2 = get_client()
    try:
        matches = _fetch_all(cl2, "job_matches",
                             "id,posting_id,company_id,match_stufe,match_score,"
                             "prio,status,status_notiz")
        postings = _fetch_all(cl2, "job_postings",
                              "id,refnr,titel,beruf,arbeitgeber,plz,ort,keyword,"
                              "veroeffentlicht_am,letzte_sichtung")
    except Exception:                                          # noqa: BLE001
        return None
    p_by_id = {p["id"]: p for p in postings}
    firma: dict = {}
    cids = sorted({m["company_id"] for m in matches})
    for i in range(0, len(cids), 50):
        for c in (cl2.table("companies").select("id,name,plz,gf_alter")
                  .in_("id", cids[i:i + 50]).execute().data):
            firma[c["id"]] = c
    return matches, p_by_id, firma


@st.cache_data(ttl=600)
def load_welle1_ids() -> set:
    """Alle jemals kuratierten company_ids (Kontext-Flag, wellenübergreifend)."""
    try:
        return {json.loads(ln)["company_id"]
                for ln in SELECTION_FILE.read_text(encoding="utf-8").splitlines()
                if ln.strip()}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


with tab_jobs:
    _js_data = load_job_signale()
    if _js_data is None:
        st.warning("Job-Signale inaktiv — Migration 0007 (job_postings/job_matches) noch "
                   "nicht im Supabase-Studio angewandt.")
    else:
        js_matches, js_postings, js_firma = _js_data
        if not js_matches:
            st.info("Noch keine Matches — einmalig "
                    "`pipeline/c6_jobsignale.py --backfill 100` laufen lassen.")
        else:
            w1 = load_welle1_ids()
            n_neu = sum(1 for m in js_matches if m["status"] == "neu")
            n_grenz = sum(1 for m in js_matches
                          if m["status"] == "neu" and m["match_stufe"] == "fuzzy_grenzfall")
            n_rel = sum(1 for m in js_matches if m["status"] == "relevant")
            jm = st.columns(5)
            jm[0].metric("Anzeigen im Bestand", len(js_postings))
            jm[1].metric("Matches", len(js_matches))
            jm[2].metric("neu (Review-Queue)", n_neu)
            jm[3].metric("davon Grenzfälle", n_grenz)
            jm[4].metric("relevant (Vorrat)", n_rel)

            st.session_state.setdefault("js_status", ["neu"])
            st.session_state.setdefault("js_prio", list(_JS_PRIO_ORD))

            def _js_filter_entfernen():
                st.session_state.update({"js_status": JOB_STATI,
                                         "js_prio": list(_JS_PRIO_ORD)})

            jf = st.columns([2, 2, 1.6, 0.8])
            js_status_sel = jf[0].multiselect("Status", JOB_STATI, key="js_status")
            js_prio_sel = jf[1].multiselect("Priorität", list(_JS_PRIO_ORD), key="js_prio")
            js_suche = jf[2].text_input("Suche (Firma/Arbeitgeber/Titel/Ort)", key="js_suche")
            with jf[3]:
                st.markdown("<div style='padding-top:1.8rem'></div>", unsafe_allow_html=True)
                st.button("Filter entfernen", key="js_reset", on_click=_js_filter_entfernen,
                          help="Alle Status/Prioritäten anzeigen — Suchtext bleibt erhalten.")

            def _js_sort(m):
                return (JOB_STATI.index(m["status"]), _JS_PRIO_ORD[m["prio"]],
                        _JS_STUFE_ORD[m["match_stufe"]], -(m["match_score"] or 0))

            js_rows, js_orig = [], {}
            for m in sorted(js_matches, key=_js_sort):
                if m["status"] not in js_status_sel or m["prio"] not in js_prio_sel:
                    continue
                p = js_postings.get(m["posting_id"]) or {}
                c = js_firma.get(m["company_id"]) or {}
                if js_suche.strip():
                    # Jedes Suchwort muss irgendwo treffen — »IGK Meschede« findet so die
                    # Firma über Name UND Ort, obwohl beides in verschiedenen Feldern steht.
                    haystack = " ".join(str(v or "") for v in (
                        c.get("name"), p.get("arbeitgeber"), p.get("titel"),
                        p.get("ort"), c.get("plz"), p.get("plz"))).lower()
                    if not all(w in haystack for w in js_suche.lower().split()):
                        continue
                js_orig[m["id"]] = m
                js_rows.append({
                    "_id": m["id"],
                    "Status": m["status"],
                    "Notiz": m.get("status_notiz") or "",
                    "Anzeige": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{p.get('refnr')}",
                    "GF-Alter": c.get("gf_alter"),
                    "Firma": c.get("name"),
                    "Firmen-PLZ": c.get("plz"),
                    "BA-Arbeitgeber": p.get("arbeitgeber"),
                    "Stellentitel": p.get("titel"),
                    "BA-Beruf": p.get("beruf"),
                    "Anzeigen-Ort": f"{p.get('plz') or ''} {p.get('ort') or ''}".strip(),
                    "veröffentlicht": _de_date(p.get("veroeffentlicht_am")),
                    "zuletzt gesehen": _de_date(p.get("letzte_sichtung")),
                    "Stufe": m["match_stufe"],
                    "Score": m["match_score"],
                    "Welle 1": m["company_id"] in w1,
                })
            if not js_rows:
                st.info("Kein Match im Filter.")
            else:
                st.caption("Status/Notiz direkt in der Tabelle setzen, dann speichern. "
                           "Grenzfälle: BA-Arbeitgeber gegen Firma prüfen (Anzeige öffnen).")
                js_df = pd.DataFrame(js_rows).set_index("_id")
                js_edited = st.data_editor(
                    js_df, hide_index=True, width="stretch",
                    height=min(520, 60 + 35 * len(js_rows)),
                    key="js_editor",
                    disabled=[col for col in js_df.columns if col not in ("Status", "Notiz")],
                    column_config={
                        "Status": st.column_config.SelectboxColumn(
                            "Status", options=JOB_STATI, required=True, width="small"),
                        "Notiz": st.column_config.TextColumn("Notiz", width="medium"),
                        "Anzeige": st.column_config.LinkColumn(
                            "Anzeige", display_text="Link", width="small"),
                        "GF-Alter": st.column_config.NumberColumn("GF-Alter", width="small"),
                        "Score": st.column_config.NumberColumn(
                            "Score", format="%.0f", width="small"),
                        "Welle 1": st.column_config.CheckboxColumn(
                            "W1", help="war in der Welle-1-Kuratierung", width="small"),
                    })
                if st.button("Änderungen speichern", type="primary", key="js_save"):
                    cl_js = get_client()
                    n_upd = 0
                    try:
                        for mid in js_edited.index:
                            neu_status = js_edited.at[mid, "Status"]
                            neu_notiz = (js_edited.at[mid, "Notiz"] or "").strip()
                            orig = js_orig[mid]
                            if (neu_status != orig["status"]
                                    or neu_notiz != (orig.get("status_notiz") or "")):
                                upd = {"status": neu_status,
                                       "status_notiz": neu_notiz or None}
                                if neu_status != "neu":
                                    upd["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                                (cl_js.table("job_matches").update(upd)
                                 .eq("id", mid).execute())
                                n_upd += 1
                        if n_upd:
                            load_job_signale.clear()
                            st.toast(f"{n_upd} Matches aktualisiert.")
                            st.rerun()
                        else:
                            st.toast("Keine Änderungen.")
                    except Exception as e:                     # noqa: BLE001
                        st.error(f"Speichern fehlgeschlagen: {e}")

st.caption(
    f"Auswahl: {SELECTION_FILE}  ·  c5_export liest selected==true (decision==lead) je Welle.  "
    f"Default Welle 1: Köln-Bonn, BWL-fern/mittel, ohne Berater, GF-Alter ≥ 58."
)
