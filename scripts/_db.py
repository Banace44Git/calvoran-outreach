"""Supabase-Client-Helper. Liefert Schema-bewussten Client für `calvoran`."""

import os

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def get_client(schema: str = "calvoran"):
    """Returns a schema-scoped client. `client.table('foo')` operiert auf `<schema>.foo`."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    client = create_client(url, key)
    return client.schema(schema)


def get_apify_token() -> str:
    return os.environ.get("APIFY_TOKEN") or os.environ["APIFY_API_KEY"]
