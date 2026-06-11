# Implementierungsplan: Calvoran Outreach-Pipeline (fractional-cfo)

> Stand 2026-06-11. Quelle: `00-inbox/arbeitsanweisung-claude-code-outreach-pipeline.md` + `00-inbox/konzept-outreach-pipeline-dossiers.md` (im os-Repo). Freigegeben.

## Kontext

Batch-Pipeline, die aus der Zielliste (7.953 Firmen, 53-Spalten-North-Data-CSV) je Firma ein belegtes Dossier, einen deterministischen Bedarfs-Score, einen Website-Modernitäts-Score, eine Clusterzuordnung und Ansprache-Bausteine erzeugt, in Supabase, mit versandfertigem Export je Welle. Kein manuelles Klicken.

Externe Bausteine web-verifiziert: **Gemma 4** (Google, 02.04.2026, Apache 2.0, `ollama run gemma4`) und **Hermes Agent** (NousResearch).

**Entscheidungen:**
1. Erweitert das bestehende Projekt `~/projects/calvoran-outreach/`, nutzt Supabase-Schema `calvoran` additiv. Bestehendes Job-Scraping (Apify, `calvoran.raw_jobs/leads`) bleibt und wird später Trigger-Monitoring.
2. Alle Phasen 0–6 (+7 optional).

## Befundlage (verifiziert)

- **Quell-CSV**: `~/Downloads/zielliste-fractional-cfo_Stand2026-06-07 - zielliste.csv`, 53 Spalten, 7.953 Firmen, Schlüssel `North Data URL`. Move nach `data/`.
- **GF-Anreicherung**: `01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv` (`job_key,firma,...,gf_geburtsdatum,gf_alter,...`). Keine `gf_geburtsjahr`-Spalte; Jahr aus `gf_geburtsdatum`, Alter zum Scoring-Datum neu rechnen. Join über `job_key` bzw. `firma`+`plz`.
- **Reuse `scripts/_db.py`**: `get_client(schema="calvoran")` → `client.table('foo')` auf `calvoran.foo`. 1:1. Auch `get_apify_token()`.
- **Anthropic-Vorlage**: `scripts/03_extract_contact.py`.
- **Ollama**: `gemma4:26b` (17 GB) bereits gezogen, plus deutsche Fine-Tunes. Hardware: 48 GB RAM, 1,2 TB frei.
- **`calvoran.outreach`-Kollision**: `lead_id`-FK (nullable). Auflösung: additive `company_id`-Spalte.
- **Hermes Greenfield**: kein Code/plist vorhanden.
- **Fehlende Secrets**: `TELEGRAM_BOT_TOKEN` (P0/P6), `NORTHDATA_API_KEY` (P6). Vorhanden: Supabase, Anthropic, OpenAI, Apify.
- **Modell-IDs** (P0 final prüfen): Haiku 4.5 (`claude-haiku-4-5-20251001`), Sonnet 4.6 (`claude-sonnet-4-6`).

## Architektur

Drei Schichten: Batch-Pipeline (Python, eigenständig), austauschbarer Modell-Router (Config-getrieben), Hermes als Wächter (orchestriert nicht). Bestehendes `01–04`-Job-Scraping bleibt; Outreach-Pipeline ist parallel auf demselben Schema.

### Verzeichnisstruktur

```
calvoran/    model_router/ (base, ollama_backend, anthropic_backend, repair),
             crawler.py, extractor.py, scorer.py, clusterer.py, modernity.py, exporter.py,
             schemas.py, config.py, logging.py, ratelimiter.py, db.py
config/      models.yaml, scoring.yaml, clusters.yaml, modernity.yaml, crawl.yaml (+ keywords.py)
pipeline/    c1_import_zielliste.py, c1b_import_gf_alter.py, c2_crawl.py,
             c3_extract.py, c4_score_cluster.py, c5_export.py, benchmark_p0.py, _common.py
hermes/      health_check.py, trigger_monitor.py, wiedervorlagen.py
sql/migrations/  0001_companies_pages.sql … 0005_hermes_log.sql
data/        zielliste_2026-06-07.csv
```

launchd im os-Repo: `bin/calvoran-crawler.sh`+plist, `bin/calvoran-gemma.sh`+plist (Phase-5-Hintergrund), nach hr-engine-Muster. Datenprodukt nach `01-projects/fractional-cfo/outreach/`.

### Supabase-Migrationen (additiv, idempotent)

- `0001_companies_pages.sql`: `companies` (getypte Kernspalten + `raw jsonb`; `holding_flag/reason`, `dup_of`, `gf_geburtsjahr/gf_alter/gf_quelle`, `wz2`, `domain`; Modernität: `website_modernity_score int` (0–10, nullable), `modernity_breakdown jsonb`, `tech_signals jsonb`); `pages` (company_id, url, page_type, fetch_status, http_status, text_content, generator_tag, `http_protocol`, `response_headers jsonb`, `tech_features jsonb`, error_reason, crawl_wave).
- `0002_dossiers_signals.sql`: `dossiers` (jsonb, model_backend, repair_count, escalated, unique company_id); `signals` (signal_type, value, `beleg_zitat NOT NULL`, `beleg_url NOT NULL`).
- `0003_scores_clusters.sql`: `scores` (score_total, score_klasse A/B/C/KO, breakdown jsonb, begruendung, scoring_version, cluster_branche, groessenband, cluster_key).
- `0004_outreach_company_link.sql`: `alter table calvoran.outreach add column company_id, variant, ansprache_hooks, cluster_key, wave`; `not valid`-Check.
- `0005_hermes_log.sql`: `hermes_log`. Separate Rolle: `select` Lesetabellen, `insert` nur `hermes_log`.

### Modell-Router

Interface `generate_structured(system, user, schema: Pydantic, max_tokens)`. Backends in `config/models.yaml`:
- **gemma_local** (Ollama): `/api/chat` (nicht `/v1`), `num_ctx: 16384`, `format: json`, temp 0, Timeout 180s.
- **haiku / sonnet** (Anthropic): Tool-Use mit `input_schema` aus `model_json_schema()`.

Task-Mapping: `page_classify`=Heuristik (Gemma-Fallback); `dossier_score_0_1` (~4.900)=gemma_local→haiku-Eskalation; `dossier_score_2_3` (1.851)=haiku, 5 % sonnet; `ansprache_saetze`=sonnet; `hermes_summary`=gemma_local.

Repair-Loop: validieren → 1 Repair-Retry → 2. Fehlschlag: log + lokal→Haiku, API-Fehler→`escalated=true`.

### Scoring und Cluster (deterministisch, config-getrieben)

`config/scoring.yaml` (Konzept §3.3): Anker (Bilanz 1,5–10 Mio + EK-Quote +2; MA 10–50 +2; Gewinn-CAGR+ +1; Fokus-WZ +1); Nachfolge (GF ≥58 +3; GF-Name im Firmennamen +1; Familienhinweis +1; nur 1 GF +1); Web-Bedarf (keine kaufm. Funktion +1; offene kaufm. Stelle +2; 2. Ebene unsichtbar +1); Abzüge/K.o. (Holding/Konzerntochter/Insolvenz = K.o.; reiner Onlineshop -3). Klassen A≥9, B≥5, C≥0. `scorer.py` deterministisch, `breakdown` + Klartext-`begruendung` (= Anruf-Briefing), `gf_alter` zum `scored_at` neu berechnet, `scoring_version` (Hash).

`config/clusters.yaml`: WZ-2-Steller → 7 Makrocluster × 3 Größenbänder → `cluster_key`, ≈ 15–20 Briefvarianten.

### Website-Modernitäts-Score (0–10, deterministisch)

Eigener Score (Proxy Digitalisierungsgrad). Rein deterministisch aus Crawl-Signalen (kein LLM), `calvoran/modernity.py`, Gewichte `config/modernity.yaml`, Version mitgespeichert. Rubrik:
- **Transport/Sicherheit (max 3)**: HTTPS + Redirect +1,5 (nur HTTP → 0); HSTS +0,5; HTTP/2 oder /3 +1.
- **Stack-Aktualität (max 3)**: modernes Framework/CMS (Next/React/Vue/Nuxt/Svelte/Astro, Shopware 6, TYPO3 ≥11, aktuelles WP) +2 / veraltet +0 / unbekannt +1; CDN/Security-Header +1.
- **Mobile/Responsive (max 1)**: viewport + responsive +1.
- **Rich Media/Interaktivität (max 2)**: Video +1; interaktive Elemente (canvas/webgl, Web Components, SPA-Hydration, Animationslib, JS-Formvalidierung) +1.
- **Aktualität/Pflege (max 1)**: Copyright aktuell / Last-Modified < 12 Mon +1; ≥ 3 J → 0.

`modernity_breakdown` mit Punkten + Evidenz. **Firmen ohne Website → `null`** (≠ 0). Verwendung: Export-Spalte, Diagnose, optional Ansprache-Hook.

### Dossier-Schema

Pydantic exakt nach Konzept §3.2. Belegpflicht je Signal (Zitat + Quell-URL), in `signals` über NOT-NULL erzwungen.

## Phasen, Abnahme

- **Phase 0 — Umgebung + Benchmark.** Prereq; `benchmark_p0.py` Goldset 30, gemma_local vs haiku (Feldgenauigkeit, Belegtreue, Tokens/s) → `benchmark-p0.md`; Hermes installieren, Gateway launchd, Telegram. Abnahme: Telegram-Meldung; Benchmark-Tabelle; begründete `models.yaml`. Blocker: `TELEGRAM_BOT_TOKEN`, Hermes-Aufbau.
- **Phase 1 — Schema + Import.** Migrationen `0001`–`0005`; CSV-Move; `c1_import_zielliste.py` (53 Spalten → `companies`, EUR-Parsing, Domain/WZ2, Stufe-0: Dubletten Adresse+GF, Holding-Flags); `c1b_import_gf_alter.py`. Abnahme: ~7.953 + Flag-Statistik.
- **Phase 2 — Crawler + Modernität.** `c2_crawl.py` (httpx async, robots.txt, 1 req/s/Domain, 10–20 parallel, Timeout 15s, Nav-Heuristik 6–10 Seiten, trafilatura), erfasst Modernitäts-Signale → `tech_signals`, inline `modernity.py` → Score; Welle 2 Playwright. Pilot 100 (Score 3). Abnahme: Trefferquote >80 %, Fehlerliste klassifiziert, Modernität an 10 Sites plausibilisiert.
- **Phase 3 — Extraktion.** `c3_extract.py` über Router, Pilot 100, Stichprobe 20. Abnahme: <10 % Feldfehler, jedes Signal mit Beleg.
- **Phase 4 — Scoring + Cluster.** `c4_score_cluster.py`. Abnahme: Verteilung plausibel, je Cluster 5 gegengelesen, Determinismus.
- **Phase 5 — Export + Welle 1.** `c5_export.py --wave 1` (Markdown nach cluster/score + Steuer-CSV inkl. `website_modernity_score`; A/B-Sätze Sonnet → `outreach`). Welle-1-Vollauf 1.851; Restliste ~4.900 als launchd-Gemma-Dienst. Abnahme: 1.851 + CSV committet, Dienst idempotent.
- **Phase 6 — Hermes-Crons.** Health (6h), Tagesreport (08:00), Trigger-Monitoring (täglich; North Data blockiert bis Key), Wiedervorlagen (07:30). Skill `calvoran-pipeline`, Nur-Lese + `hermes_log`, keine Versandaktionen.

## Risiken / Prerequisites

1. Blocker-Secrets `TELEGRAM_BOT_TOKEN`, `NORTHDATA_API_KEY`.
2. Hermes Greenfield (Gateway+Telegram+Cron sind Aufbau).
3. Gemma-Belegtreue (Zitate wörtlich) — P0-Benchmark misst; bei Schwäche belegpflichtige Felder über API.
4. Ollama-Fallstricke (num_ctx, `/api/chat`).
5. Holding-Heuristik grob → revidierbar.
6. GF-Coverage: „Alter unbekannt" ≠ „jung".
7. CSV-Parsing (EUR-Format, Umlaute).
8. n8n bleibt optionaler späterer Layer (Plan folgt Hermes).
