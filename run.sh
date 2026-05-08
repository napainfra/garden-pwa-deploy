#!/bin/sh
set -e

OPTS=/data/options.json

APP_PIN=$(jq -r '.pin // "8899"' "$OPTS")
FREEZE_AT=$(jq -r '.freeze_at // 1.0' "$OPTS")
COOKIE_SECRET=$(jq -r '.cookie_secret // ""' "$OPTS")
CFG_HA_URL=$(jq -r '.ha_url // ""' "$OPTS")
CFG_HA_TOKEN=$(jq -r '.ha_token // ""' "$OPTS")

if [ -z "$COOKIE_SECRET" ] || [ "$COOKIE_SECRET" = "null" ]; then
  COOKIE_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  echo "[garden_pwa] cookie_secret blank; generated ephemeral one"
fi

# Token resolution priority:
#   1. Supervisor-injected SUPERVISOR_TOKEN (if hassio_api works)
#   2. User-set ha_token in addon config (long-lived token)
if [ -n "$SUPERVISOR_TOKEN" ]; then
  HA_URL="http://supervisor/core"
  HA_TOKEN="$SUPERVISOR_TOKEN"
  echo "[garden_pwa] Using supervisor token"
elif [ -n "$CFG_HA_TOKEN" ] && [ "$CFG_HA_TOKEN" != "null" ]; then
  HA_URL="${CFG_HA_URL:-http://homeassistant.local.hass.io:8123}"
  HA_TOKEN="$CFG_HA_TOKEN"
  echo "[garden_pwa] Using long-lived token from config; HA_URL=$HA_URL"
else
  echo "[garden_pwa] ERROR: neither SUPERVISOR_TOKEN nor ha_token set in config"
  echo "[garden_pwa] Open Configuration tab and paste a long-lived access token in the ha_token field"
  exit 1
fi

export APP_PIN FREEZE_AT COOKIE_SECRET HA_URL HA_TOKEN
export PORT=8090

echo "[garden_pwa] Starting Veg Garden PWA on :8090 (PIN=${APP_PIN}, freeze=${FREEZE_AT})"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port 8090 --root-path /garden
