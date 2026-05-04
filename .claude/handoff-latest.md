# Handoff 2026-05-04 17:05 — calvoran-outreach Bootstrap

## Stand

- Neues Projekt komplett aufgebaut, End-to-End-Pipeline lauffähig
- 3 Apify-Actors validiert (Indeed/StepStone/Arbeitsagentur), Mapping je Quelle korrekt
- Supabase-Schema `calvoran` mit 3 Tabellen (raw_jobs, leads, outreach), Schema exponiert, GRANTs gesetzt
- Smoke-Test 1 Query × 5 Items je Plattform → 15 raw_jobs, 11 unique Leads, 2 in Target Cities
- Cross-Plattform-Dedup, Stadt-Filter (Top 21–100), Buchhaltungs-Exclude funktionieren

**Erstellte Dateien:**
- `sql/schema.sql` (Schema + GRANTs)
- `config/keywords.py` (10 Queries, 13 Excludes, 80 Target Cities, 21 Excluded Top-20)
- `config/actor_*.json` (gecachte Apify-Schemas)
- `scripts/_db.py`, `scripts/inspect_actors.py`, `scripts/init_db.py`
- `scripts/01_scrape.py`, `scripts/02_filter_dedup.py`, `scripts/03_extract_contact.py`, `scripts/04_export_csv.py`
- `CLAUDE.md`, `.gitignore`, `requirements.txt`, `.env`

## Offene Punkte

- **Full-Scan noch nicht ausgeführt** — wartet auf Freigabe (geschätzt 3–6 USD Apify-Credits)
- **StepStone-Description ist null** im Default-Run → Buchhaltungs-Filter greift dort nur auf Titel; Detail-Fetch-Mode des Actors prüfen
- **Edge-Case Stadt-Matching**: "Lübecker Straße" wird als Lübeck erkannt → Word-Boundary-Matching nachziehen
- **Outreach-Phase** komplett offen (Tabelle existiert, kein Workflow)
- **Git-Remote** nicht eingerichtet — `git push` lokal nicht möglich

## Entscheidungen

- **Python statt Node.js**: Konsistent mit haufe-scraper, supabase-py vorhanden
- **Schema `calvoran`** im selben Supabase-Projekt wie haufe-scraper (User-Vorgabe)
- **Stadt-Filter im Post-Processing**, nicht als Apify-Input: 80 Städte × 10 Queries × 3 Plattformen wären zu teuer; Apify scrapt bundesweit, Python filtert
- **Apify-Memory hochgesetzt**: Indeed 512 MB, StepStone 1024 MB, Arbeitsagentur 1024 MB (Default war OOM bei Captcha-Solving)
- **Arbeitsagentur mit `includeJobDetails: true`** läuft → liefert Ansprechpartner direkt strukturiert (Captcha-Solving funktioniert)
- **Indeed parallelisiert (ThreadPoolExecutor, max 4)**, Arbeitsagentur in einem Run mit `searchQueries`-Array
- **Dedupe-Key**: `normalize(firma)|normalize(titel)` — Standort wird absichtlich rausgelassen, da Plattformen ihn unterschiedlich formatieren
- **Schema-Routing supabase-py**: `client.schema('calvoran').table(...)` ist die korrekte API in 2.x; `postgrest.schema()` modifiziert nicht in-place

## Nächste Schritte

1. **Full-Scan freigeben und ausführen**: `.venv/bin/python scripts/01_scrape.py` (10 Queries × 20 Items × 3 Plattformen)
2. **02 + 03 + 04 laufen lassen** für vollständigen Lead-CSV
3. **Trefferquote in Target Cities prüfen** — bei <10% verwertbar nachjustieren (z.B. Stadt-spezifische Suchen für die wertvollsten Großstädte)
4. **StepStone-Detail-Fetch evaluieren**: Actor-Doku prüfen, ob es einen Mode für Volltext gibt
5. **Outreach-Workflow konzipieren**: Hook über LinkedIn-Outreach-Skill? E-Mail-Vorlage mit "Prozess statt Person"-Framing?

## Kontext

**Pfade:**
- Projekt: `/Users/johannesbreuers/projects/calvoran-outreach`
- Apify-Skills (Referenz): `/Users/johannesbreuers/projects/_Claude-main/skills/custom/apify-*`
- Supabase-Pattern (Referenz): `/Users/johannesbreuers/projects/haufe-scraper/haufe_import.py`

**Apify-Actor-IDs:**
- `automation-lab/indeed-scraper`
- `unfenced-group/stepstone-de-scraper`
- `santamaria-automations/arbeitsagentur-de-scraper`

**Supabase:** Schema `calvoran`, gleiche Instanz wie haufe-scraper

**Offene Fragen:**
- Outreach-Channel: E-Mail primär, oder LinkedIn als Erstkontakt?
- Stadt-Liste: Top 21–100 strikt, oder Sonderfälle (z.B. Speckgürtel)?
- Frequenz: einmaliger Scan, oder wöchentlich/monatlich wiederholen?
