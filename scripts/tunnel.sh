#!/bin/bash
# Keeps a Cloudflare quick tunnel open to the local dashboard (port 3001) and
# publishes its URL, HMAC-signed with the access key, to the ntfy topic the
# Vercel site reads. Runs forever under launchd (KeepAlive).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p logs data
TLOG="logs/tunnel.log"
CFLOG="logs/cloudflared.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(ts) $*" >> "$TLOG"; }

KEY="$(grep -E '^JOBBOT_ACCESS_KEY=' web/.env.local | cut -d= -f2-)"
TOPIC="$(grep -E '^JOBBOT_NTFY_TOPIC=' web/.env.local | cut -d= -f2-)"
TOPIC="${TOPIC:-jobbot-link-04021e998f9b1712}"
[ -z "$KEY" ] && { log "no JOBBOT_ACCESS_KEY in web/.env.local"; exit 1; }

CF_PID=""; URL=""; LAST_PUB=0; FAILS=0

start_cf() {
  [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null
  pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:3001" 2>/dev/null
  : > "$CFLOG"
  cloudflared tunnel --no-autoupdate --url http://localhost:3001 >> "$CFLOG" 2>&1 &
  CF_PID=$!
  URL=""
  for _ in $(seq 1 60); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CFLOG" | head -1)"
    [ -n "$URL" ] && break
    sleep 1
  done
  if [ -z "$URL" ]; then log "cloudflared gave no URL"; return 1; fi
  echo "$URL" > data/tunnel_url.txt
  log "tunnel up: $URL (pid $CF_PID)"
  LAST_PUB=0; FAILS=0
  sleep 8   # let the hostname propagate
}

publish() {
  local sig
  sig="$(printf '%s' "$URL" | openssl dgst -sha256 -hmac "$KEY" -hex | awk '{print $NF}')"
  if curl -fsS -m 15 -d "$URL $sig" "https://ntfy.sh/$TOPIC" >/dev/null; then
    LAST_PUB=$(date +%s); log "published $URL"
  else
    log "publish to ntfy failed"
  fi
}

trap 'kill $CF_PID 2>/dev/null; exit 0' TERM INT

while true; do
  if [ -z "$CF_PID" ] || ! kill -0 "$CF_PID" 2>/dev/null || [ -z "$URL" ]; then
    start_cf || { sleep 30; continue; }
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 -H "x-jobbot-key: $KEY" "$URL/api/health")"
  if [ "$code" = "200" ]; then
    FAILS=0
  else
    lcode="$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://localhost:3001/api/health)"
    if [ "$lcode" != "200" ]; then log "local dashboard down ($lcode) - tunnel kept, waiting"; sleep 30; continue; fi
    FAILS=$((FAILS + 1)); log "health via tunnel -> $code (fail $FAILS)"
    if [ "$FAILS" -ge 4 ]; then log "tunnel unhealthy -> restarting"; start_cf || true; continue; fi
  fi
  # republish every 10 min (ntfy keeps messages 12h) or when asked
  if [ $(( $(date +%s) - LAST_PUB )) -ge 600 ] || [ -f data/tunnel_republish ]; then
    rm -f data/tunnel_republish
    publish
  fi
  [ -f data/tunnel_restart ] && { rm -f data/tunnel_restart; log "restart requested"; start_cf || true; continue; }
  sleep 30
done
