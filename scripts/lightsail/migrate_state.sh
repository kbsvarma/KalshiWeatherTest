#!/usr/bin/env bash
# One-shot state migration from Mac to Lightsail.
# Run from the LOCAL Mac (not the cloud box). Uses rsync over SSH.
#
# Copies:
#   - data/state/runtime.sqlite3              (the single source of truth)
#   - data/reference/climate_normals/         (per-city 30-year normals)
#   - logs/cycle_reports/*.jsonl              (historical cycle data)
#   - secrets/private_key.pem                 (Kalshi API private key)
#
# Stops the Mac launchd cycle job for the duration of the copy so the
# state DB isn't written to while being copied. Re-enables it afterward.
#
# Usage:
#   bash scripts/lightsail/migrate_state.sh
#   # or to target a different host:
#   REMOTE_HOST=44.214.100.254 bash scripts/lightsail/migrate_state.sh

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-54.225.195.11}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lightsail.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/kalshi-weather}"
LOCAL_ROOT="${LOCAL_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
KALSHI_PRIVATE_KEY="${KALSHI_PRIVATE_KEY:-$HOME/.kalshi/private_key.pem}"

log() { echo "[migrate] $*"; }

[[ -f "$SSH_KEY" ]] || { log "ERROR: SSH key not found at $SSH_KEY"; exit 1; }
[[ -d "$LOCAL_ROOT" ]] || { log "ERROR: local repo not found at $LOCAL_ROOT"; exit 1; }
[[ -f "$KALSHI_PRIVATE_KEY" ]] || { log "WARN: Kalshi private key not found at $KALSHI_PRIVATE_KEY — skipping secret copy"; }

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=15 $REMOTE_USER@$REMOTE_HOST"
RSYNC_SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

# Verify connectivity before doing anything destructive
log "Verifying SSH to $REMOTE_USER@$REMOTE_HOST..."
$SSH "echo connected" >/dev/null 2>&1 || { log "ERROR: SSH failed — aborting"; exit 1; }

# Stop Mac cycle job briefly so DB isn't mid-write during copy.
# Use launchctl (Mac-only); cloud-side has nothing running yet.
log "Pausing Mac cycle job during copy"
launchctl unload ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.backup.plist 2>/dev/null || true
trap 'log "Re-enabling Mac cycle job"; launchctl load ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.plist 2>/dev/null || true; launchctl load ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.backup.plist 2>/dev/null || true' EXIT

# Wait a few seconds for any in-flight cycle to finish its DB writes
sleep 5

# State DB
log "Copying state DB"
rsync -avz --progress -e "$RSYNC_SSH" \
  "$LOCAL_ROOT/data/state/runtime.sqlite3" \
  "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/data/state/"

# Climate normals
log "Copying climate normals"
rsync -avz --progress -e "$RSYNC_SSH" \
  "$LOCAL_ROOT/data/reference/" \
  "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/data/reference/"

# Recent cycle reports (history matters for the survey resolver)
log "Copying recent cycle reports"
rsync -avz --progress -e "$RSYNC_SSH" \
  "$LOCAL_ROOT/logs/cycle_reports/" \
  "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/logs/cycle_reports/"

# Kalshi private key
if [[ -f "$KALSHI_PRIVATE_KEY" ]]; then
  log "Copying Kalshi private key (one-time, mode 0400)"
  $SSH "sudo mkdir -p $REMOTE_DIR/secrets && sudo chown kalshibot:kalshibot $REMOTE_DIR/secrets && sudo chmod 0700 $REMOTE_DIR/secrets"
  rsync -avz -e "$RSYNC_SSH" --chmod=F0400 \
    "$KALSHI_PRIVATE_KEY" \
    "$REMOTE_USER@$REMOTE_HOST:/tmp/kalshi_pk.pem"
  $SSH "sudo install -o kalshibot -g kalshibot -m 0400 /tmp/kalshi_pk.pem $REMOTE_DIR/secrets/private_key.pem && rm /tmp/kalshi_pk.pem"
fi

# Fix ownership of all copied data (rsync ran as ubuntu)
log "Fixing ownership on remote"
$SSH "sudo chown -R kalshibot:kalshibot $REMOTE_DIR/data $REMOTE_DIR/logs"

log ""
log "Migration complete. Remote state ready for dry-run."
log "Trigger a cycle on Lightsail to validate:"
log "  ssh -i $SSH_KEY $REMOTE_USER@$REMOTE_HOST"
log "  sudo systemctl start kalshi-weather-cycle.service"
log "  sudo journalctl -u kalshi-weather-cycle.service -f"
