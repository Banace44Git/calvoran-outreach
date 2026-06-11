"""Deterministischer Website-Modernitäts-Score (0-10) aus Crawl-Signalen.

Kein LLM. Reproduzierbar: gleiche tech_signals + gleiche modernity.yaml -> gleicher
Score. Firmen ohne erreichbare Website ergeben None (nicht 0).

Erwartete tech_signals (vom Crawler je Firma aggregiert):
  reachable: bool
  scheme: 'https' | 'http'
  http_to_https_redirect: bool
  http_version: 'HTTP/2' | 'HTTP/1.1' | ...
  headers: dict (Response-Header der Startseite)
  home_html: str (Start-HTML, lowercased, gekappt) -- für Fingerprints
  generator: str | None (meta generator)
  viewport: bool
  copyright_year: int | None
  last_modified_year: int | None
"""

from __future__ import annotations

from typing import Optional, Tuple


def _has_any(text: str, needles) -> bool:
    return any(str(n).lower() in text for n in needles)


def compute(tech_signals: dict, cfg: dict, *, now_year: int) -> Tuple[Optional[int], dict]:
    version = cfg.get("version")
    if not tech_signals or not tech_signals.get("reachable"):
        return None, {"version": version, "reason": "keine_website_oder_nicht_erreichbar"}

    k = cfg["komponenten"]
    fp = cfg["fingerprints"]
    headers = {str(kk).lower(): str(vv).lower() for kk, vv in (tech_signals.get("headers") or {}).items()}
    header_blob = " ".join(f"{kk}: {vv}" for kk, vv in headers.items())
    html = (tech_signals.get("home_html") or "").lower()
    generator = (tech_signals.get("generator") or "").lower()
    stack_blob = generator + " " + html

    bd: dict = {"version": version, "komponenten": {}, "evidenz": []}

    # 1) Transport / Sicherheit (max 3)
    ts = 0.0
    if tech_signals.get("scheme") == "https":
        ts += k["transport_sicherheit"]["https_mit_redirect"]
        bd["evidenz"].append("https+redirect" if tech_signals.get("http_to_https_redirect") else "https")
    else:
        bd["evidenz"].append("nur_http")
    if "strict-transport-security" in headers:
        ts += k["transport_sicherheit"]["hsts"]
        bd["evidenz"].append("hsts")
    hv = str(tech_signals.get("http_version") or "").upper()
    if "2" in hv or "3" in hv:
        ts += k["transport_sicherheit"]["http2_oder_3"]
        bd["evidenz"].append(f"http:{hv}")
    bd["komponenten"]["transport_sicherheit"] = round(ts, 2)

    # 2) Stack-Aktualität (max 3)
    st = 0.0
    if _has_any(stack_blob, fp["framework_veraltet"]):
        st += k["stack_aktualitaet"]["framework_veraltet"]
        bd["evidenz"].append("stack_veraltet")
    elif _has_any(stack_blob, fp["framework_modern"]):
        st += k["stack_aktualitaet"]["framework_modern"]
        bd["evidenz"].append("stack_modern")
    else:
        st += k["stack_aktualitaet"]["framework_unbekannt"]
        bd["evidenz"].append("stack_unbekannt")
    if _has_any(header_blob, fp["cdn_header_keys"]) or "content-security-policy" in headers:
        st += k["stack_aktualitaet"]["cdn_oder_security_header"]
        bd["evidenz"].append("cdn_oder_csp")
    bd["komponenten"]["stack_aktualitaet"] = round(st, 2)

    # 3) Mobile / Responsive (max 1)
    mob = k["mobile_responsive"]["viewport_responsive"] if tech_signals.get("viewport") else 0.0
    if mob:
        bd["evidenz"].append("viewport")
    bd["komponenten"]["mobile_responsive"] = round(mob, 2)

    # 4) Rich Media / Interaktivität (max 2)
    rm = 0.0
    if _has_any(html, fp["video_markers"]):
        rm += k["rich_media_interaktiv"]["video"]
        bd["evidenz"].append("video")
    if _has_any(html, fp["interaktiv_markers"]):
        rm += k["rich_media_interaktiv"]["interaktiv"]
        bd["evidenz"].append("interaktiv")
    bd["komponenten"]["rich_media_interaktiv"] = round(rm, 2)

    # 5) Aktualität / Pflege (max 1)
    ap = 0.0
    cy = tech_signals.get("copyright_year")
    lm = tech_signals.get("last_modified_year")
    veraltet_ab = int(k["aktualitaet_pflege"]["veraltet_ab_jahre"])
    newest = max([y for y in (cy, lm) if y], default=None)
    recent = bool(newest and newest >= now_year - 1)
    old = bool(newest and newest <= now_year - veraltet_ab)
    if recent and not old:
        ap += k["aktualitaet_pflege"]["aktuell"]
        bd["evidenz"].append("aktuell")
    elif old:
        bd["evidenz"].append("veraltet")
    bd["komponenten"]["aktualitaet_pflege"] = round(ap, 2)

    raw = max(0.0, min(10.0, ts + st + mob + rm + ap))
    bd["score_raw"] = round(raw, 2)
    return int(round(raw)), bd
