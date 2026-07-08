# calvoran-outreach

Nachfolge-Lead-Pipeline für Fractional-CFO-/Verwaltungs-Dienstleistungen: North-Data-
Zielliste (~70k deutsche KMU) → Anreicherung/Scoring → kuratierte Brief-Wellen → CRM-
Nachverfolgung. Dazu signalgetriebene Leads aus BA-Stellenanzeigen (c6).

## Stack

- Python 3.11+ in `.venv`
- Supabase (gleiche Instanz wie haufe-scraper, Schema `calvoran`)
- Modell-Router: lokales Gemma (Ollama) / Claude Haiku / Sonnet (`config/models.yaml`)
- Streamlit-Dashboard (`dashboard/kuratierung.py`)

## Pipeline (aktiv)

```
pipeline/c0_merge_searchresults.py   North-Data-CSVs -> Master-Zielliste
pipeline/c1_import_zielliste.py      -> calvoran.companies (raw jsonb verlustfrei)
pipeline/c1b_import_gf_alter.py      GF-Geburtsjahr/-Alter aus hr-engine-CSV
pipeline/c2_crawl.py                 Website-Crawl + Modernitäts-Score (resumebar)
pipeline/c3_extract.py               Dossiers + belegpflichtige signals (resumebar)
pipeline/c4_score_cluster.py         Scoring A/B/C/KO -> scores
pipeline/c5_brief_merge.py           Serienbriefe (Word-Anker-Merge) + outreach-Zeilen
pipeline/c6_jobsignale.py            BA-Jobsuche -> job_postings/job_matches (s.u.)
```

Dashboard: `.venv/bin/streamlit run dashboard/kuratierung.py` (Port 8502; 8501 gehört
einem fremden Projekt). Tabs: Tabelle, Karteikarte, Nachverfolgung (Anruf-CRM), Job-Signale.

## Job-Signal-Modul (c6)

Sucht eine Zielfirma per Stellenanzeige einen GF / eine kaufmännische Leitung / zweite
Führungsebene, ist das bei Inhabern 58+ ein Übergabe-Indikator. BA-Jobsuche-API
(inoffiziell, statischer Key `jobboerse-jobsuche`, community-dokumentiert:
bundesAPI/jobsuche-api) — kein PLZ-Filter, daher bundesweiter Scan je Keyword und
lokales Matching (Name+PLZ, `calvoran/matching.py`, rapidfuzz).

```bash
.venv/bin/python pipeline/c6_jobsignale.py --backfill 28    # Erstlauf (API-Maximum 28 Tage!)
.venv/bin/python pipeline/c6_jobsignale.py --since 7        # Wochenlauf, idempotent
.venv/bin/python pipeline/c6_jobsignale.py --rematch        # Schwellwert-Tuning ohne API
.venv/bin/python pipeline/c6_jobsignale.py --reprio         # Prio nach GF-Alter-Anreicherung
.venv/bin/python pipeline/c6_jobsignale.py --report         # KPI-Markdown nach OUTPUT_DIR
```

Achtung API-Eigenheit: `veroeffentlichtseit` akzeptiert nur 1/7/14/28 — andere Werte
ignoriert die API **still** und liefert den ungefilterten Gesamtbestand. Der Client
(`calvoran/ba_jobsuche.py`) snappt deshalb aufwärts auf den nächsten gültigen Wert.

Konfig: `config/jobsignale.yaml` (Keywords, Titel-Filter, Match-Schwellwerte).
Sichtung: Dashboard-Tab »Job-Signale« (Status neu → gesichtet/relevant/irrelevant).
Phase B (nach Sichtungsmonat, User-Go nötig): Hermes-Cron täglich + Brief »zweite
Führungsebene« über c5-Mechanik.

## Supabase (Schema `calvoran`)

- `companies` (Zielliste, `raw` jsonb, `gf_alter` partiell — Anreicherung läuft extern),
  `pages`, `dossiers`, `signals` (von c3 je Firma überschrieben!), `scores`
- `outreach` (Brief/E-Mail je Welle, Unique `(company_id, channel, wave)`),
  `outreach_calls` (Anruf-CRM), `hermes_log`
- `job_postings`/`job_matches` (Migration 0007) — BA-Anzeigen + Firmen-Matches
- Migrationen: `sql/migrations/0001..0007`, einzeln im Supabase SQL-Editor ausführen

## Pfade außerhalb des Repos

- Output/Selektion: `/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/outreach/`
  (`selection.jsonl` = Lead-Wahrheit je Welle, Briefe, Reports)
- Word-Vorlage: `os/00-inbox/JTILS-v*.docx`; GF-Daten: `…/fractional-cfo/hr-abruf/`

## Alt-Bestand (tot, nicht anfassen)

`scripts/01..04` + `config/keywords.py` + `config/actor_*.json`: der frühere
Apify-Stellenanzeigen-Scraper (Indeed/StepStone/Arbeitsagentur) samt `raw_jobs`/`leads`.
Wird nicht mehr betrieben; c6 nutzt stattdessen die BA-API direkt.
