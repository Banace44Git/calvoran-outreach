---
type: supabase table
schema: calvoran
title: outreach
description: Brief/E-Mail-Zeile je Firma, Channel und Welle. Doppel-Herkunft — company_id (Nachfolge-Pipeline) ODER lead_id (Alt-Bestand). Idempotenz-Guard über Unique.
written_by: [c5_brief_merge, dashboard]
read_by: [c5_export, outreach_calls, dashboard]
source_migration: [alt-bestand, 0004, 0006]
tags: [outreach, brief, welle]
---

# outreach

Eine Zeile je Versand (Firma × Channel × Welle). Die Basis-Spalten stammen aus dem
Alt-Bestand (Job-Scraping, `lead_id`); `0004` hat die Tabelle firmenzentriert erweitert
(`company_id` + Brief-Metadaten), `0006` den Idempotenz-Guard ergänzt. **Kein Rename, keine
Datenmigration** — beide Herkünfte koexistieren.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `lead_id` | uuid | ja | FK → `leads.id` (**Alt-Bestand/tot**). |
| `company_id` | uuid | ja | FK → `companies.id` (cascade) — die aktive Herkunft. |
| `channel` | text | ja | brief \| email. |
| `status` | text | ja | Versand-/Antwortstatus. |
| `subject` | text | ja | Betreff (E-Mail) bzw. Brief-Kopf. |
| `body` | text | ja | Brief-/Mailtext. |
| `sent_at` | timestamptz | ja | Versandzeitpunkt (NULL = noch nicht versandt). |
| `response_at` | timestamptz | ja | Antwortzeitpunkt. |
| `notes` | text | ja | |
| `created_at` | timestamptz | ja | |
| `variant` | text | ja | 'A' \| 'B' (A/B-Test). |
| `ansprache_hooks` | jsonb | ja | Belegte Hooks aus dem Dossier. |
| `cluster_key` | text | ja | Für Export-Joins (→ `scores.cluster_key`). |
| `wave` | int | ja | Versandwelle (1 = die erste Selektion). |

## Constraints & Indizes

- **PK** `id`.
- **Unique** `(company_id, channel, wave)` (`outreach_company_channel_wave_uidx`, 0006) — Idempotenz-Guard fürs Brief-Tracking.
- **CHECK** `outreach_one_source`: `company_id IS NOT NULL OR lead_id IS NOT NULL` — **NOT VALID** (prüft nur neue/aktualisierte Zeilen, keine Altzeilen).
- FK `company_id → companies.id` (cascade), `lead_id → leads.id`.
- Index: `company_id`.

## Join-Pfade

- `outreach.company_id → companies.id`.
- `outreach_calls.outreach_id → outreach.id` (set null).
- `outreach.lead_id → leads.id` (Alt-Bestand, nicht weiterverwenden).

## Invarianten & Fallstricke

- **Doppel-Herkunft:** neue Zeilen immer über `company_id` anlegen; `lead_id` ist Erbe der toten Apify-Pipeline.
- Der `outreach_one_source`-CHECK ist `NOT VALID` — Altzeilen mit beiden NULL sind theoretisch möglich, werden aber nicht neu erzeugt.
- Unique `(company_id, channel, wave)`: derselbe Brief je Firma/Welle kann nicht doppelt geschrieben werden (bewusst, für Wiederholungsläufe).
- 15 Spalten (Drift-Check-Bezug).
