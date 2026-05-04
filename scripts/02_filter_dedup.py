"""Liest calvoran.raw_jobs, mappt Felder je Quelle, wendet Filter an, dedupliziert plattformübergreifend
und schreibt nach calvoran.leads.

- Include-Filter ist bereits per Suchbegriff erfolgt (kein Extra-Match nötig).
- Exclude-Filter: Treffer in EXCLUDE_TERMS in titel ODER description → excluded=true.
- Stadt-Filter: in_target_cities=true wenn Standort einer Top-21-100-Stadt zuordenbar.
- Dedupe-Key: normalisiert(firma|titel) — gleiches Lead in mehreren Plattformen wird einmal angelegt
              (erster Treffer gewinnt; spätere Treffer werden NICHT angelegt).
"""

import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tqdm import tqdm

from config.keywords import EXCLUDE_TERMS, EXCLUDED_CITIES, TARGET_CITIES
from scripts._db import get_client


REMOTE_HINTS = ("remote", "homeoffice", "home-office", "home office", "telearbeit", "mobiles arbeiten")


def normalize(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def detect_city(standort: str | None) -> tuple[bool, str | None]:
    """Returns (in_target, matched_city). Matched_city ist der gefundene Cityname."""
    if not standort:
        return False, None
    norm_loc = normalize(standort)
    # Excluded zuerst (Top 20)
    for city in EXCLUDED_CITIES:
        if normalize(city) in norm_loc:
            return False, city
    for city in TARGET_CITIES:
        if normalize(city) in norm_loc:
            return True, city
    return False, None


def is_remote(*texts: str | None) -> bool:
    blob = " ".join((t or "") for t in texts).lower()
    return any(h in blob for h in REMOTE_HINTS)


def has_excluded_term(*texts: str | None) -> str | None:
    blob = " ".join((t or "") for t in texts).lower()
    for term in EXCLUDE_TERMS:
        if term.lower() in blob:
            return term
    return None


# ============ Mapping je Quelle ============

def map_indeed(p: dict) -> dict:
    posted = p.get("datePosted") or p.get("postedAt") or p.get("postingDate")
    # datePosted ist Unix-ms-Timestamp
    if isinstance(posted, (int, float)):
        from datetime import datetime, timezone
        posted = datetime.fromtimestamp(posted / 1000, tz=timezone.utc).date().isoformat()
    return {
        "firmenname": p.get("company") or p.get("companyName"),
        "stellentitel": p.get("title") or p.get("positionName") or p.get("jobTitle"),
        "standort": p.get("location"),
        "link": p.get("jobUrl") or p.get("url") or p.get("externalApplyLink"),
        "description": p.get("description") or p.get("descriptionText"),
        "posted_date": posted,
        "remote_hint": p.get("isRemote"),
    }


def map_stepstone(p: dict) -> dict:
    return {
        "firmenname": p.get("company") or p.get("companyName") or p.get("employer"),
        "stellentitel": p.get("title") or p.get("jobTitle"),
        "standort": p.get("city") or p.get("location"),
        "link": p.get("url") or p.get("jobUrl") or p.get("applyUrl"),
        "description": p.get("descriptionText") or p.get("description") or p.get("descriptionMarkdown") or p.get("summary"),
        "posted_date": p.get("publishDate") or p.get("publishDateISO") or p.get("postedDate"),
    }


def map_arbeitsagentur(p: dict) -> dict:
    name_parts = [p.get("contact_salutation"), p.get("contact_firstname"), p.get("contact_lastname")]
    ansprechpartner = " ".join(x for x in name_parts if x).strip() or None
    return {
        "firmenname": p.get("company"),
        "stellentitel": p.get("title"),
        "standort": p.get("location"),
        "link": p.get("source_url") or p.get("apply_url"),
        "description": p.get("description_full") or p.get("description_snippet"),
        "posted_date": p.get("posted_at"),
        "ansprechpartner": ansprechpartner,
        "ansprechpartner_email": p.get("contact_email") or p.get("apply_email"),
        "ansprechpartner_phone": p.get("contact_phone"),
        "remote_hint": p.get("remote_option"),
    }


MAPPERS = {
    "indeed": map_indeed,
    "stepstone": map_stepstone,
    "arbeitsagentur": map_arbeitsagentur,
}


def parse_date(val) -> str | None:
    if not val:
        return None
    if isinstance(val, str):
        # ISO-Date oder Date-String → wir vertrauen Postgres
        return val[:10] if len(val) >= 10 else None
    return None


def main() -> None:
    db = get_client()
    print("Lade raw_jobs ...")
    raw_rows = db.table("raw_jobs").select("*").execute().data
    print(f"  {len(raw_rows)} raw_jobs")

    print("Lade existierende leads (für Dedupe) ...")
    existing = db.table("leads").select("dedupe_key").execute().data
    seen_keys: set[str] = {r["dedupe_key"] for r in existing if r.get("dedupe_key")}
    print(f"  {len(seen_keys)} dedupe_keys bereits vorhanden")

    new_leads: list[dict] = []
    skipped_dup = 0
    skipped_no_data = 0

    for row in tqdm(raw_rows, desc="mappen"):
        source = row["source"]
        payload = row["payload"]
        mapper = MAPPERS.get(source)
        if not mapper:
            continue

        try:
            mapped = mapper(payload)
        except Exception as exc:
            tqdm.write(f"  map error ({source}/{row['external_id']}): {exc}")
            continue

        firma = (mapped.get("firmenname") or "").strip()
        titel = (mapped.get("stellentitel") or "").strip()
        if not firma or not titel:
            skipped_no_data += 1
            continue

        dedupe_key = f"{normalize(firma)}|{normalize(titel)}"
        if dedupe_key in seen_keys:
            skipped_dup += 1
            continue
        seen_keys.add(dedupe_key)

        standort = mapped.get("standort")
        in_target, matched_city = detect_city(standort)
        excluded_term = has_excluded_term(titel, mapped.get("description"))
        excluded = bool(excluded_term)
        exclude_reason = f"excluded_term: {excluded_term}" if excluded_term else None

        new_leads.append({
            "raw_job_id": row["id"],
            "source": source,
            "external_id": row.get("external_id"),
            "firmenname": firma,
            "stellentitel": titel,
            "standort": standort,
            "remote": bool(mapped.get("remote_hint")) or is_remote(titel, mapped.get("description"), standort),
            "link": mapped.get("link"),
            "ansprechpartner": mapped.get("ansprechpartner"),
            "ansprechpartner_email": mapped.get("ansprechpartner_email"),
            "ansprechpartner_phone": mapped.get("ansprechpartner_phone"),
            "posted_date": parse_date(mapped.get("posted_date")),
            "description": (mapped.get("description") or "")[:5000],  # cap für DB
            "dedupe_key": dedupe_key,
            "in_target_cities": in_target,
            "excluded": excluded,
            "exclude_reason": exclude_reason,
        })

    print(f"\n→ neu: {len(new_leads)}  |  duplikat (skip): {skipped_dup}  |  ohne firma/titel: {skipped_no_data}")

    if new_leads:
        # in Batches einfügen
        batch_size = 100
        for i in tqdm(range(0, len(new_leads), batch_size), desc="insert"):
            db.table("leads").insert(new_leads[i:i+batch_size]).execute()

    # Statistik
    in_target = sum(1 for l in new_leads if l["in_target_cities"] and not l["excluded"])
    print(f"\n→ verwertbar (in target cities, nicht excluded): {in_target}")


if __name__ == "__main__":
    main()
