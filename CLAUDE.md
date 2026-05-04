# calvoran-outreach

Lead-Generierung über Stellenanzeigen. Deutsche KMU mit akutem Admin-Bedarf identifizieren als Kunden für KI-gestützte Verwaltungsdienstleistungen.

## Stack

- Python 3.11+ in `.venv`
- Apify (3 Actors: Indeed DE, StepStone DE, Arbeitsagentur DE)
- Supabase (gleiche Instanz wie haufe-scraper, Schema `calvoran`)
- Claude Haiku für Ansprechpartner-Extraktion

## Pipeline

```
01_scrape.py        → calvoran.raw_jobs   (alle Treffer, Roh)
02_filter_dedup.py  → calvoran.leads      (gefiltert, dedupliziert, Stadt-getaggt)
03_extract_contact.py → updated leads     (Ansprechpartner via Haiku)
04_export_csv.py    → data/YYYY-MM-DD_leads.csv
```

## Setup (einmalig)

1. **Schema in Supabase anlegen**: `sql/schema.sql` in Supabase Studio → SQL Editor ausführen.
2. **Schema exponieren**: Supabase Dashboard → Project Settings → API → "Exposed schemas" → `calvoran` hinzufügen, save.
3. **Venv**: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (bereits passiert).

## Run

```bash
# Dry-run: nur erste Query, alle 3 Quellen
.venv/bin/python scripts/01_scrape.py --dry-run

# Full Scan (10 queries × 3 Plattformen × 20 items = ~600 Anzeigen)
.venv/bin/python scripts/01_scrape.py

# Filter + Dedup
.venv/bin/python scripts/02_filter_dedup.py

# Ansprechpartner extrahieren (default 50)
.venv/bin/python scripts/03_extract_contact.py --limit 100

# CSV exportieren
.venv/bin/python scripts/04_export_csv.py
```

## Konfiguration

- `config/keywords.py` — Include-Queries, Exclude-Terms, Stadt-Listen (Top 21–100)
- `config/actor_*.json` — Cached Input-Schemas der drei Apify-Actors

## Stadt-Strategie

Apify scrapt **bundesweit** (kein location-Filter), Stadt-Filter passiert in 02. Begründung: Indeed/StepStone akzeptieren nur eine Location pro Run; 80 Städte × 10 Queries × 3 Plattformen wären zu teuer. Trade-off: Top-20-Städte dominieren die Treffer; Top-21-100-Anteil muss empirisch beobachtet werden. Bei zu niedriger Quote: Zweiter Scan mit gezielten Stadt-Suchen.

## Scope-Hinweise

- Buchhaltungs-Themen sind explizit raus (ausgeschlossen via EXCLUDE_TERMS).
- Outreach-Tabelle ist angelegt aber noch nicht befüllt (Phase 2).
