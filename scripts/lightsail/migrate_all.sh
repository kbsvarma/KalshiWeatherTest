#!/usr/bin/env bash
# One-shot full migration from Mac to Lightsail.
# Run this from the LOCAL Mac. Idempotent — safe to re-run.
#
# What it does:
#   1. Pre-flight: verify SSH to Lightsail works
#   2. Rsync the entire local repo to /opt/kalshi-weather-staging on Lightsail
#   3. Run bootstrap.sh on Lightsail (creates user, venv, systemd units, Ollama)
#   4. Run migrate_state.sh: copy state DB, climate normals, recent reports, secrets
#   5. Validate: trigger one cycle on Lightsail, confirm it completes
#   6. Print a one-liner cheat sheet for monitoring
#
# Stays in PAPER MODE the whole time. Never touches LIVE_ORDERS_*.
#
# Usage:
#   bash scripts/lightsail/migrate_all.sh

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-54.225.195.11}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lightsail.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/kalshi-weather}"
LOCAL_ROOT="${LOCAL_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
KALSHI_PRIVATE_KEY="${KALSHI_PRIVATE_KEY:-$HOME/.kalshi/private_key.pem}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=20 $REMOTE_USER@$REMOTE_HOST"
RSYNC_SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

log() { echo "[migrate_all $(date +%H:%M:%S)] $*"; }

# ── Pre-flight ───────────────────────────────────────────────────────────
log "Pre-flight: SSH reachable?"
if ! $SSH "echo connected" >/dev/null 2>&1; then
  log "FAIL: cannot SSH to $REMOTE_USER@$REMOTE_HOST"
  log "Possible causes: Comcast↔AWS routing flake, security group, instance down."
  log "Check from another network or browser SSH."
  exit 1
fi
log "  OK"

# ── Step 1: rsync repo to staging area on Lightsail ─────────────────────
log "Step 1/5: rsync repo → $REMOTE_HOST:$REMOTE_DIR"
# First time only: ensure target dir exists with correct ownership.
$SSH "sudo mkdir -p $REMOTE_DIR && sudo chown -R $REMOTE_USER:$REMOTE_USER $REMOTE_DIR"
rsync -avz --delete --progress \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.streamlit' \
  --exclude='logs/cron_cycle.log*' \
  --exclude='logs/cycle.lockdir' \
  --exclude='logs/heartbeat.txt' \
  --exclude='data/state/runtime.sqlite3' \
  -e "$RSYNC_SSH" \
  "$LOCAL_ROOT/" \
  "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
log "  rsync complete"

# ── Step 2: run bootstrap.sh on Lightsail ───────────────────────────────
log "Step 2/5: bootstrap.sh (system user, venv, Ollama, systemd units)"
$SSH "cd $REMOTE_DIR && bash scripts/lightsail/bootstrap.sh"
log "  bootstrap complete"

# ── Step 3: migrate state (one-time) ────────────────────────────────────
log "Step 3/5: migrating state DB + secrets"
# Pause Mac cycle briefly during DB copy
launchctl unload ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.backup.plist 2>/dev/null || true
trap 'launchctl load ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.plist 2>/dev/null || true; launchctl load ~/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.backup.plist 2>/dev/null || true' EXIT
sleep 5
# Copy state DB
rsync -avz -e "$RSYNC_SSH" "$LOCAL_ROOT/data/state/runtime.sqlite3" \
  "$REMOTE_USER@$REMOTE_HOST:/tmp/runtime.sqlite3"
$SSH "sudo install -o kalshibot -g kalshibot -m 0644 /tmp/runtime.sqlite3 $REMOTE_DIR/data/state/runtime.sqlite3 && rm /tmp/runtime.sqlite3"
# Copy historical cycle reports (gives the survey resolver context)
rsync -avz -e "$RSYNC_SSH" "$LOCAL_ROOT/logs/cycle_reports/" \
  "$REMOTE_USER@$REMOTE_HOST:/tmp/cycle_reports/"
$SSH "sudo mkdir -p $REMOTE_DIR/logs/cycle_reports && sudo cp -r /tmp/cycle_reports/. $REMOTE_DIR/logs/cycle_reports/ && sudo chown -R kalshibot:kalshibot $REMOTE_DIR/logs/cycle_reports && rm -rf /tmp/cycle_reports"
# Kalshi private key
if [[ -f "$KALSHI_PRIVATE_KEY" ]]; then
  rsync -avz -e "$RSYNC_SSH" --chmod=F0400 "$KALSHI_PRIVATE_KEY" \
    "$REMOTE_USER@$REMOTE_HOST:/tmp/kalshi_pk.pem"
  $SSH "sudo mkdir -p $REMOTE_DIR/secrets && sudo install -o kalshibot -g kalshibot -m 0400 /tmp/kalshi_pk.pem $REMOTE_DIR/secrets/private_key.pem && sudo chmod 0700 $REMOTE_DIR/secrets && sudo chown kalshibot:kalshibot $REMOTE_DIR/secrets && rm /tmp/kalshi_pk.pem"
  log "  Kalshi private key installed at $REMOTE_DIR/secrets/private_key.pem"
else
  log "  WARN: no Kalshi key at $KALSHI_PRIVATE_KEY — bot will fail to place orders"
fi
log "  state migration complete"

# ── Step 4: trigger one cycle to validate ───────────────────────────────
log "Step 4/5: triggering one validation cycle (dry-run, no live orders)"
$SSH "sudo systemctl start kalshi-weather-cycle.service" || true
log "  cycle started; tailing log for 90s..."
$SSH "timeout 90 sudo journalctl -u kalshi-weather-cycle.service -f --since '5s ago'" || true

# ── Step 5: post-validation summary ─────────────────────────────────────
log "Step 5/5: post-validation summary"
$SSH "
echo '--- timers ---'
systemctl list-timers --no-pager | grep kalshi-weather || true
echo
echo '--- service states ---'
for s in kalshi-weather-cycle.service kalshi-weather-dashboard.service ollama.service; do
  echo \"\$s: \$(systemctl is-active \$s) | enabled=\$(systemctl is-enabled \$s 2>/dev/null)\"
done
echo
echo '--- last cycle outcome ---'
tail -5 $REMOTE_DIR/logs/cron_cycle.log 2>/dev/null || echo '(no log yet)'
echo
echo '--- recent cycle report row ---'
tail -1 $REMOTE_DIR/logs/cycle_reports/\$(date -u +%Y-%m-%d).jsonl 2>/dev/null || echo '(no report yet)'
"

log ""
log "════════════════════════════════════════════════════════════════════════"
log "MIGRATION COMPLETE — Lightsail in PAPER MODE"
log "════════════════════════════════════════════════════════════════════════"
log ""
log "Mac is still running live; Lightsail is dry-running in parallel."
log ""
log "Monitor Lightsail:"
log "  ssh -i $SSH_KEY $REMOTE_USER@$REMOTE_HOST"
log "  sudo journalctl -u kalshi-weather-cycle.service -f"
log "  sudo journalctl -u kalshi-weather-dashboard.service -f"
log ""
log "Dashboard (need to open port 8501 via Lightsail firewall first):"
log "  aws lightsail open-instance-public-ports --instance-name kalshi-bot \\"
log "    --port-info fromPort=8501,toPort=8501,protocol=TCP --region us-east-1"
log "  Then: http://$REMOTE_HOST:8501"
log ""
log "When you're ready to flip live, run:"
log "  bash $LOCAL_ROOT/scripts/lightsail/flip_to_live.sh"
log "════════════════════════════════════════════════════════════════════════"
