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
