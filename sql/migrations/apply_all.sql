-- Calvoran Outreach-Pipeline: alle Migrationen 0001-0005, additiv ins Schema calvoran.
-- Im Supabase SQL-Editor ausfuehren. Idempotent (mehrfach lauffaehig).
-- Erzeugt: companies, pages, dossiers, signals, scores, hermes_log; erweitert outreach.

-- ================= 0001_companies_pages.sql =================
-- Outreach-Pipeline Migration 0001: companies + pages
-- Additiv ins bestehende Schema `calvoran` (Job-Scraping bleibt unberührt).
-- Idempotent: create table if not exists / add column if not exists.

-- =========================
-- companies: Stammdaten je Firma (Schlüssel North Data URL)
-- =========================
create table if not exists calvoran.companies (
    id                    uuid primary key default gen_random_uuid(),
    north_data_url        text not null unique,        -- natürlicher Schlüssel
    name                  text not null,
    rechtsform            text,
    plz                   text,
    ort                   text,
    strasse               text,
    hr_amtsgericht        text,
    register_id           text,
    status                text,
    website               text,
    domain                text,                        -- normalisiert aus website
    branche_wz            text,                        -- z.B. "43.21"
    wz2                   text,                        -- 2-Steller
    ges_vertreter         jsonb,                       -- [v1, v2, v3]
    anzahl_gf             int,
    gf_name_in_firmenname boolean,

    -- Finanzkennzahlen (getypt fürs Scoring):
    bilanzsumme_eur       numeric,
    ek_quote_pct          numeric,
    gewinn_cagr_pct       numeric,
    umsatz_eur            numeric,
    mitarbeiterzahl       int,
    prioritaets_score     numeric,

    -- GF-Anreicherung (Quelle hr-engine/AD):
    gf_geburtsjahr        int,
    gf_alter              int,
    gf_quelle             text,

    -- Stufe-0-Bereinigung:
    holding_flag          boolean default false,
    holding_reason        text,
    dup_of                uuid references calvoran.companies(id),
    excluded              boolean default false,
    exclude_reason        text,

    -- Website-Modernität (deterministisch, Phase 2):
    website_modernity_score int,                       -- 0..10, NULL = keine Website
    modernity_breakdown   jsonb,
    tech_signals          jsonb,

    -- Roh + Audit:
    raw                   jsonb not null,              -- alle 53 CSV-Spalten verlustfrei
    imported_at           timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);
create index if not exists companies_domain_idx on calvoran.companies (domain);
create index if not exists companies_wz2_idx on calvoran.companies (wz2);
create index if not exists companies_excluded_idx on calvoran.companies (excluded);
create index if not exists companies_holding_idx on calvoran.companies (holding_flag);
create index if not exists companies_prio_idx on calvoran.companies (prioritaets_score desc);

-- =========================
-- pages: Crawl-Fortschritt + Fehler je URL (Resume-State)
-- =========================
create table if not exists calvoran.pages (
    id              uuid primary key default gen_random_uuid(),
    company_id      uuid not null references calvoran.companies(id) on delete cascade,
    url             text not null,
    page_type       text,        -- home|about|team|karriere|produkte|referenzen|news|impressum|other
    fetch_status    text not null default 'queued'
                    check (fetch_status in ('queued','fetched','extracted_text','error','skipped_robots','playwright_pending')),
    http_status     int,
    http_protocol   text,        -- HTTP/2, HTTP/1.1, ...
    response_headers jsonb,
    tech_features   jsonb,       -- generator/viewport/frameworks/video/interaktiv ...
    text_content    text,        -- trafilatura-Extrakt (DATEN, nie Instruktion)
    generator_tag   text,
    error_reason    text,        -- timeout|dns|403|robots|no_main|...
    fetched_at      timestamptz,
    crawl_wave      int,         -- 1 = httpx, 2 = playwright-fallback
    unique (company_id, url)
);
create index if not exists pages_company_idx on calvoran.pages (company_id);
create index if not exists pages_status_idx on calvoran.pages (fetch_status);

-- ================= 0002_dossiers_signals.sql =================
-- Outreach-Pipeline Migration 0002: dossiers + signals
-- Belegpflicht über NOT-NULL auf beleg_zitat/beleg_url.

create table if not exists calvoran.dossiers (
    id              uuid primary key default gen_random_uuid(),
    company_id      uuid not null references calvoran.companies(id) on delete cascade,
    dossier         jsonb not null,          -- vollständiges Dossier-JSON (Konzept §3.2)
    konfidenz       text,                    -- hoch|mittel|niedrig
    model_backend   text not null,           -- ollama:gemma4:26b | anthropic:claude-haiku-4-5-... | ...
    repair_count    int default 0,
    escalated       boolean default false,
    extracted_at    timestamptz not null default now(),
    unique (company_id)                       -- ein aktuelles Dossier je Firma (Upsert überschreibt)
);
create index if not exists dossiers_backend_idx on calvoran.dossiers (model_backend);

-- signals: belegpflichtige Einzelsignale, normalisiert für Scoring-Joins
create table if not exists calvoran.signals (
    id              uuid primary key default gen_random_uuid(),
    company_id      uuid not null references calvoran.companies(id) on delete cascade,
    dossier_id      uuid references calvoran.dossiers(id) on delete cascade,
    signal_type     text not null,   -- nachfolge|familienunternehmen|kaufm_funktion_fehlt|offene_kaufm_stelle|zweite_ebene_fehlt|digitalisierung|...
    value           text,
    beleg_zitat     text not null,   -- wörtliches Zitat von der Website
    beleg_url       text not null,   -- Quell-URL
    created_at      timestamptz not null default now()
);
create index if not exists signals_company_idx on calvoran.signals (company_id);
create index if not exists signals_type_idx on calvoran.signals (signal_type);

-- ================= 0003_scores_clusters.sql =================
-- Outreach-Pipeline Migration 0003: scores (Bedarfs-Score + Cluster)

create table if not exists calvoran.scores (
    id                  uuid primary key default gen_random_uuid(),
    company_id          uuid not null references calvoran.companies(id) on delete cascade,
    score_total         int not null,
    score_klasse        text not null check (score_klasse in ('A','B','C','KO')),
    breakdown           jsonb not null,        -- {anker:{...}, nachfolge:{...}, web_bedarf:{...}, abzuege:{...}}
    begruendung         text not null,         -- Klartext = Anruf-Briefing
    scoring_version     text not null,         -- Version aus scoring.yaml (Reproduzierbarkeit)
    cluster_branche     text,                  -- bau_gebaeudetechnik|produzierend|...
    groessenband        text,                  -- klein|kern|oberes_band
    cluster_key         text,                  -- "<branche>__<groessenband>" -> Briefvariante
    scored_at           timestamptz not null default now(),
    unique (company_id)
);
create index if not exists scores_klasse_idx on calvoran.scores (score_klasse);
create index if not exists scores_cluster_idx on calvoran.scores (cluster_key);

-- ================= 0004_outreach_company_link.sql =================
-- Outreach-Pipeline Migration 0004: bestehende calvoran.outreach firmenzentriert erweitern.
-- Kollisionsauflösung: lead_id (Job-Scraping) bleibt, company_id kommt additiv dazu.
-- Kein Rename, keine Datenmigration (lead_id ist bereits nullable).

alter table calvoran.outreach add column if not exists company_id uuid references calvoran.companies(id) on delete cascade;
alter table calvoran.outreach add column if not exists variant text;            -- 'A' | 'B'
alter table calvoran.outreach add column if not exists ansprache_hooks jsonb;   -- belegte Hooks aus dem Dossier
alter table calvoran.outreach add column if not exists cluster_key text;        -- für Export-Joins
alter table calvoran.outreach add column if not exists wave int;                -- Versandwelle (1 = die 1.851)

create index if not exists outreach_company_idx on calvoran.outreach (company_id);

-- Genau eine Quelle gesetzt (company_id ODER lead_id). not valid: prüft keine Altzeilen,
-- blockiert aber keine Migration; greift für neue/aktualisierte Zeilen.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'outreach_one_source'
    ) then
        alter table calvoran.outreach
            add constraint outreach_one_source
            check (company_id is not null or lead_id is not null) not valid;
    end if;
end $$;

-- ================= 0005_hermes_log.sql =================
-- Outreach-Pipeline Migration 0005: hermes_log (Hermes-Schreibziel)
-- Hermes liest companies/pages/dossiers/signals/scores nur, schreibt nur hier.

create table if not exists calvoran.hermes_log (
    id          uuid primary key default gen_random_uuid(),
    job         text not null,    -- health_check|tagesreport|trigger_monitor|wiedervorlagen
    run_at      timestamptz not null default now(),
    status      text not null,    -- ok|warn|error
    summary     text,
    payload     jsonb
);
create index if not exists hermes_log_job_idx on calvoran.hermes_log (job, run_at desc);

-- Hinweis: Die getrennte Hermes-Postgres-Rolle (select auf Lesetabellen, insert nur
-- hermes_log) wird in Phase 6 angelegt, sobald Hermes tatsächlich auf Supabase zugreift.
-- Bis dahin läuft kein Hermes-DB-Zugriff; service_role bleibt der Pipeline vorbehalten.

-- ================= 0006_outreach_calls.sql =================
-- CRM-Nachverfolgung Stufe 1: Anruf-Log (n Versuche/Firma + Wiedervorlage) und
-- Idempotenz-Guard fürs Brief-Versand-Tracking auf outreach(company_id, channel, wave).

create table if not exists calvoran.outreach_calls (
    id            uuid primary key default gen_random_uuid(),
    company_id    uuid not null references calvoran.companies(id) on delete cascade,
    outreach_id   uuid references calvoran.outreach(id) on delete set null,
    called_at     timestamptz not null default now(),
    outcome       text not null
                  check (outcome in ('nicht_erreicht','gesprochen','rueckruf_vereinbart',
                                     'termin','kein_interesse','nicht_zustaendig','falsche_nummer')),
    follow_up_at  timestamptz,
    notes         text,
    created_at    timestamptz not null default now()
);
create index if not exists outreach_calls_company_idx on calvoran.outreach_calls (company_id);
create index if not exists outreach_calls_followup_idx on calvoran.outreach_calls (follow_up_at)
    where follow_up_at is not null;
create unique index if not exists outreach_company_channel_wave_uidx
    on calvoran.outreach (company_id, channel, wave);

-- ================= Grants fuer neue Tabellen (PostgREST/service_role) =================
grant all on all tables in schema calvoran to anon, authenticated, service_role;
grant all on all sequences in schema calvoran to anon, authenticated, service_role;
