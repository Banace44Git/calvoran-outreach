"""Client für die BA-Jobsuche-API (rest.arbeitsagentur.de).

Keine offizielle API: Endpunkte und der statische Key sind community-dokumentiert
(github.com/bundesAPI/jobsuche-api). Der Key kann sich ändern — bei 401/403 scheitert
der Client deshalb laut (RuntimeError) statt leer durchzulaufen. Rate-Limits sind
unbekannt: konservative Drossel zwischen Requests, Backoff bei 429/5xx.

Die API kennt keinen PLZ-/Bundesland-Filter; gescannt wird bundesweit je Keyword
(`was`), der Firmen-Match passiert lokal (calvoran/matching.py).
"""

from __future__ import annotations

import base64
import time

import httpx

BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
SEARCH_PATH = "/pc/v6/jobs"
DETAIL_PATH = "/pc/v4/jobdetails/{refnr_b64}"
API_KEY = "jobboerse-jobsuche"  # statischer Community-Key, kein Account
ANZEIGE_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

# v6 akzeptiert für `veroeffentlichtseit` NUR diese Werte (die UI-Optionen gestern /
# 7 Tage / 14 Tage / 4 Wochen). Alle anderen — auch die community-dokumentierten 0-100 —
# werden STILL ignoriert und liefern den ungefilterten Gesamtbestand (empirisch 2026-07-08).
# Maximale Rückschau ist damit 28 Tage.
VALID_VEROEFFENTLICHTSEIT = (1, 7, 14, 28)


def snap_veroeffentlichtseit(tage: int) -> int:
    """Nächstgrößerer gültiger Wert (Überlappung ist unschädlich, Dedup über refnr)."""
    for v in VALID_VEROEFFENTLICHTSEIT:
        if tage <= v:
            return v
    return VALID_VEROEFFENTLICHTSEIT[-1]

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


class BaJobsucheClient:
    def __init__(self, *, drossel_sekunden: float = 1.0, timeout: float = 30.0) -> None:
        self.drossel = drossel_sekunden
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": API_KEY, "User-Agent": "jobsuche/2.5.4"},
            timeout=timeout,
        )
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BaJobsucheClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        wait = self.drossel - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(_MAX_RETRIES + 1):
            resp = self._client.get(path, params=params)
            self._last_request = time.monotonic()
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"BA-API-Auth fehlgeschlagen (HTTP {resp.status_code}) — der statische "
                    f"Community-Key '{API_KEY}' ist vermutlich rotiert worden. "
                    "Aktuellen Key unter github.com/bundesAPI/jobsuche-api prüfen."
                )
            if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)  # 1,2,4,8 s
                continue
            resp.raise_for_status()
            return resp
        raise AssertionError("unreachable")

    def search_page(self, was: str, *, veroeffentlichtseit: int, size: int, page: int,
                    zeitarbeit: bool = False, pav: bool = False) -> dict:
        """Eine Ergebnisseite. `page` beginnt bei 1."""
        params = {
            "was": was,
            "veroeffentlichtseit": snap_veroeffentlichtseit(int(veroeffentlichtseit)),
            "size": size,
            "page": page,
            "angebotsart": 1,                     # Arbeitsstellen (keine Ausbildung/Praktika)
            "zeitarbeit": str(zeitarbeit).lower(),
            "pav": str(pav).lower(),
        }
        return self._get(SEARCH_PATH, params).json()

    def search_all(self, was: str, *, veroeffentlichtseit: int, size: int = 100,
                   max_pages: int = 50, zeitarbeit: bool = False, pav: bool = False):
        """Generator über alle Anzeigen eines Keywords (paginiert bis leer/max_pages)."""
        for page in range(1, max_pages + 1):
            data = self.search_page(
                was, veroeffentlichtseit=veroeffentlichtseit, size=size, page=page,
                zeitarbeit=zeitarbeit, pav=pav)
            angebote = data.get("ergebnisliste") or []
            yield from angebote
            if len(angebote) < size:
                return

    def jobdetails(self, refnr: str) -> dict:
        """Detail-Abruf (Volltext etc.) — Phase B; refnr wird base64-kodiert."""
        refnr_b64 = base64.b64encode(refnr.encode()).decode()
        return self._get(DETAIL_PATH.format(refnr_b64=refnr_b64)).json()


def lokationen(item: dict) -> list[tuple[str | None, str | None]]:
    """Alle (plz, ort)-Paare einer Anzeige — Anzeigen können mehrere Standorte haben."""
    out = []
    for lok in item.get("stellenlokationen") or []:
        adr = lok.get("adresse") or {}
        if adr.get("plz") or adr.get("ort"):
            out.append((adr.get("plz"), adr.get("ort")))
    return out


def parse_posting(item: dict, keyword: str) -> dict | None:
    """Mappt ein v6-Angebot auf eine job_postings-Zeile; None wenn Referenznummer fehlt.

    Erste Lokation landet in den getypten Spalten plz/ort; alle weiteren stehen im raw
    (`stellenlokationen`) und werden beim Matching mit berücksichtigt.
    """
    refnr = item.get("referenznummer") or item.get("refnr")
    if not refnr:
        return None
    loks = lokationen(item)
    plz, ort = loks[0] if loks else (None, None)
    veroeffentlicht = (item.get("datumErsteVeroeffentlichung") or "")[:10] or None
    return {
        "refnr": refnr,
        "titel": item.get("stellenangebotsTitel") or item.get("hauptberuf") or "(ohne Titel)",
        "beruf": item.get("hauptberuf"),
        "arbeitgeber": item.get("firma") or "(unbekannt)",
        "plz": plz,
        "ort": ort,
        "keyword": keyword,
        "veroeffentlicht_am": veroeffentlicht,
        "raw": item,
    }
