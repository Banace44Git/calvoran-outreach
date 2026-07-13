---
type: supabase table
schema: calvoran
title: job_postings
description: Roh-Stellenanzeigen der BA-Jobsuche-API, dedupliziert über refnr. Erste/letzte Sichtung macht die Anzeigen-Laufzeit sichtbar (Langläufer = Besetzungsschwierigkeit).
written_by: [c6_jobsignale]
read_by: [c6_jobsignale, dashboard (job-signale, job-signale temp)]
source_migration: [0007]
tags: [ba-jobsuche, signal, stellenanzeige]
---

# job_postings

Eine Zeile je BA-Anzeige, Dedup über `refnr`. Bewusst **nicht** `raw_jobs` (tote
Apify-Semantik). `erste_sichtung`/`letzte_sichtung` bilden die Laufzeit ab, ohne Dubletten:
lang offen = Besetzungsschwierigkeit = stärkeres Signal.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `refnr` | text | nein | BA-Referenznummer = Dedup-Anker, **unique**. |
| `titel` | text | nein | Stellentitel. |
| `beruf` | text | ja | BA-Hauptberuf (eigene Negativliste, ≠ Titel). |
| `arbeitgeber` | text | nein | |
| `plz` | text | ja | Aus arbeitsort — Match-Key (indexiert). |
| `ort` | text | ja | |
| `keyword` | text | nein | Welcher Suchbegriff traf (KPI je Keyword). |
| `veroeffentlicht_am` | date | ja | Indexiert desc. |
| `erste_sichtung` | timestamptz | nein | default now(). |
| `letzte_sichtung` | timestamptz | nein | Zuletzt in der API gesehen (default now()). |
| `raw` | jsonb | nein | Roh-Antwort der BA-API. |

## Constraints & Indizes

- **PK** `id`. **Unique** `refnr`.
- Indizes: `plz`, `veroeffentlicht_am desc`.

## Join-Pfade

- `job_matches.posting_id → job_postings.id` (cascade).

## Invarianten & Fallstricke

- `refnr` ist der Dedup-Anker — Re-Runs aktualisieren `letzte_sichtung` statt Dubletten anzulegen.
- `beruf` vs. `titel`: die BA klassifiziert echte kaufm. Leitungen als »Betriebsleiter/in - kaufmännisch« — Titel-Negativliste und Beruf-Negativliste sind deshalb getrennt (`config/jobsignale.yaml`).
- Nicht mit `raw_jobs` verwechseln (Alt-Bestand, andere Semantik).
- 12 Spalten (Drift-Check-Bezug).
