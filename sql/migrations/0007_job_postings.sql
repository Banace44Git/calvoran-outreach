-- Job-Signal-Modul Migration 0007: BA-Jobsuche als Nachfolge-Signal.
-- Idee: Sucht eine Zielfirma per Stellenanzeige einen GF / eine kaufmännische Leitung /
-- zweite Führungsebene, deutet das bei Inhabern 58+ auf Übergabevorbereitung oder
-- gescheiterte interne Nachfolge — zeitkritisches Outreach-Signal.
-- (a) job_postings: Roh-Anzeigen der BA-Jobsuche-API, Dedup über refnr. Bewusst NICHT
--     raw_jobs (gehört zur toten Apify-Pipeline, andere Semantik: search_query-Pflicht,
--     apify_run_id, leads-Kopplung). erste/letzte_sichtung macht die Anzeigen-Laufzeit
--     sichtbar (lang offen = Besetzungsschwierigkeit) ohne Dubletten.
-- (b) job_matches: Abgleich Anzeige <-> calvoran.companies (Name+PLZ, exakt/fuzzy).
--     Eigene Tabelle statt signals, weil c3_extract signals je Firma per delete+insert
--     überschreibt — BA-Treffer würden dort beim nächsten c3-Lauf verschwinden.
--     Lebenszyklus: neu -> gesichtet/relevant/irrelevant -> outreach (Phase B).
-- Additiv, idempotent (create … if not exists). Im Supabase SQL-Editor ausführen.

create table if not exists calvoran.job_postings (
    id                 uuid primary key default gen_random_uuid(),
    refnr              text not null unique,        -- BA-Referenznummer = Dedup-Anker
    titel              text not null,
    beruf              text,
    arbeitgeber        text not null,
    plz                text,                        -- aus arbeitsort (Match-Key)
    ort                text,
    keyword            text not null,               -- welcher Suchbegriff traf (KPI je Keyword)
    veroeffentlicht_am date,
    erste_sichtung     timestamptz not null default now(),
    letzte_sichtung    timestamptz not null default now(),  -- zuletzt in der API gesehen
    raw                jsonb not null
);
create index if not exists job_postings_plz_idx on calvoran.job_postings (plz);
create index if not exists job_postings_veroeff_idx
    on calvoran.job_postings (veroeffentlicht_am desc);

create table if not exists calvoran.job_matches (
    id           uuid primary key default gen_random_uuid(),
    posting_id   uuid not null references calvoran.job_postings(id) on delete cascade,
    company_id   uuid not null references calvoran.companies(id) on delete cascade,
    match_stufe  text not null
                 check (match_stufe in ('exakt','fuzzy','fuzzy_grenzfall','region')),
    match_score  numeric,                            -- rapidfuzz 0-100 (100 = exakt)
    prio         text not null check (prio in ('hoch','mittel','niedrig','unbekannt')),
    status       text not null default 'neu'
                 check (status in ('neu','gesichtet','relevant','irrelevant','outreach')),
    status_notiz text,
    reviewed_at  timestamptz,
    created_at   timestamptz not null default now(),
    unique (posting_id, company_id)                  -- idempotenter Re-Run
);
create index if not exists job_matches_status_idx on calvoran.job_matches (status);
create index if not exists job_matches_company_idx on calvoran.job_matches (company_id);

-- Grants für die neuen Tabellen (PostgREST/service_role), analog apply_all.sql.
grant all on all tables in schema calvoran to anon, authenticated, service_role;
grant all on all sequences in schema calvoran to anon, authenticated, service_role;
