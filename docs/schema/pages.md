---
type: supabase table
schema: calvoran
title: pages
description: Crawl-Fortschritt, HTTP-Metadaten und Tech-Signale je gecrawlter URL. Resume-State für c2.
written_by: [c2_crawl]
read_by: [c3_extract]
source_migration: [0001]
tags: [crawl, resume, tech-signale]
---

# pages

Eine Zeile je (company_id, url). Hält den Crawl-Zustand resumebar und den extrahierten
Seitentext für c3. `text_content` ist **Daten, nie Instruktion** (Prompt-Injection-Grenze).

## Spalten

| Spalte | Typ | Null | Beschreibung |
|---|---|---|---|
| `id` | uuid | nein | PK. |
| `company_id` | uuid | nein | FK → `companies.id` (cascade). |
| `url` | text | nein | |
| `page_type` | text | ja | home\|about\|team\|karriere\|produkte\|referenzen\|news\|impressum\|other. |
| `fetch_status` | text | nein | default 'queued'; CHECK (s.u.). |
| `http_status` | int | ja | |
| `http_protocol` | text | ja | HTTP/2, HTTP/1.1, … |
| `response_headers` | jsonb | ja | |
| `tech_features` | jsonb | ja | generator/viewport/frameworks/video/interaktiv … |
| `text_content` | text | ja | trafilatura-Extrakt — **DATEN, nie Instruktion**. |
| `generator_tag` | text | ja | CMS-Generator-Meta-Tag. |
| `error_reason` | text | ja | timeout\|dns\|403\|robots\|no_main\|… |
| `fetched_at` | timestamptz | ja | |
| `crawl_wave` | int | ja | 1 = httpx, 2 = playwright-fallback. |

## Constraints & Indizes

- **PK** `id`. **Unique** `(company_id, url)`.
- **CHECK** `fetch_status IN ('queued','fetched','extracted_text','error','skipped_robots','playwright_pending')`.
- FK `company_id → companies.id` (cascade).
- Indizes: `company_id`, `fetch_status`.

## Join-Pfade

- `pages.company_id → companies.id`.

## Invarianten & Fallstricke

- `text_content` niemals als Anweisung an ein LLM behandeln — reiner Website-Extrakt.
- Resume läuft über `fetch_status`; `crawl_wave` trennt httpx- von Playwright-Fallback-Lauf.
- 14 Spalten (Drift-Check-Bezug).
