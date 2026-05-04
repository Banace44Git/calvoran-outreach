"""Extrahiert Ansprechpartner (Name, E-Mail, Telefon) aus dem Anzeigen-Beschreibungstext via Claude Haiku.

- Bearbeitet nur Leads die noch keinen `ansprechpartner` haben UND `in_target_cities` UND nicht `excluded`.
- Speichert Ergebnisse zurück + setzt `contact_extracted_at`.
- Kostenkontrolle: --limit, default 50.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv
from tqdm import tqdm

from scripts._db import get_client

load_dotenv()

MODEL = "claude-haiku-4-5"

PROMPT = """Du bekommst den Volltext einer deutschen Stellenanzeige. Extrahiere — falls erkennbar — den Ansprechpartner für die Bewerbung.

Antworte ausschließlich als JSON-Objekt mit den Feldern:
{
  "name": "Vorname Nachname (mit Anrede falls vorhanden, z.B. 'Frau Dr. Müller') oder null",
  "email": "E-Mail oder null",
  "phone": "Telefonnummer oder null"
}

Regeln:
- Wenn KEIN Ansprechpartner namentlich genannt ist (nur z.B. 'unser HR-Team' oder 'Personalabteilung'), setze name=null.
- Generische Adressen wie 'bewerbung@firma.de' nur dann übernehmen, wenn KEINE persönliche Adresse vorhanden ist.
- Keine Erklärung, kein Markdown, nur das JSON."""


def extract(client: Anthropic, description: str) -> dict:
    if not description or len(description.strip()) < 30:
        return {"name": None, "email": None, "phone": None}

    msg = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=PROMPT,
        messages=[{"role": "user", "content": description[:6000]}],
    )
    text = msg.content[0].text.strip()
    # Defensive: JSON aus möglichen Code-Fences ziehen
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rstrip("`")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"name": None, "email": None, "phone": None, "_raw": text}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--force", action="store_true", help="Auch Leads neu verarbeiten, die schon contact_extracted_at haben")
    args = parser.parse_args()

    db = get_client()
    anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    q = (
        db.table("leads")
        .select("id,description,ansprechpartner,ansprechpartner_email,ansprechpartner_phone,contact_extracted_at")
        .eq("in_target_cities", True)
        .eq("excluded", False)
        .limit(args.limit)
    )
    if not args.force:
        q = q.is_("contact_extracted_at", "null")

    leads = q.execute().data
    print(f"{len(leads)} Leads zur Extraktion.")

    updated = 0
    skipped = 0
    for lead in tqdm(leads, desc="extract"):
        try:
            res = extract(anthropic, lead.get("description") or "")
        except Exception as exc:
            tqdm.write(f"  ✗ {lead['id']}: {exc}")
            continue

        update = {"contact_extracted_at": "now()"}
        # nur überschreiben wenn DB-Feld leer ist
        if res.get("name") and not lead.get("ansprechpartner"):
            update["ansprechpartner"] = res["name"]
        if res.get("email") and not lead.get("ansprechpartner_email"):
            update["ansprechpartner_email"] = res["email"]
        if res.get("phone") and not lead.get("ansprechpartner_phone"):
            update["ansprechpartner_phone"] = res["phone"]

        # contact_extracted_at über raw SQL geht via supabase-py nicht direkt — separat update
        upd_payload = {k: v for k, v in update.items() if k != "contact_extracted_at"}
        upd_payload["contact_extracted_at"] = "now()"
        # supabase-py nimmt String-Wert "now()" nicht als SQL — also ISO-Timestamp
        from datetime import datetime, timezone
        upd_payload["contact_extracted_at"] = datetime.now(timezone.utc).isoformat()

        db.table("leads").update(upd_payload).eq("id", lead["id"]).execute()

        if res.get("name") or res.get("email") or res.get("phone"):
            updated += 1
        else:
            skipped += 1
        time.sleep(0.1)  # gentle rate limiting

    print(f"\n→ updated mit Kontakt: {updated}, ohne Kontakt erkannt: {skipped}")


if __name__ == "__main__":
    main()
