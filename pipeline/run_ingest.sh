#!/bin/bash
# run_ingest.sh — idempotenter Anreicherungs-Orchestrator für calvoran.companies.
#
# Kette:  North Data (c0/c1)  ->  Geburtsdaten (consolidate/c1b)  ->  Website (c2/c3/c4).
# Alle Stufen sind upsert-idempotent bzw. resume-bar (Firmen mit tech_signals/Dossier
# werden übersprungen). Ein Re-Run setzt gefahrlos fort — dafür ist das Skript gedacht,
# weil die hr-engine noch wochenlang neue Geburtsdaten liefert und die Website-Abdeckung
# wellenweise wächst.
#
# Aufruf:
#   ./pipeline/run_ingest.sh data     # Datenstand: (c0/c1 nur mit ND_CSV_DIR) + consolidate + c1b
#   ./pipeline/run_ingest.sh web      # Website gestaffelt: c2 (Crawl) + c3 priorisiert + c4
#   ./pipeline/run_ingest.sh all      # beides (Default)
#
# Env:
#   ND_CSV_DIR=/pfad   neuer North-Data-Batch (searchresults*.csv) — sonst c0/c1 übersprungen
#   MIN_SCORE=2        Prioritätsschwelle für c2/c3 (Default 2 = A/B-Kandidaten zuerst)
#   WEB_TAIL=1         auch den Rest (prioritaets_score < MIN_SCORE) crawlen/dossieren
#   IDS_FILE=/pfad     gezielter Web-Lauf über eine ID-Liste (z.B. Nachfolge-Kandidaten
#                      nach GF-Alter) statt über MIN_SCORE — c2/c3/c4 laufen mit --ids-file
#
# Der Datenstand-Teil ist billig (Minuten). Der Website-Teil, speziell c3 (Dossiers über
# lokales Gemma), ist der teure Schritt und konkurriert mit der hr-engine um den Mac mini.
# c3 ist resume-bar: mit Ctrl-C unterbrechen und später erneut starten ist unkritisch.

set -u

ROOT="$HOME/projects/calvoran-outreach"
HR_ENGINE="$HOME/projects/Unternehmensregister/hr-engine"
GF_CSV="$HOME/projects/os/01-projects/fractional-cfo/hr-abruf/gf-geburtsdaten.csv"
PY="$ROOT/.venv/bin/python"
HR_PY="$HR_ENGINE/.venv/bin/python"
export PYTHONPATH="$ROOT:$ROOT/pipeline"

MIN_SCORE="${MIN_SCORE:-2}"
WEB_TAIL="${WEB_TAIL:-0}"
MODE="${1:-all}"
LOG="$HOME/projects/os/01-projects/fractional-cfo/outreach/ingest-$(date +%F).log"

cd "$ROOT" || { echo "calvoran-outreach fehlt"; exit 1; }
ts() { date '+%F %T'; }
run() {
  echo "[$(ts)] > $*" | tee -a "$LOG"
  "$@" >>"$LOG" 2>&1
  local rc=$?
  echo "[$(ts)] < rc=$rc" | tee -a "$LOG"
  return $rc
}

phase_data() {
  echo "[$(ts)] === DATA-Phase ===" | tee -a "$LOG"
  # 1. North Data: nur wenn ein Batch-Verzeichnis übergeben wurde (der 29.06-Lauf ist bereits importiert).
  if [ -n "${ND_CSV_DIR:-}" ] && ls "$ND_CSV_DIR"/searchresults*.csv >/dev/null 2>&1; then
    local master="data/zielliste_$(date +%F).csv"
    run "$PY" pipeline/c0_merge_searchresults.py "$master" "$ND_CSV_DIR"/searchresults*.csv || return 1
    run "$PY" pipeline/c1_import_zielliste.py --csv "$master" || return 1
  else
    echo "[$(ts)] c0/c1 übersprungen (kein ND_CSV_DIR mit searchresults*.csv)" | tee -a "$LOG"
  fi
  # 2. Geburtsdaten frisch aus der hr-engine (liest state.db read-only, Daemon darf laufen).
  if [ -x "$HR_PY" ]; then
    run "$HR_PY" -m hr_engine.cli consolidate || return 1
  else
    echo "[$(ts)] consolidate übersprungen (hr-engine nicht auf diesem Host)" | tee -a "$LOG"
  fi
  # 3. GF-Geburtsjahr/-Alter nach calvoran.companies.
  [ -f "$GF_CSV" ] && run "$PY" pipeline/c1b_import_gf_alter.py --gf "$GF_CSV"
}

phase_web() {
  if [ -n "${IDS_FILE:-}" ]; then
    # Gezielter Lauf über eine ID-Liste (z.B. Nachfolge-Kandidaten nach GF-Alter).
    echo "[$(ts)] === WEB-Phase (IDS_FILE=$IDS_FILE) ===" | tee -a "$LOG"
    [ -f "$IDS_FILE" ] || { echo "[$(ts)] IDS_FILE fehlt: $IDS_FILE"; return 1; }
    run "$PY" pipeline/c2_crawl.py   --ids-file "$IDS_FILE" || true
    run "$PY" pipeline/c3_extract.py --ids-file "$IDS_FILE" --report || true
    run "$PY" pipeline/c4_score_cluster.py --ids-file "$IDS_FILE" --report || true
    return 0
  fi
  echo "[$(ts)] === WEB-Phase (MIN_SCORE=$MIN_SCORE, WEB_TAIL=$WEB_TAIL) ===" | tee -a "$LOG"
  # 4. Crawl (billig, netzgebunden): erst Priorität, dann optional der Rest. Resume-bar via tech_signals.
  run "$PY" pipeline/c2_crawl.py --min-score "$MIN_SCORE" || true
  [ "$WEB_TAIL" = "1" ] && run "$PY" pipeline/c2_crawl.py || true
  # 5. Dossiers (teuer, Gemma lokal): priorisiert. Resume-bar via bestehendes Dossier.
  run "$PY" pipeline/c3_extract.py --min-score "$MIN_SCORE" --report || true
  [ "$WEB_TAIL" = "1" ] && run "$PY" pipeline/c3_extract.py --report || true
  # 6. Deterministisches Scoring (kein LLM, schnell).
  run "$PY" pipeline/c4_score_cluster.py --report || true
}

echo "[$(ts)] ===== run_ingest.sh MODE=$MODE =====" | tee -a "$LOG"
case "$MODE" in
  data) phase_data ;;
  web)  phase_web ;;
  all)  phase_data && phase_web ;;
  *)    echo "Unbekannter Modus: $MODE (data|web|all)"; exit 2 ;;
esac
echo "[$(ts)] ===== run_ingest.sh fertig (Log: $LOG) =====" | tee -a "$LOG"
