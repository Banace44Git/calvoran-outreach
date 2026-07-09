-- Job-Signal-Modul Migration 0008: externe Signal-Leads (Firma nicht in Zielliste).
-- Jo sichtet im Tab »Job-Signale TEMP« Anzeigen OHNE Zielliste-Match und kontaktiert
-- einzelne direkt (z.B. Bezirksverein für soziale Rechtspflege). Damit solche Anzeigen
-- in die reguläre Arbeitsliste »rüberwandern«, bekommen sie eine job_matches-Zeile
-- mit company_id NULL und match_stufe 'extern' — kein companies-Eintrag nötig
-- (die Zielliste gehört dem os-Import, hier wird nichts hineingeschrieben).
-- Additiv, idempotent. Im Supabase SQL-Editor ausführen.

alter table calvoran.job_matches alter column company_id drop not null;

alter table calvoran.job_matches drop constraint if exists job_matches_match_stufe_check;
alter table calvoran.job_matches add constraint job_matches_match_stufe_check
    check (match_stufe in ('exakt','fuzzy','fuzzy_grenzfall','region','extern'));

-- Unique (posting_id, company_id) behandelt NULLs als distinct -> eigener partieller
-- Index, damit je Anzeige höchstens EIN externer Signal-Lead existiert.
create unique index if not exists job_matches_extern_uidx
    on calvoran.job_matches (posting_id) where company_id is null;
