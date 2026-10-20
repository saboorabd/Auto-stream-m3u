#!/bin/sh
# Keeper: keeps a fresh, working stream link ready, so reopening the page finds one.
# Runs beside app.py inside the container. Plain POSIX shell + curl.

PORT_NUM="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT_NUM}"

CHECK_S=20          # how often to look at the server
FRESH_S=240         # refresh when the current link is older than this (signed links expire)
NOLINK_S=90         # refresh when there has been no link for this long
COOLDOWN_S=120      # minimum gap between two refresh requests

log() { echo "[keeper $(date -u +%H:%M:%S)] $*"; }
now() { date -u +%s; }

# Wait until the app answers.
until curl -fs --max-time 4 "$BASE/api/status" >/dev/null 2>&1; do
  sleep 3
done
log "app is up on $BASE"

last_refresh=0
no_link_since=""

while true; do
  body=$(curl -fs --max-time 6 "$BASE/api/status" 2>/dev/null)
  if [ -z "$body" ]; then
    sleep "$CHECK_S"
    continue
  fi

  # Manual .m3u8 mode, or no target set: nothing to keep fresh.
  if ! echo "$body" | grep -q '"manual_url": *null'; then
    sleep "$CHECK_S"; continue
  fi
  if echo "$body" | grep -q '"page_url": *""'; then
    sleep "$CHECK_S"; continue
  fi

  t=$(now)

  if echo "$body" | grep -q '"url": *null'; then
    # No link at all: ask for one after NOLINK_S.
    [ -z "$no_link_since" ] && no_link_since=$t
    waited=$((t - no_link_since))
    if [ "$waited" -ge "$NOLINK_S" ] && [ $((t - last_refresh)) -ge "$COOLDOWN_S" ]; then
      log "no link for ${waited}s, requesting a refresh"
      curl -fs -X POST --max-time 6 "$BASE/api/refresh" >/dev/null 2>&1
      last_refresh=$t
    fi
  else
    no_link_since=""
    # Link exists: refresh it before it expires.
    ts=$(echo "$body" | sed -n 's/.*"updated_at": *"\([^"]*\)".*/\1/p')
    if [ -n "$ts" ]; then
      up=$(date -u -d "$ts" +%s 2>/dev/null || echo 0)
      age=$((t - up))
      if [ "$age" -ge "$FRESH_S" ] && [ $((t - last_refresh)) -ge "$COOLDOWN_S" ]; then
        log "link is ${age}s old, refreshing before it expires"
        curl -fs -X POST --max-time 6 "$BASE/api/refresh" >/dev/null 2>&1
        last_refresh=$t
      fi
    fi
  fi

  sleep "$CHECK_S"
done
