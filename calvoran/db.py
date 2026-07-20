"""Schema-bewusster Supabase-Client für `calvoran`.

Kanonische Variante (robustes .env-Laden mit explizitem Pfad, damit es auch in
Subprozessen und Heredocs funktioniert). `scripts/_db.py` bleibt für die
bestehende Job-Scraping-Pipeline; die Logik ist identisch.
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from postgrest.exceptions import APIError
from supabase import create_client

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_LOADED = False


def ensure_env() -> None:
    """Lädt die projekt-lokale .env genau einmal (idempotent, expliziter Pfad)."""
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))
        _ENV_LOADED = True


# Rückwärtskompatibler Alias.
_ensure_env = ensure_env


def get_client(schema: str = "calvoran"):
    """Liefert einen schema-scoped Client. `client.table('foo')` -> `<schema>.foo`."""
    _ensure_env()
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key).schema(schema)


def fetch_all(make_query, *, key: str = "id", step: int = 1000, retries: int = 3) -> list:
    """Liest eine Selektion vollständig per Keyset-Pagination (`key` > letzter Wert).

    `make_query` liefert je Aufruf einen frischen Builder (select + Filter, OHNE
    order/range/limit); `key` muss eindeutig und in der Select-Liste enthalten sein.
    Keyset statt OFFSET, weil deep OFFSET auf companies je Seite teurer wird und in
    den Supabase-statement_timeout läuft (APIError 57014); genau der wird zusätzlich
    mit Backoff neu versucht.
    """
    rows: list = []
    last = None
    while True:
        for attempt in range(retries):
            q = make_query()
            if last is not None:
                q = q.gt(key, last)
            try:
                r = q.order(key).limit(step).execute()
                break
            except APIError as e:
                if str(getattr(e, "code", "")) != "57014" or attempt == retries - 1:
                    raise
                time.sleep(5 * (attempt + 1))
        rows.extend(r.data)
        if len(r.data) < step:
            return rows
        last = r.data[-1][key]


def get_apify_token() -> str:
    _ensure_env()
    return os.environ.get("APIFY_TOKEN") or os.environ["APIFY_API_KEY"]
