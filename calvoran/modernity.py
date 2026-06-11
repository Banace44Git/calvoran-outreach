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

import re
from typing import Optional, Tuple

# Versions werden ausschließlich aus dem generator-Meta gelesen (zuverlässig),
# nicht aus dem HTML — sonst matchen Copyright-Jahre ("1998-2026 ... TYPO3").
_WP_RE = re.compile(r"wordpress[ /]*(\d+)", re.I)
_TYPO3_RE = re.compile(r"typo3[^0-9]{0,8}(\d+)", re.I)
_JOOMLA_RE = re.compile(r"joomla!?[^0-9]{0,8}(\d+)", re.I)


def _has_any(text: str, needles) -> bool:
    return any(str(n).lower() in text for n in needles)


def _cms_tier(stack_blob: str, generator: str, fp: dict):
    """Versions-bewusste CMS-Einstufung. generator-Tag hat Vorrang vor den losen
    HTML-Fingerprints. Rückgabe (tier, label) oder (None, None), wenn kein CMS
    eindeutig erkannt wurde (dann greifen die Fingerprint-Listen)."""
    gen = (generator or "").lower()
    # Parking-/Platzhalterseiten: kein produktiver Stack -> wie veraltet (0.0).
    if _has_any(stack_blob, fp.get("parking_markers", [])):
        return "veraltet", "geparkt"
    # WordPress: Major-Cutoff 6.0 (Mai 2022). 5.x und älter = veraltet.
    m = _WP_RE.search(gen)
    if m:
        major = int(m.group(1))
        return ("modern", f"wordpress_{major}") if major >= 6 else ("veraltet", f"wordpress_{major}_alt")
    if "wordpress" in gen or "wp-content" in stack_blob or "/wp-json" in stack_blob:
        return "unbekannt", "wordpress_ohne_version"
    # TYPO3: >=11 modern, <=10 veraltet; ohne Version unbekannt.
    m = _TYPO3_RE.search(gen)
    if m:
        v = int(m.group(1))
        return ("modern", f"typo3_{v}") if v >= 11 else ("veraltet", f"typo3_{v}_alt")
    if "typo3" in gen:
        return "unbekannt", "typo3_ohne_version"
    # Joomla: >=4 modern, <=3 veraltet (Joomla 3 EOL 08/2023); ohne Version unbekannt.
    m = _JOOMLA_RE.search(gen)
    if m:
        v = int(m.group(1))
        return ("modern", f"joomla_{v}") if v >= 4 else ("veraltet", f"joomla_{v}_alt")
    if "joomla" in gen:
        return "unbekannt", "joomla_ohne_version"
    # Evergreen-SaaS-Baukästen sind per Definition aktuell.
    if _has_any(stack_blob, fp.get("framework_evergreen", [])):
        return "modern", "evergreen_saas"
    return None, None


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
    tls_defekt = bool(tech_signals.get("tls_insecure"))
    if tech_signals.get("scheme") == "https" and not tls_defekt:
        ts += k["transport_sicherheit"]["https_mit_redirect"]
        bd["evidenz"].append("https+redirect" if tech_signals.get("http_to_https_redirect") else "https")
    elif tls_defekt:
        # defektes/abgelaufenes Zertifikat: kein Transport-Bonus, dafür Lead-Signal.
        bd["evidenz"].append("tls_defekt")
    else:
        bd["evidenz"].append("nur_http")
    if "strict-transport-security" in headers:
        ts += k["transport_sicherheit"]["hsts"]
        bd["evidenz"].append("hsts")
    hv = str(tech_signals.get("http_version") or "").upper()
    if "2" in hv or "3" in hv:
        ts += k["transport_sicherheit"]["http2_oder_3"]
        bd["evidenz"].append(f"http_version:{hv}")
    bd["komponenten"]["transport_sicherheit"] = round(ts, 2)

    # 2) Stack-Aktualität (max 3) — generator-basierte CMS-Erkennung zuerst,
    #    danach die losen HTML-Fingerprints als Fallback.
    st = 0.0
    tier, tlabel = _cms_tier(stack_blob, generator, fp)
    if tier is None:
        if _has_any(stack_blob, fp["framework_veraltet"]):
            tier, tlabel = "veraltet", "stack_veraltet"
        elif _has_any(stack_blob, fp["framework_modern"]):
            tier, tlabel = "modern", "stack_modern"
        else:
            tier, tlabel = "unbekannt", "stack_unbekannt"
    _tier_key = {"modern": "framework_modern", "veraltet": "framework_veraltet",
                 "unbekannt": "framework_unbekannt"}[tier]
    st += k["stack_aktualitaet"][_tier_key]
    bd["evidenz"].append(tlabel)
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
