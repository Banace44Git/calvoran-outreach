-- Outreach-Pipeline Migration 0006: CRM-Nachverfolgung Stufe 1 (Brief + Anruf).
-- (a) outreach_calls: strukturiertes Anruf-Log — mehrere Versuche je Firma + Wiedervorlage.
--     Bewusst eigene Tabelle (nicht outreach.notes), weil Nachtelefonieren n Versuche mit
--     je eigenem Ausgang/Datum/Wiedervorlage hat. follow_up_at füttert später den geplanten
--     Hermes-Cron 'wiedervorlagen' (Phase 6).
-- (b) Unique-Index auf outreach(company_id, channel, wave): macht das Brief-Versand-Tracking
--     idempotent (Backfill/c5 erzeugen keine Dubletten). NULL-company_id-Zeilen (Job-Scraping
--     über lead_id) bleiben unberührt, weil NULLs in Unique-Indizes als distinct gelten.
-- Additiv, idempotent (create … if not exists). Im Supabase SQL-Editor ausführen.

create table if not exists calvoran.outreach_calls (
    id            uuid primary key default gen_random_uuid(),
    company_id    uuid not null references calvoran.companies(id) on delete cascade,
    outreach_id   uuid references calvoran.outreach(id) on delete set null,  -- der Brief, falls verknüpfbar
    called_at     timestamptz not null default now(),
    outcome       text not null
                  check (outcome in ('nicht_erreicht','gesprochen','rueckruf_vereinbart',
                                     'termin','kein_interesse','nicht_zustaendig','falsche_nummer')),
    follow_up_at  timestamptz,            -- Wiedervorlage (NULL = keine)
    notes         text,
    created_at    timestamptz not null default now()
);
create index if not exists outreach_calls_company_idx on calvoran.outreach_calls (company_id);
create index if not exists outreach_calls_followup_idx on calvoran.outreach_calls (follow_up_at)
    where follow_up_at is not null;

-- Idempotenz fürs Versand-Tracking: genau eine Zeile je (company_id, channel, wave).
create unique index if not exists outreach_company_channel_wave_uidx
    on calvoran.outreach (company_id, channel, wave);

-- Grants für die neue Tabelle (PostgREST/service_role), analog apply_all.sql.
grant all on all tables in schema calvoran to anon, authenticated, service_role;
grant all on all sequences in schema calvoran to anon, authenticated, service_role;
