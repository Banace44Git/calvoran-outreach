---
type: supabase table
schema: calvoran
title: companies
description: Firmen-Stammdaten der North-Data-Zielliste (~70k KMU) inkl. Finanzkennzahlen, GF-Anreicherung und verlustfreiem raw-jsonb. Nabe des Schemas.
written_by: [c1_import_zielliste, c1b_import_gf_alter, c2_crawl, os-import (extern)]
read_by: [c2_crawl, c3_extract, c4_score_cluster, c5_brief_merge, c6_jobsignale, dashboard]
source_migration: [0001]
tags: [zielliste, stammdaten, scoring]
---

# companies

Eine Zeile je Firma. Natürlicher Schlüssel: `north_data_url` (unique). `raw` hält alle
53 CSV-Spalten der North-Data-Ausgabe verlustfrei; getypte Felder daneben sind fürs Scoring
extrahiert. **Die Zielliste gehört dem os-Import** — c6 & Co. schreiben hier nicht neu ein,
sondern matchen dagegen.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK, `gen_random_uuid()`. |
| `north_data_url` | text | nein | Natürlicher Schlüssel, **unique**. |
| `name` | text | nein | Firmenname. |
| `rechtsform` | text | ja | GmbH, KG, … |
| `plz` | text | ja | Match-Key für Job-Signale (Name+PLZ). |
| `ort` | text | ja | |
| `strasse` | text | ja | |
| `hr_amtsgericht` | text | ja | Handelsregister-Gericht. |
| `register_id` | text | ja | HR-Nummer. |
| `status` | text | ja | Firmenstatus (aktiv/…) aus North Data. |
| `website` | text | ja | |
| `domain` | text | ja | Aus `website` normalisiert (indexiert). |
| `branche_wz` | text | ja | WZ-Code, z.B. "43.21". |
| `wz2` | text | ja | 2-Steller (indexiert, Cluster). |
| `ges_vertreter` | jsonb | ja | `[v1, v2, v3]` Geschäftsführer/Vertreter. |
| `anzahl_gf` | int | ja | |
| `gf_name_in_firmenname` | boolean | ja | Heuristik Familienunternehmen. |
| `bilanzsumme_eur` | numeric | ja | Getypt fürs Scoring. |
| `ek_quote_pct` | numeric | ja | Eigenkapitalquote. |
| `gewinn_cagr_pct` | numeric | ja | |
| `umsatz_eur` | numeric | ja | |
| `mitarbeiterzahl` | int | ja | |
| `prioritaets_score` | numeric | ja | Vorpriorisierung (indexiert desc). |
| `gf_geburtsjahr` | int | ja | GF-Anreicherung (hr-engine/AD). |
| `gf_alter` | int | ja | **Partiell befüllt** — externe os-Anreicherung; treibt Job-Signal-Prio. |
| `gf_quelle` | text | ja | Herkunft der GF-Daten. |
| `holding_flag` | boolean | ja | Stufe-0-Bereinigung (default false, indexiert). |
| `holding_reason` | text | ja | |
| `dup_of` | uuid | ja | Self-FK → `companies.id` (Dedup). |
| `excluded` | boolean | ja | Aus Zielliste ausgeschlossen (default false, indexiert). |
| `exclude_reason` | text | ja | |
| `website_modernity_score` | int | ja | 0..10, NULL = keine Website (c2). |
| `modernity_breakdown` | jsonb | ja | Score-Herleitung (c2). |
| `tech_signals` | jsonb | ja | Erkannte Tech-Merkmale (c2). |
| `raw` | jsonb | nein | Alle 53 CSV-Spalten verlustfrei. |
| `imported_at` | timestamptz | nein | default now(). |
| `updated_at` | timestamptz | nein | default now(). |

## Constraints & Indizes

- **PK** `id`. **Unique** `north_data_url`.
- Self-FK `dup_of → companies.id`.
- Indizes: `domain`, `wz2`, `excluded`, `holding_flag`, `prioritaets_score desc`.

## Join-Pfade

- `pages.company_id`, `dossiers.company_id`, `signals.company_id`, `scores.company_id`,
  `outreach.company_id`, `outreach_calls.company_id`, `job_matches.company_id` → **`companies.id`**.
- Self: `companies.dup_of → companies.id`.

## Invarianten & Fallstricke

- **Zielliste-Hoheit liegt beim os-Import.** Neuer Firmenbestand kommt von dort; c6 legt
  keine companies-Zeilen an (externe Job-Leads laufen als `job_matches` mit company_id NULL).
- `gf_alter` ist nur teilweise gefüllt. Nach jedem Anreicherungs-Schub `c6_jobsignale.py --rematch` + `--reprio`.
- `raw` ist die Quelle der Wahrheit für nicht-getypte Felder — bei Zweifeln dort nachsehen.
- 37 Spalten (Drift-Check-Bezug).
