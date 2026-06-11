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
