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
