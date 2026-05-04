-- calvoran-outreach: Lead-Generierung über Stellenanzeigen
-- Schema: calvoran (im selben Supabase-Projekt wie haufe-scraper)

create schema if not exists calvoran;

-- PostgREST/Supabase: Rollen brauchen explizit Zugriff auf neues Schema
grant usage on schema calvoran to anon, authenticated, service_role;
grant all on all tables in schema calvoran to anon, authenticated, service_role;
grant all on all sequences in schema calvoran to anon, authenticated, service_role;
alter default privileges in schema calvoran grant all on tables to anon, authenticated, service_role;
alter default privileges in schema calvoran grant all on sequences to anon, authenticated, service_role;

-- =========================
-- 1. raw_jobs: Rohdaten je Apify-Run
-- =========================
create table if not exists calvoran.raw_jobs (
    id              uuid primary key default gen_random_uuid(),
    source          text not null check (source in ('indeed', 'stepstone', 'arbeitsagentur')),
    external_id     text,
    search_query    text not null,
    apify_run_id    text,
    payload         jsonb not null,
    scraped_at      timestamptz not null default now(),
    unique (source, external_id)
);

create index if not exists raw_jobs_source_idx on calvoran.raw_jobs (source);
create index if not exists raw_jobs_scraped_at_idx on calvoran.raw_jobs (scraped_at desc);

-- =========================
-- 2. leads: dedupliziert + gefiltert
-- =========================
create table if not exists calvoran.leads (
    id                      uuid primary key default gen_random_uuid(),
    raw_job_id              uuid references calvoran.raw_jobs(id) on delete cascade,
    source                  text not null,
    external_id             text,

    firmenname              text,
    stellentitel            text,
    standort                text,
    plz                     text,
    remote                  boolean default false,
    link                    text,

    ansprechpartner         text,
    ansprechpartner_email   text,
    ansprechpartner_phone   text,

    posted_date             date,
    description             text,

    dedupe_key              text,
    in_target_cities        boolean default false,
    excluded                boolean default false,
    exclude_reason          text,

    contact_extracted_at    timestamptz,

    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

create index if not exists leads_dedupe_key_idx on calvoran.leads (dedupe_key);
create index if not exists leads_excluded_idx on calvoran.leads (excluded);
create index if not exists leads_in_target_cities_idx on calvoran.leads (in_target_cities);
create index if not exists leads_firmenname_idx on calvoran.leads (lower(firmenname));

-- =========================
-- 3. outreach: Ansprache-Tracking (für später)
-- =========================
create table if not exists calvoran.outreach (
    id              uuid primary key default gen_random_uuid(),
    lead_id         uuid references calvoran.leads(id) on delete cascade,
    channel         text check (channel in ('email', 'phone', 'linkedin', 'letter')),
    status          text check (status in ('queued', 'sent', 'opened', 'replied', 'no_response', 'rejected', 'won')),
    subject         text,
    body            text,
    sent_at         timestamptz,
    response_at     timestamptz,
    notes           text,
    created_at      timestamptz not null default now()
);

create index if not exists outreach_lead_idx on calvoran.outreach (lead_id);
create index if not exists outreach_status_idx on calvoran.outreach (status);
