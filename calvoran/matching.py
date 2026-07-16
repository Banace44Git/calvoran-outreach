"""Firmennamen-Matching: BA-Arbeitgeber gegen calvoran.companies (Name + PLZ).

Erweitert die Normalisierung aus pipeline/c5_brief_merge.py:_norm() um Rechtsform-
Phrasen (»GmbH & Co. KG« als Einheit, eG, gGmbH, AöR, e.K. …). c5/c1b bleiben
unangetastet — deren Join-Keys dürfen sich nicht ändern.

Stufen (Präzision vor Recall, kein Nur-Name-Match bundesweit bei 70k Firmen):
  exakt            norm_firma gleich, gleiche PLZ
  fuzzy            token_set_ratio >= fuzzy_auto, gleiche PLZ
  fuzzy_grenzfall  fuzzy_review <= Score < fuzzy_auto, gleiche PLZ (Review-Queue)
  region           PLZ-Präfix gleich (Betriebsstätte != HR-Sitz), Name exakt oder >= 95

Teilmengen-Regel: Sind die Namens-Tokens des einen eine ECHTE Teilmenge des anderen
(»e on« vs »e on one«), gibt token_set_ratio 100 — bei Konzernen matcht so eine
einzige Mutter-Anzeige jede Tochter. Teilmengen sind deshalb nie Auto-Match:
gleiche PLZ -> fuzzy_grenzfall (Review, z.B. Baldus Medical vs Baldus Medical
Engineering), Region-Stufe -> gar kein Match (E.ON SE vs E.ON One GmbH).

Ort-Token-Regel: Vor dem Fuzzy-Scoring werden die Tokens des Anzeigen-Orts aus
beiden Namen gestrippt. Das PLZ-Blocking vergleicht ohnehin nur Firmen am selben
Ort — ein Städtename im Firmennamen ist dort null Information, bläht aber den
token_set_ratio auf (»Roller … Gelsenkirchen« vs »Matena Gelsenkirchen« = 78,8,
obwohl nur die Stadt gemeinsam ist). Wird ein Name durchs Strippen leer, gilt
für ihn der volle Name; der Exakt-Vergleich läuft immer über die vollen Namen.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from rapidfuzz import fuzz

# Längste Phrasen zuerst — »und co kg« muss vor »kg« fallen, sonst bleibt »und« stehen.
_LEGAL_PHRASES = [
    "und co kg", "u co kg", "co kg", "und co kgaa", "co kgaa", "co ohg",
    "haftungsbeschrankt", "ggmbh", "gmbh", "mbh", "kgaa", "ohg", "gbr",
    "aor", "e kfm", "e kfr", "e k", "ek", "eg", "kg", "ag", "ug", "se",
    "inh", "co",
]
_REGION_MIN_SCORE = 95


def norm_text(s: str | None) -> str:
    """NFKD→ASCII, lower, Interpunktion raus — ohne Rechtsform-Stripping (für Titel)."""
    s = unicodedata.normalize("NFKD", (s or "").lower()).replace("ß", "ss")
    s = s.encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def norm_firma(s: str | None) -> str:
    """Wie norm_text, zusätzlich Rechtsform-Phrasen strippen (für Firmennamen)."""
    s = f" {norm_text(s)} "
    for phrase in _LEGAL_PHRASES:
        s = s.replace(f" {phrase} ", " ")
    return s.strip()


def _token_teilmenge(a: str, b: str) -> bool:
    """True, wenn die Tokens des einen eine echte Teilmenge des anderen sind."""
    ta, tb = set(a.split()), set(b.split())
    return ta != tb and (ta <= tb or tb <= ta)


def mehrfach_key(arbeitgeber: str | None, titel: str | None) -> tuple[str, str]:
    """Mehrfach-Anzeigen-Anker: Re-Posts unter neuer refnr und Mehrstädte-Ketten
    desselben Gesuchs (gleicher Arbeitgeber, gleicher Titel) zählen als EIN Signal.
    Gemeinsamer Schlüssel für c6-Match-Dedup und Dashboard-Gruppierung."""
    return norm_firma(arbeitgeber), norm_text(titel)


def prio_from_alter(gf_alter) -> str:
    """companies.gf_alter -> Prio. NULL ist eigene Klasse (»Alter unbekannt != jung«)."""
    if gf_alter is None:
        return "unbekannt"
    if gf_alter >= 58:
        return "hoch"
    if gf_alter >= 50:
        return "mittel"
    return "niedrig"


_STUFEN_RANG = {"exakt": 0, "fuzzy": 1, "region": 2, "fuzzy_grenzfall": 3}


class CompanyIndex:
    """In-Memory-Index über companies mit PLZ-Blocking (voll + Präfix)."""

    def __init__(self, rows: list[dict], *, plz_praefix_stellen: int = 3) -> None:
        self.praefix = plz_praefix_stellen
        self.by_plz: dict[str, list] = defaultdict(list)
        self.by_praefix: dict[str, list] = defaultdict(list)
        for r in rows:
            n = norm_firma(r.get("name"))
            plz = (r.get("plz") or "").strip()
            if not n or not plz:
                continue
            eintrag = (n, r["id"], r.get("gf_alter"))
            self.by_plz[plz].append(eintrag)
            self.by_praefix[plz[: self.praefix]].append(eintrag)

    def match_posting(self, arbeitgeber: str, lokationen: list[tuple[str | None, str | None]],
                      *, fuzzy_auto: int = 90, fuzzy_review: int = 75) -> list[dict]:
        """Beste Match-Stufe je Firma über alle Standorte einer Anzeige."""
        n_ag = norm_firma(arbeitgeber)
        if not n_ag:
            return []
        best: dict[str, dict] = {}  # company_id -> Match

        def consider(company_id, stufe, score, gf_alter):
            prev = best.get(company_id)
            kand = {"company_id": company_id, "match_stufe": stufe,
                    "match_score": round(score, 1), "gf_alter": gf_alter}
            if prev is None or (_STUFEN_RANG[stufe], -score) < (
                    _STUFEN_RANG[prev["match_stufe"]], -prev["match_score"]):
                best[company_id] = kand

        gesehen_plz, gesehen_praefix = set(), set()
        for plz, ort in lokationen:
            plz = (plz or "").strip()
            if not plz or plz in gesehen_plz:
                continue
            gesehen_plz.add(plz)
            ort_tokens = set(norm_text(ort).split())

            def ohne_ort(name: str) -> str:
                rest = " ".join(t for t in name.split() if t not in ort_tokens)
                return rest or name

            n_ag_o = ohne_ort(n_ag)
            for n, cid, alter in self.by_plz.get(plz, ()):
                if n == n_ag:
                    consider(cid, "exakt", 100.0, alter)
                    continue
                n_o = ohne_ort(n)
                score = fuzz.token_set_ratio(n_ag_o, n_o)
                if score < fuzzy_review:
                    continue
                if score >= fuzzy_auto and not _token_teilmenge(n_ag_o, n_o):
                    consider(cid, "fuzzy", score, alter)
                else:
                    consider(cid, "fuzzy_grenzfall", score, alter)
            praefix = plz[: self.praefix]
            if praefix in gesehen_praefix:
                continue
            gesehen_praefix.add(praefix)
            for n, cid, alter in self.by_praefix.get(praefix, ()):
                if cid in best and _STUFEN_RANG[best[cid]["match_stufe"]] < _STUFEN_RANG["region"]:
                    continue
                if n == n_ag:
                    consider(cid, "region", 100.0, alter)
                else:
                    n_o = ohne_ort(n)
                    score = fuzz.token_set_ratio(n_ag_o, n_o)
                    if score >= _REGION_MIN_SCORE and not _token_teilmenge(n_ag_o, n_o):
                        consider(cid, "region", score, alter)
        return list(best.values())
