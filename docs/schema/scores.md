---
type: supabase table
schema: calvoran
title: scores
description: Bedarfs-Score A/B/C/KO je Firma plus Cluster-Zuordnung (Branche × Größenband) für die Briefvariante. Ein Score je Firma.
written_by: [c4_score_cluster]
read_by: [c5_brief_merge, dashboard]
source_migration: [0003]
tags: [scoring, cluster, klassifikation]
---

# scores

Ein Score je Firma (`unique (company_id)`). `breakdown` hält die Score-Herleitung, `begruendung`
ist der Klartext, der zugleich als Anruf-Briefing dient. `cluster_key` = `"<branche>__<groessenband>"`
wählt die Briefvariante.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `company_id` | uuid | nein | FK → `companies.id` (cascade), **unique**. |
| `score_total` | int | nein | Gesamt-Bedarfs-Score. |
| `score_klasse` | text | nein | CHECK A\|B\|C\|KO. |
| `breakdown` | jsonb | nein | `{anker,nachfolge,web_bedarf,abzuege}`. |
| `begruendung` | text | nein | Klartext = Anruf-Briefing. |
| `scoring_version` | text | nein | Version aus scoring.yaml (Reproduzierbarkeit). |
| `cluster_branche` | text | ja | bau_gebaeudetechnik\|produzierend\|… |
| `groessenband` | text | ja | klein\|kern\|oberes_band. |
| `cluster_key` | text | ja | `<branche>__<groessenband>` → Briefvariante (indexiert). |
| `scored_at` | timestamptz | nein | default now(). |

## Constraints & Indizes

- **PK** `id`. **Unique** `(company_id)`.
- **CHECK** `score_klasse IN ('A','B','C','KO')`.
- FK `company_id → companies.id` (cascade).
- Indizes: `score_klasse`, `cluster_key`.

## Join-Pfade

- `scores.company_id → companies.id`.
- `cluster_key` verbindet (logisch) mit `outreach.cluster_key` für Export-Joins.

## Invarianten & Fallstricke

- Upsert je Firma: kein Score-Verlauf, der letzte c4-Lauf gewinnt.
- `score_klasse = 'KO'` = ausgeschlossen (kein Outreach).
- `scoring_version` festhalten, damit alte Scores reproduzierbar bleiben.
- 11 Spalten (Drift-Check-Bezug).
