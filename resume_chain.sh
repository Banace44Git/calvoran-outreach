#!/bin/bash
# Auto-Resume-Wrapper für die Pipeline-Chain (c3 Gemma -> c4) nach dem Crash 16:06
# (httpx.ReadTimeout zu Supabase, vermutl. Stromausfall-Netz-Blip). c3 ist idempotent
# (Firmen mit Dossier werden übersprungen), daher resumed jeder Re-Run von selbst.
set -u
cd "$HOME/projects/calvoran-outreach" || exit 1
export PYTHONPATH=pipeline
LOG="$HOME/projects/os/01-projects/fractional-cfo/outreach/pipeline-chain-2026-06-16.log"
PY=".venv/bin/python"
ts() { date '+%F %T'; }

echo "[$(ts)] === Auto-Resume-Wrapper gestartet (Recovery nach Stromausfall) ===" >> "$LOG"

attempt=0; max=40
while true; do
  attempt=$((attempt+1))
  echo "[$(ts)] c3 (Gemma) Versuch $attempt/$max ..." >> "$LOG"
  $PY pipeline/c3_extract.py >> "$LOG" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    echo "[$(ts)] c3 sauber fertig (rc=0) nach $attempt Versuch(en)." >> "$LOG"
    break
  fi
  echo "[$(ts)] c3 abgebrochen (rc=$rc) -> idempotenter Resume in 60s." >> "$LOG"
  if [ $attempt -ge $max ]; then
    echo "[$(ts)] c3 Max-Versuche ($max) erreicht -> Abbruch, c4 NICHT gestartet." >> "$LOG"
    exit 1
  fi
  sleep 60
done

echo "[$(ts)] -> starte c4 (Scoring, inkl. Generationswechsel-KO) ..." >> "$LOG"
$PY pipeline/c4_score_cluster.py --report >> "$LOG" 2>&1
echo "[$(ts)] c4 fertig (rc=$?). Chain komplett." >> "$LOG"
