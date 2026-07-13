---
type: supabase table
schema: calvoran
title: hermes_log
description: Einziges Schreibziel des Hermes-Automaten (Phase B). Health-Checks, Tagesreports, Trigger-/Wiedervorlage-Läufe. Aktuell inaktiv.
written_by: [hermes-cron (phase B)]
read_by: [dashboard, monitoring]
source_migration: [0005]
tags: [hermes, automation, log]
---

# hermes_log

Append-only Lauf-Protokoll des Hermes-Cron. Hermes liest companies/pages/dossiers/signals/scores
**nur** und schreibt ausschließlich hier. **Noch inaktiv:** die getrennte Hermes-Postgres-Rolle
wird erst in Phase B angelegt; bis dahin läuft kein Hermes-DB-Zugriff.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `job` | text | nein | health_check\|tagesreport\|trigger_monitor\|wiedervorlagen. |
| `run_at` | timestamptz | nein | default now(). |
| `status` | text | nein | ok\|warn\|error. |
| `summary` | text | ja | Kurzfassung des Laufs. |
| `payload` | jsonb | ja | Strukturierte Lauf-Details. |

## Constraints & Indizes

- **PK** `id`.
- Index: `(job, run_at desc)`.

## Join-Pfade

- Keine FKs — bewusst entkoppeltes Log.

## Invarianten & Fallstricke

- Hermes ist **read-only auf alle Fachtabellen**, write-only auf `hermes_log`.
- Phase B braucht User-Go (siehe CLAUDE.md); bis dahin bleibt die Tabelle leer.
- 6 Spalten (Drift-Check-Bezug).
