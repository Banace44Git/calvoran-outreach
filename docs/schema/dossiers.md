---
type: supabase table
schema: calvoran
title: dossiers
description: Ein aktuelles LLM-Dossier je Firma (Upsert überschreibt). Vollständiges Dossier-JSON plus Konfidenz und Modell-Herkunft.
written_by: [c3_extract]
read_by: [c4_score_cluster, c5_brief_merge, dashboard]
source_migration: [0002]
tags: [llm, extraktion, dossier]
---

# dossiers

Genau ein Dossier je Firma (`unique (company_id)`, Upsert überschreibt). `dossier` ist das
vollständige JSON nach Konzept §3.2. `model_backend` protokolliert, welches Modell den
Extrakt lieferte (Reproduzierbarkeit / Eskalations-Audit).

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `company_id` | uuid | nein | FK → `companies.id` (cascade), **unique**. |
| `dossier` | jsonb | nein | Vollständiges Dossier-JSON. |
| `konfidenz` | text | ja | hoch\|mittel\|niedrig. |
| `model_backend` | text | nein | z.B. `ollama:gemma...`, `anthropic:claude-haiku-4-5-...` (indexiert). |
| `repair_count` | int | ja | JSON-Repair-Versuche (default 0). |
| `escalated` | boolean | ja | An stärkeres Modell eskaliert (default false). |
| `extracted_at` | timestamptz | nein | default now(). |

## Constraints & Indizes

- **PK** `id`. **Unique** `(company_id)` — ein aktuelles Dossier je Firma.
- FK `company_id → companies.id` (cascade).
- Index: `model_backend`.

## Join-Pfade

- `dossiers.company_id → companies.id`.
- `signals.dossier_id → dossiers.id` (cascade) — Signale referenzieren ihr Quell-Dossier.

## Invarianten & Fallstricke

- Upsert je Firma: ein Re-Run von c3 **ersetzt** das Dossier, es gibt keine Historie.
- `model_backend` steuert das Kosten-/Qualitäts-Audit (lokal vs. Haiku vs. Sonnet).
- 8 Spalten (Drift-Check-Bezug).
