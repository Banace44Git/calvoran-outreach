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
