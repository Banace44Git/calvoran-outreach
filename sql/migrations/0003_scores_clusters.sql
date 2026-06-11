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
