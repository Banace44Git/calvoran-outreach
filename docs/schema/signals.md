---
type: supabase table
schema: calvoran
title: signals
description: Belegpflichtige Einzelsignale je Firma (Zitat + Quell-URL Pflicht). Wird von c3 je Firma per delete+insert überschrieben.
written_by: [c3_extract]
read_by: [c4_score_cluster, c5_brief_merge, dashboard]
source_migration: [0002]
tags: [signale, belegpflicht, scoring]
---

# signals

Normalisierte Einzelsignale fürs Scoring. **Belegpflicht** über NOT-NULL auf `beleg_zitat`
und `beleg_url` — jedes Signal trägt sein wörtliches Website-Zitat und die Quelle.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `company_id` | uuid | nein | FK → `companies.id` (cascade). |
| `dossier_id` | uuid | ja | FK → `dossiers.id` (cascade). |
| `signal_type` | text | nein | nachfolge\|familienunternehmen\|kaufm_funktion_fehlt\|offene_kaufm_stelle\|zweite_ebene_fehlt\|digitalisierung\|… |
| `value` | text | ja | Signal-Ausprägung. |
| `beleg_zitat` | text | nein | Wörtliches Zitat von der Website (**Pflicht**). |
| `beleg_url` | text | nein | Quell-URL (**Pflicht**). |
| `created_at` | timestamptz | nein | default now(). |

## Constraints & Indizes

- **PK** `id`. Kein Unique — mehrere Signale je Firma erlaubt.
- FK `company_id → companies.id` (cascade), `dossier_id → dossiers.id` (cascade).
- Indizes: `company_id`, `signal_type`.

## Join-Pfade

- `signals.company_id → companies.id`, `signals.dossier_id → dossiers.id`.

## Invarianten & Fallstricke

- **c3_extract überschreibt `signals` je Firma per delete+insert.** Alles, was hier je Firma
  liegen soll, muss c3 in einem Lauf produzieren — Fremd-Inserts (z.B. BA-Job-Treffer) würden
  beim nächsten c3-Lauf verschwinden. **Genau deshalb** leben Job-Signale in
  [job_matches](job_matches.md), nicht hier.
- Belegpflicht ist DB-erzwungen (NOT NULL) — kein Signal ohne Zitat + URL.
- 8 Spalten (Drift-Check-Bezug).
