---
type: supabase table
schema: calvoran
title: outreach_calls
description: Anruf-CRM — n Kontaktversuche je Firma mit Ergebnis und Wiedervorlage. Speist den Nachverfolgung-Tab des Dashboards.
written_by: [dashboard (nachverfolgung)]
read_by: [dashboard, hermes (wiedervorlagen)]
source_migration: [0006]
tags: [crm, anruf, wiedervorlage]
---

# outreach_calls

Ein Eintrag je Anrufversuch (n je Firma). `outcome` ist enum-geprüft, `follow_up_at` treibt
die Wiedervorlage-Liste (partieller Index).

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `company_id` | uuid | nein | FK → `companies.id` (cascade). |
| `outreach_id` | uuid | ja | FK → `outreach.id` (set null). |
| `called_at` | timestamptz | nein | default now(). |
| `outcome` | text | nein | CHECK (s.u.). |
| `follow_up_at` | timestamptz | ja | Wiedervorlage-Termin. |
| `notes` | text | ja | Gesprächsnotiz. |
| `created_at` | timestamptz | nein | default now(). |

## Constraints & Indizes

- **PK** `id`.
- **CHECK** `outcome IN ('nicht_erreicht','gesprochen','rueckruf_vereinbart','termin','kein_interesse','nicht_zustaendig','falsche_nummer')`.
- FK `company_id → companies.id` (cascade), `outreach_id → outreach.id` (set null).
- Indizes: `company_id`; partiell `follow_up_at WHERE follow_up_at IS NOT NULL`.

## Join-Pfade

- `outreach_calls.company_id → companies.id`, `outreach_calls.outreach_id → outreach.id`.

## Invarianten & Fallstricke

- Mehrere Anrufe je Firma sind der Normalfall (kein Unique).
- Offene Wiedervorlagen = Zeilen mit `follow_up_at IS NOT NULL` (partieller Index).
- 8 Spalten (Drift-Check-Bezug).
