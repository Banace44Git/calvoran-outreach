"""Async-Crawler: 6-10 Seiten je Domain über Navigations-Heuristik.

httpx async + trafilatura (Text) + selectolax (Linkauswahl). Respektiert robots.txt,
1 Request/Sekunde/Domain. Sammelt nebenbei die Modernitäts-Signale (Protokoll,
Header, Generator, Viewport, Video/Interaktiv, Copyright-Jahr) ohne Zusatz-Requests.

Kein Headless-Browser hier; Playwright-Fallback ist ein separater zweiter Durchlauf.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from selectolax.parser import HTMLParser

_YEAR_RE = re.compile(r"(20\d{2})")
_COPYRIGHT_RE = re.compile(r"(?:©|&copy;|copyright)\s*[^0-9]{0,12}(20\d{2})", re.I)


def normalize_host(website: str) -> str:
    w = (website or "").strip()
    if not w:
        return ""
    if "//" not in w:
        w = "http://" + w
    host = urlparse(w).netloc or urlparse(w).path
    return host.strip().strip("/").lower()


def _registrable(host: str) -> str:
    h = host[4:] if host.startswith("www.") else host
    return h


def _same_site(url: str, base_host: str) -> bool:
    try:
        h = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return _registrable(h) == _registrable(base_host) if h else True


def _year_from_http_date(value: str):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(value, fmt).year
        except ValueError:
            continue
    m = _YEAR_RE.search(value or "")
    return int(m.group(1)) if m else None


def _copyright_year(html: str):
    m = _COPYRIGHT_RE.search(html)
    if m:
        return int(m.group(1))
    tail = html[-3000:]
    years = [int(y) for y in _YEAR_RE.findall(tail)]
    cur = datetime.now(timezone.utc).year
    years = [y for y in years if 2000 <= y <= cur + 1]
    return max(years) if years else None


def _classify_links(home_html: str, base_url: str, base_host: str, page_types: dict):
    """Liefert {page_type: url} für den jeweils ersten passenden Nav-Link."""
    tree = HTMLParser(home_html)
    found: dict = {}
    for a in tree.css("a"):
        href = a.attributes.get("href")
        if not href:
            continue
        try:
            url = urldefrag(urljoin(base_url, href))[0]
        except ValueError:
            continue  # kaputter href (z.B. "Invalid IPv6 URL") darf den Crawl nicht killen
        if not url.startswith("http") or not _same_site(url, base_host):
            continue
        text = (a.text() or "").strip().lower()
        path = urlparse(url).path.lower()
        hay = text + " " + path
        for ptype, keywords in page_types.items():
            if ptype == "home" or ptype in found:
                continue
            if any(kw.lower() in hay for kw in keywords):
                found[ptype] = url
                break
    return found


async def _fetch(client: httpx.AsyncClient, url: str, max_bytes: int):
    try:
        r = await client.get(url)
    except (httpx.HTTPError, UnicodeError) as e:
        return None, f"{type(e).__name__}: {e}"  # volle Meldung für TLS-/Protokoll-Klassifikation
    content = r.text if len(r.content) <= max_bytes else r.content[:max_bytes].decode(r.encoding or "utf-8", "ignore")
    return r, content


async def _load_robots(client: httpx.AsyncClient, scheme: str, host: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse([])  # default: alles erlaubt
    try:
        r = await client.get(f"{scheme}://{host}/robots.txt")
        if r.status_code == 200 and r.text:
            rp.parse(r.text.splitlines())
    except httpx.HTTPError:
        pass
    return rp


def _is_tls_error(s: str) -> bool:
    sl = (s or "").lower()
    return any(x in sl for x in ("certificate_verify", "dh_key_too_small",
                                 "certificate", "ssl", "tlsv1"))


def _is_http2_error(s: str) -> bool:
    sl = (s or "").lower()
    return "streamreset" in sl or "remoteprotocol" in sl or "protocolerror" in sl


def _conn_error_label(s: str) -> str:
    """Grobe Kategorie aus einer httpx-Fehlermeldung fürs diagnostische error-Feld."""
    sl = (s or "").lower()
    if "certificate_verify" in sl or "certificate" in sl:
        return "tls_zertifikat_defekt"
    if "dh_key_too_small" in sl or "ssl" in sl or "tlsv1" in sl:
        return "tls_defekt"
    if _is_http2_error(sl):
        return "http2_protokoll"
    if "timeout" in sl or "timed out" in sl:
        return "timeout"
    if "getaddrinfo" in sl or "name or service" in sl or "nodename" in sl:
        return "dns_unbekannt"
    if "connect" in sl:
        return "connect_fehler"
    return "startseite_nicht_erreichbar"


def _insecure_ssl_context() -> ssl.SSLContext:
    """Permissiver Context für TLS-defekte Zielserver: Verify aus (abgelaufene/
    unvollständige Cert-Chain) UND SECLEVEL runter (zu schwacher DH-Key)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx


def _new_client(http2: bool, secure: bool, timeout, headers) -> httpx.AsyncClient:
    verify = True if secure else _insecure_ssl_context()
    return httpx.AsyncClient(http2=http2, verify=verify, follow_redirects=True,
                             timeout=timeout, headers=headers, max_redirects=5)


async def _fetch_home(client, host, max_bytes):
    """Startseite holen: erst http:// (Redirect-Erkennung), dann https://.
    Rückgabe (resp, html, scheme, redirect_to_https, err_str)."""
    last_err = None
    for candidate in (f"http://{host}", f"https://{host}"):
        resp, body = await _fetch(client, candidate, max_bytes)
        if resp is not None and resp.status_code < 400 and body:
            scheme = urlparse(str(resp.url)).scheme
            r2h = candidate.startswith("http://") and scheme == "https"
            return resp, body, scheme, r2h, None
        if resp is None:
            last_err = body  # _fetch liefert bei Fehler (None, "Typ: meldung")
    return None, None, None, None, last_err


async def crawl_domain(website: str, cfg: dict, *, logger=None) -> dict:
    """Crawlt eine Domain. Rückgabe: {pages: [...], tech_signals: {...}, error: str|None}.

    Verbindungs-Strategie: strikt zuerst (HTTP/2, TLS-Verify an). Bei TLS-Fehler
    (defektes/abgelaufenes Zertifikat, zu schwacher Schlüssel) Retry mit permissivem
    SSL-Context und Markierung tls_insecure -> die Seite ist crawlbar, der TLS-Defekt
    fließt als Modernitäts-Malus und Lead-Signal ein. Bei HTTP/2-Protokollabbruch
    Retry ohne HTTP/2 (http2_disabled).
    """
    limits = cfg["limits"]
    page_types = cfg["page_types"]
    host = normalize_host(website)
    result = {"pages": [], "tech_signals": {"reachable": False}, "error": None}
    if not host:
        result["error"] = "keine_website"
        return result

    ua = limits["user_agent"]
    timeout = httpx.Timeout(limits["timeout_s"])
    headers = {"User-Agent": ua}
    max_bytes = int(limits["max_bytes_per_page"])

    # Strikt, dann gezielt degradiert je nach Fehlertyp.
    degraded: dict = {}
    client = _new_client(True, True, timeout, headers)
    resp, home_html, scheme, redirect_to_https, err = await _fetch_home(client, host, max_bytes)
    if resp is None:
        await client.aclose()
        client = None
        if _is_tls_error(err):
            client = _new_client(True, False, timeout, headers)
            degraded = {"tls_insecure": True}
        elif _is_http2_error(err):
            client = _new_client(False, True, timeout, headers)
            degraded = {"http2_disabled": True}
        if client is not None:
            resp, home_html, scheme, redirect_to_https, err = await _fetch_home(client, host, max_bytes)
            # Deckt der erste Fallback den jeweils anderen Defekt auf: beides aus.
            if resp is None and (_is_tls_error(err) or _is_http2_error(err)):
                await client.aclose()
                client = _new_client(False, False, timeout, headers)
                degraded = {"tls_insecure": True, "http2_disabled": True}
                resp, home_html, scheme, redirect_to_https, err = await _fetch_home(client, host, max_bytes)

    if resp is None:
        if client is not None:
            await client.aclose()
        result["error"] = _conn_error_label(err)
        return result

    try:
        home_resp = resp
        base_url = str(home_resp.url)
        base_host = urlparse(base_url).netloc
        if limits.get("respect_robots", True):
            rp = await _load_robots(client, scheme, base_host)
            if not rp.can_fetch(ua, base_url):
                result["error"] = "robots_disallow_home"
                result["tech_signals"] = {"reachable": False, "reason": "robots"}
                return result
        else:
            rp = None

        # Tech-Signale aus der Startseite.
        tree = HTMLParser(home_html)
        gen_node = tree.css_first("meta[name=generator]")
        generator = gen_node.attributes.get("content") if gen_node else None
        viewport = tree.css_first("meta[name=viewport]") is not None
        lm = home_resp.headers.get("last-modified")
        tech = {
            "reachable": True,
            "scheme": scheme,
            "http_to_https_redirect": redirect_to_https,
            "http_version": home_resp.http_version,
            "headers": dict(home_resp.headers),
            "home_html": home_html[:200_000].lower(),
            "generator": generator,
            "viewport": viewport,
            "copyright_year": _copyright_year(home_html),
            "last_modified_year": _year_from_http_date(lm) if lm else None,
            "final_url": base_url,
            **degraded,
        }
        result["tech_signals"] = tech

        # Seitenauswahl: Home + klassifizierte Nav-Links.
        selected = [("home", base_url)]
        classified = _classify_links(home_html, base_url, base_host, page_types)
        for ptype in cfg.get("extract_priority", list(page_types.keys())):
            if ptype == "home":
                continue
            if ptype in classified and len(selected) < int(limits["pages_per_domain"]):
                selected.append((ptype, classified[ptype]))

        # Startseite als erste Page übernehmen.
        result["pages"].append({
            "url": base_url, "page_type": "home", "http_status": home_resp.status_code,
            "text": trafilatura.extract(home_html) or "", "error": None,
        })

        # Übrige Seiten sequentiell mit 1 req/s.
        spacing = float(limits["request_spacing_s"])
        for ptype, url in selected[1:]:
            await asyncio.sleep(spacing)
            if rp is not None and not rp.can_fetch(ua, url):
                result["pages"].append({"url": url, "page_type": ptype, "http_status": None,
                                        "text": "", "error": "robots"})
                continue
            resp2, body = await _fetch(client, url, max_bytes)
            if resp2 is None or not body:
                result["pages"].append({"url": url, "page_type": ptype, "http_status": None,
                                        "text": "", "error": "fetch_failed"})
                continue
            result["pages"].append({
                "url": str(resp2.url), "page_type": ptype, "http_status": resp2.status_code,
                "text": trafilatura.extract(body) or "", "error": None,
            })
    finally:
        await client.aclose()

    if logger:
        logger.log("crawled", host=host, pages=len(result["pages"]),
                   modernity_reachable=result["tech_signals"].get("reachable"))
    return result


async def crawl_many(websites: list[str], cfg: dict, *, logger=None) -> list[dict]:
    """Crawlt mehrere Domains parallel (Domain-Concurrency aus crawl.yaml)."""
    sem = asyncio.Semaphore(int(cfg["limits"]["domain_concurrency"]))

    async def _one(w):
        async with sem:
            try:
                return await crawl_domain(w, cfg, logger=logger)
            except Exception as e:  # pragma: no cover - Schutznetz
                return {"pages": [], "tech_signals": {"reachable": False}, "error": f"crash:{type(e).__name__}:{e}"}

    return await asyncio.gather(*[_one(w) for w in websites])
