#!/bin/bash
# Start the continuous job-apply bot (used by launchd at 8 AM and by the watchdog).
#   start_bot.sh daily    -> clears user STOP markers and starts (morning start)
#   start_bot.sh watchdog -> restarts only if not running AND the user did not press Stop
MODE="${1:-daily}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p logs
WLOG="logs/watchdog.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# keep the dashboard (port 3001) alive too
if ! lsof -nP -iTCP:3001 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "$(ts) [$MODE] dashboard not listening on 3001 -> starting" >> "$WLOG"
  (cd web && nohup npx next dev --port 3001 > ../logs/dashboard.log 2>&1 &)
fi

# answers / profile details saved on the website while the Mac was off
[ -x .venv/bin/python ] && .venv/bin/python scripts/cloud.py inbox >> "$WLOG" 2>&1

# GitHub runner is primary: while it is applying the Mac only backs up
if .venv/bin/python scripts/cloud.py cloud_running >/dev/null 2>&1; then
  if pgrep -f "jobbot\.cli (apply|run)" >/dev/null; then
    echo "$(ts) [$MODE] GitHub runner applying -> Mac bot yields (stopping)" >> "$WLOG"
    .venv/bin/python scripts/state_sync.py push >> "$WLOG" 2>&1
    pkill -f "jobbot\.cli (apply|run)"; sleep 5; pkill -9 -f "jobbot\.cli (apply|run)"
  else
    echo "$(ts) [$MODE] GitHub runner applying -> Mac stays idle" >> "$WLOG"
  fi
  exit 0
fi

if pgrep -f "jobbot\.cli (apply|run)" >/dev/null; then
  # running: check it is not frozen (log untouched for 25 min)
  if [ -f logs/realrun.log ] && [ $(( $(date +%s) - $(stat -f %m logs/realrun.log) )) -gt 1500 ]; then
    echo "$(ts) [$MODE] bot log idle >25 min -> killing frozen bot" >> "$WLOG"
    pkill -f "jobbot\.cli (apply|run)"; sleep 5; pkill -9 -f "jobbot\.cli (apply|run)"
  else
    echo "$(ts) [$MODE] bot running OK" >> "$WLOG"
    # share progress with the cloud runner every ~30 min
    if [ ! -f data/last_push ] || [ $(( $(date +%s) - $(stat -f %m data/last_push) )) -gt 1700 ]; then
      .venv/bin/python scripts/state_sync.py push >> "$WLOG" 2>&1 && touch data/last_push
    fi
    exit 0
  fi
fi

if [ "$MODE" = "daily" ]; then
  rm -f data/STOP data/STOP-harshank data/STOP-divya
elif [ -f data/STOP ]; then
  echo "$(ts) [$MODE] user pressed Stop -> not restarting" >> "$WLOG"; exit 0
elif .venv/bin/python scripts/cloud.py paused >/dev/null 2>&1; then
  echo "$(ts) [$MODE] paused from the website -> not restarting" >> "$WLOG"; exit 0
fi

# pick up whatever the cloud runner applied while the Mac was off
.venv/bin/python scripts/state_sync.py pull >> "$WLOG" 2>&1 || echo "$(ts) [$MODE] state pull failed" >> "$WLOG"

# diagnose why it died (last errors from the previous log)
if [ -f logs/realrun.log ]; then
  echo "$(ts) [$MODE] last log lines before restart:" >> "$WLOG"
  grep -iE "error|traceback|exception|crash" logs/realrun.log | tail -5 >> "$WLOG"
  mv logs/realrun.log "logs/realrun-$(date +%Y%m%d-%H%M).log"
fi
# auto-fix: stale Chrome profile locks left by a crash
find data -maxdepth 2 -name 'Singleton*' -path '*chrome-profile*' -exec rm -f {} + 2>/dev/null

nohup .venv/bin/python -u -m jobbot.cli apply --candidate both --continuous --workers 2 \
  --india-remote-only --min-score 50 --min-score-floor 40 --skip-ats greenhouse,lever \
  > logs/realrun.log 2>&1 &
echo "$(ts) [$MODE] started bot pid $!" >> "$WLOG"
