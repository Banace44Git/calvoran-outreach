"""Schema-bewusster Supabase-Client für `calvoran`.

Kanonische Variante (robustes .env-Laden mit explizitem Pfad, damit es auch in
Subprozessen und Heredocs funktioniert). `scripts/_db.py` bleibt für die
bestehende Job-Scraping-Pipeline; die Logik ist identisch.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
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


def get_apify_token() -> str:
    _ensure_env()
    return os.environ.get("APIFY_TOKEN") or os.environ["APIFY_API_KEY"]
