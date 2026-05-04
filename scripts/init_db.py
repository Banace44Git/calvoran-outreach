"""Wendet sql/schema.sql auf Supabase an. Idempotent (CREATE IF NOT EXISTS)."""

import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    client = create_client(url, key)

    sql = SCHEMA_PATH.read_text()
    print(f"Applying {SCHEMA_PATH.name} ({len(sql)} chars)...")

    # supabase-py hat keinen direkten SQL-Exec. Nutze postgrest RPC, fallback: direkt via httpx auf /rest/v1/rpc.
    # Wir nutzen die Supabase Admin API über den postgres connection string nicht hier — stattdessen:
    # Manuell in Supabase SQL Editor ausführen ODER über pg-Connection. supabase-py hat .rpc() für stored procs.
    # Für DDL: Anweisung an User, das SQL in Supabase Studio auszuführen.
    print("\n----- Supabase hat keinen DDL-Endpunkt über die REST API. -----")
    print("Bitte sql/schema.sql in Supabase Studio → SQL Editor ausführen.")
    print(f"Datei: {SCHEMA_PATH}")
    print(
        "\nAlternativ: psql mit DB_URL aus Supabase Settings → Database → Connection String."
    )


if __name__ == "__main__":
    main()
