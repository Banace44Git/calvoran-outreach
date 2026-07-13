---
type: supabase table
schema: calvoran
title: job_matches
description: Abgleich BA-Anzeige ⨯ Zielliste-Firma (Name+PLZ). company_id NULLABLE — externe Signal-Leads (Firma nicht in Zielliste) haben company_id NULL und match_stufe 'extern'.
written_by: [c6_jobsignale, dashboard (job-signale, job-signale temp)]
read_by: [c6_jobsignale, dashboard]
source_migration: [0007, 0008, 0009]
tags: [ba-jobsuche, match, signal-lead, sichtung]
---

# job_matches

Verbindet eine [job_postings](job_postings.md)-Anzeige mit einer [companies](companies.md)-Firma.
Eigene Tabelle statt [signals](signals.md), weil c3 signals je Firma überschreiben würde.
Zwei Ausprägungen: **regulärer Match** (company_id gesetzt) und **externer Signal-Lead**
(company_id NULL, `match_stufe='extern'`) für Anzeigen ohne Zielliste-Treffer.

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `posting_id` | uuid | nein | FK → `job_postings.id` (cascade). |
| `company_id` | uuid | **ja** | FK → `companies.id` (cascade). **NULL = externer Signal-Lead** (0008). |
| `match_stufe` | text | nein | CHECK exakt\|fuzzy\|fuzzy_grenzfall\|region\|extern. |
| `match_score` | numeric | ja | rapidfuzz 0–100 (100 = exakt). |
| `prio` | text | nein | CHECK hoch\|mittel\|niedrig\|unbekannt (aus GF-Alter). |
| `status` | text | nein | default 'neu'; CHECK (s.u.) — Sichtungs-Lebenszyklus. |
| `status_notiz` | text | ja | Freitext aus der Sichtung. |
| `reviewed_at` | timestamptz | ja | Gesetzt, sobald Status ≠ 'neu'. |
| `created_at` | timestamptz | nein | default now(). |

## Constraints & Indizes

- **PK** `id`.
- **Unique** `(posting_id, company_id)` — idempotenter Re-Run (NULLs gelten als distinct!).
- **Partieller Unique** `job_matches_extern_uidx` auf `(posting_id) WHERE company_id IS NULL` (0008) — höchstens ein externer Lead je Anzeige.
- **CHECK** `match_stufe IN ('exakt','fuzzy','fuzzy_grenzfall','region','extern')` (0008).
- **CHECK** `status IN ('neu','gesichtet','relevant','irrelevant','outreach','abgelehnt')` (0009).
- FK `posting_id → job_postings.id` (cascade), `company_id → companies.id` (cascade).
- Indizes: `status`, `company_id`.

## Join-Pfade

- `job_matches.posting_id → job_postings.id`, `job_matches.company_id → companies.id` (NULLABLE).

## Invarianten & Fallstricke

- **company_id NULLABLE** ist Absicht (0008): externe Leads aus dem TEMP-Tab wandern ohne
  companies-Eintrag in die Arbeitsliste. Der reguläre Unique `(posting_id, company_id)`
  greift bei NULL nicht — dafür der partielle Unique.
- **Status-Lebenszyklus:** `neu → gesichtet/relevant/irrelevant → outreach`; `abgelehnt` (0009)
  ist ein eigener Terminalzustand (kontaktiert, aber abgesagt) ≠ irrelevant (Match-Müll) ≠ outreach (aktiv).
- Dashboard schreibt `status`/`status_notiz`/`reviewed_at`; das »irr.«-Häkchen setzt status='irrelevant' sofort, im TEMP-Tab per INSERT eines externen irrelevant-Leads.
- `prio` wird über `--reprio` aus dem aktuellen `companies.gf_alter` neu gesetzt.
- 10 Spalten (Drift-Check-Bezug).
