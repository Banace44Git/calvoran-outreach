---
type: schema index
schema: calvoran
title: calvoran — Schema-Übersicht
description: Nachfolge-Lead-Pipeline (North-Data-Zielliste + BA-Job-Signale). Eine Doku-Datei je Tabelle, Ist-Zustand konsolidiert aus den Migrationen 0001–0009.
tags: [supabase, postgres, overview]
---

# calvoran — Schema-Übersicht

Supabase-Instanz (geteilt mit haufe-scraper), Schema `calvoran`. Zugriff der Pipeline
**ausschließlich über PostgREST** (supabase-py, `calvoran/db.py::get_client`) — kein
direkter Postgres-/`information_schema`-Zugang. Migrationen liegen als
`sql/migrations/0001..0009` und werden einzeln im Supabase SQL-Editor ausgeführt.

Diese Doku ist der **Ist-Zustand pro Tabelle**, konsolidiert aus den additiv gewachsenen
Migrationen (eine Tabelle entsteht oft aus mehreren: `outreach` = Alt-Bestand + `0004`,
`job_matches` = `0007` + `0008` + `0009`). Bei DB-Arbeit hier zuerst nachlesen; nach einer
neuen Migration die betroffene Datei mitziehen und `docs/schema/check_drift.py` laufen lassen.

## Tabellen (aktiv)

| Tabelle | Zweck | Geschrieben von |
|---|---|---|
| [companies](companies.md) | Firmen-Zielliste (North Data, ~70k KMU), `raw` verlustfrei | c1 / c1b / c2 / os-Import (extern) |
| [pages](pages.md) | Website-Crawl-Zustand + Tech-Signale je URL | c2_crawl |
| [dossiers](dossiers.md) | LLM-Dossier je Firma (1 aktuelles, Upsert) | c3_extract |
| [signals](signals.md) | Belegpflichtige Einzelsignale — **c3 überschreibt je Firma** | c3_extract |
| [scores](scores.md) | Bedarfs-Score A/B/C/KO + Cluster (1 je Firma) | c4_score_cluster |
| [outreach](outreach.md) | Brief/E-Mail je Welle; Doppel-Herkunft company_id/lead_id | c5_brief_merge / Dashboard |
| [outreach_calls](outreach_calls.md) | Anruf-CRM (n Versuche/Firma + Wiedervorlage) | Dashboard (Nachverfolgung) |
| [hermes_log](hermes_log.md) | Hermes-Schreibziel (Phase B, noch inaktiv) | Hermes-Cron |
| [job_postings](job_postings.md) | BA-Jobsuche-Roh-Anzeigen, Dedup über `refnr` | c6_jobsignale |
| [job_matches](job_matches.md) | Anzeige ⨯ Firma; extern = company_id NULL | c6_jobsignale / Dashboard |

## Beziehungsgraph (FK-Kanten)

`companies.id` ist die Nabe. `on delete cascade` sofern nicht anders vermerkt.

```
companies ──< pages            (company_id)
companies ──1 dossiers         (company_id, UNIQUE)
companies ──< signals          (company_id)   signals ──> dossiers (dossier_id)
companies ──1 scores           (company_id, UNIQUE)
companies ──< outreach         (company_id)   outreach ──> leads (lead_id, ALT/tot)
companies ──< outreach_calls   (company_id)   outreach_calls ──> outreach (outreach_id, set null)
companies ──< job_matches      (company_id, NULLABLE → extern)
job_postings ──< job_matches   (posting_id)
companies ──> companies        (dup_of, self-ref Dedup)
```

## Wer schreibt was (Pipeline c0–c6)

- **c1_import_zielliste** → `companies` (Insert/Upsert, `raw` jsonb).
- **c1b_import_gf_alter** → `companies` (Update `gf_geburtsjahr/gf_alter/gf_quelle`). Laufende GF-Anreicherung passiert extern im os-Projekt.
- **c2_crawl** → `pages` (Crawl-State), `companies` (Modernitäts-Felder).
- **c3_extract** → `dossiers` (Upsert je Firma), `signals` (**delete+insert je Firma**).
- **c4_score_cluster** → `scores` (Upsert je Firma).
- **c5_brief_merge** → `outreach` (je Welle/Channel).
- **c6_jobsignale** → `job_postings`, `job_matches`.
- **Dashboard** → `outreach` (Versand-Flag), `outreach_calls` (Anrufe), `job_matches` (Sichtungs-Status).
- **Hermes** (Phase B) → nur `hermes_log`.

## Alt-Bestand — NICHT anfassen

`raw_jobs` (7 Spalten) und `leads` (22 Spalten) sind die tote Apify-Stellenanzeigen-Pipeline
(Indeed/StepStone/Arbeitsagentur, `scripts/01..04`). Wird nicht mehr betrieben; c6 nutzt
stattdessen die BA-API direkt (→ `job_postings`/`job_matches`). Einzige lebende Verbindung:
`outreach.lead_id` (FK → `leads.id`) existiert noch neben `outreach.company_id`. Keine eigene
Doku-Datei, bewusst in `check_drift.py` ignoriert.

## Konventionen dieser Doku

- Frontmatter je Datei: `type`, `schema`, `title`, `description`, `written_by`, `read_by`, `source_migration`, `tags`.
- Spaltennamen in der `## Spalten`-Tabelle stehen in `` `backticks` `` — daran hängt der Drift-Check.
- `## Invarianten & Fallstricke` = die anwendungsseitigen Regeln, die kein DB-Constraint erzwingt.
