-- Job-Signal-Modul Migration 0009: Terminal-Status 'abgelehnt'.
-- »Kontaktiert, aber abgesagt« (z.B. Baldus Medical: nicht interessiert an externer
-- Besetzung) ist weder 'irrelevant' (Match war Müll) noch 'outreach' (aktiv) —
-- eigener Endzustand, damit Briefquote/Funnel sauber bleiben.
-- Additiv, idempotent. Im Supabase SQL-Editor ausführen.

alter table calvoran.job_matches drop constraint if exists job_matches_status_check;
alter table calvoran.job_matches add constraint job_matches_status_check
    check (status in ('neu','gesichtet','relevant','irrelevant','outreach','abgelehnt'));
