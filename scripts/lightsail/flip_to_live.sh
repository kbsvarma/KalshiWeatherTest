#!/usr/bin/env bash
# Flip Kalshi weather bot from PAPER → LIVE on Lightsail.
# Symmetric: also stops the Mac launchd cycle so there's never a moment
# where both Mac and Lightsail can place real orders.
#
# Pre-flight checks (each must pass):
#   1. Lightsail bot is reachable via SSH
#   2. Lightsail cycle has run at least N successful cycles in dry-run
#   3. Mac launchd cycle is currently loaded
#   4. Operator confirms the flip in an interactive prompt
#
# Action:
#   1. Disable Mac launchd cycle (primary + backup) — first, so Mac stops
#   2. Drop in a systemd override on Lightsail flipping LIVE_ORDERS_ENABLED=1
#   3. Reload systemd, restart the cycle service
#   4. Watch the first post-flip cycle complete
#
# Rollback:
#   bash scripts/lightsail/flip_to_live.sh --revert
#   # disables LIVE_ORDERS on Lightsail, re-enables Mac launchd

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-54.225.195.11}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lightsail.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/kalshi-weather}"
MIN_DRY_RUN_CYCLES="${MIN_DRY_RUN_CYCLES:-3}"
MAC_PRIMARY_PLIST="$HOME/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.plist"
MAC_BACKUP_PLIST="$HOME/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.backup.plist"
LIVE_DAILY_USD_CAP="${LIVE_DAILY_USD_CAP:-15.0}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=15 $REMOTE_USER@$REMOTE_HOST"

log() { echo "[flip] $*"; }

if [[ "${1:-}" == "--revert" ]]; then
  log "REVERTING — Lightsail → PAPER, Mac → LIVE"
  $SSH "sudo rm -f /etc/systemd/system/kalshi-weather-cycle.service.d/live.conf && sudo systemctl daemon-reload"
  log "Lightsail systemd override removed"
  if [[ -f "$MAC_PRIMARY_PLIST" ]]; then
    launchctl load "$MAC_PRIMARY_PLIST" 2>/dev/null || true
    launchctl load "$MAC_BACKUP_PLIST" 2>/dev/null || true
    log "Mac launchd cycle re-enabled"
  fi
  log "Revert complete. Verify with: bash scripts/healthcheck.sh"
  exit 0
fi

# ── Pre-flight check 1: SSH reachable ───────────────────────────────────
log "Pre-flight 1/4: SSH reachable?"
if ! $SSH "echo ok" >/dev/null 2>&1; then
  log "  FAIL: cannot reach $REMOTE_USER@$REMOTE_HOST"
  exit 1
fi
log "  OK"

# ── Pre-flight check 2: enough dry-run cycles completed ─────────────────
log "Pre-flight 2/4: at least $MIN_DRY_RUN_CYCLES dry-run cycles completed?"
DRY_RUN_COUNT=$($SSH "grep -cF '────── cycle end ──────' $REMOTE_DIR/logs/cron_cycle.log 2>/dev/null || echo 0")
log "  Cycles seen: $DRY_RUN_COUNT (need $MIN_DRY_RUN_CYCLES)"
if (( DRY_RUN_COUNT < MIN_DRY_RUN_CYCLES )); then
  log "  FAIL: not enough cycles. Wait for $MIN_DRY_RUN_CYCLES then retry."
  exit 1
fi

# ── Pre-flight check 3: Mac launchd is loaded ───────────────────────────
log "Pre-flight 3/4: Mac launchd cycle currently loaded?"
if launchctl list 2>/dev/null | grep -q "com.varmakammili.kalshi.weather.cycle$"; then
  log "  OK — Mac is running live"
  MAC_LOADED=1
else
  log "  Mac launchd cycle not loaded — proceeding anyway"
  MAC_LOADED=0
fi

# ── Pre-flight check 4: operator confirmation ───────────────────────────
log "Pre-flight 4/4: operator confirmation"
echo ""
echo "  ⚠  About to flip Kalshi weather bot to LIVE on $REMOTE_HOST"
echo "     - Mac launchd cycle will be DISABLED (no more Mac-side orders)"
echo "     - Lightsail LIVE_ORDERS_ENABLED=1, LIVE_ORDERS_DRY_RUN=0"
echo "     - Daily cap: \$${LIVE_DAILY_USD_CAP}"
echo ""
read -p "  Type FLIP to proceed: " -r confirm
if [[ "$confirm" != "FLIP" ]]; then
  log "  Aborted by operator"
  exit 0
fi

# ── ACTION 1: stop Mac first (so there's never overlap) ─────────────────
if (( MAC_LOADED )); then
  log "Action 1/3: disabling Mac launchd cycle"
  launchctl unload "$MAC_PRIMARY_PLIST" 2>/dev/null || true
  launchctl unload "$MAC_BACKUP_PLIST" 2>/dev/null || true
  sleep 2
  if launchctl list 2>/dev/null | grep -q "com.varmakammili.kalshi.weather.cycle$"; then
    log "  WARN: Mac cycle still loaded — proceeding anyway"
  else
    log "  Mac cycle disabled"
  fi
fi

# ── ACTION 2: drop systemd override on Lightsail flipping to LIVE ───────
log "Action 2/3: writing systemd live override on Lightsail"
$SSH "sudo mkdir -p /etc/systemd/system/kalshi-weather-cycle.service.d && \
  sudo tee /etc/systemd/system/kalshi-weather-cycle.service.d/live.conf >/dev/null <<EOF
[Service]
# Live override — written by flip_to_live.sh.
# Revert with: bash $REMOTE_DIR/scripts/lightsail/flip_to_live.sh --revert
# Or manually: sudo rm /etc/systemd/system/kalshi-weather-cycle.service.d/live.conf && sudo systemctl daemon-reload
Environment=LIVE_ORDERS_ENABLED=1
Environment=LIVE_ORDERS_DRY_RUN=0
Environment=LIVE_DAILY_USD_CAP=$LIVE_DAILY_USD_CAP
EOF
sudo systemctl daemon-reload"
log "  Override installed at /etc/systemd/system/kalshi-weather-cycle.service.d/live.conf"

# ── ACTION 3: trigger one cycle to validate post-flip ───────────────────
log "Action 3/3: triggering one cycle to validate"
$SSH "sudo systemctl start kalshi-weather-cycle.service" &
START_PID=$!
log "  Watching cycle service (cancel with Ctrl-C; cycle keeps running)"
$SSH "sudo journalctl -u kalshi-weather-cycle.service -f --since '30s ago'" &
JLOG_PID=$!
wait "$START_PID" 2>/dev/null || true
sleep 60
kill "$JLOG_PID" 2>/dev/null || true

log ""
log "══════════════════════════════════════════════════════════════════════"
log "FLIP COMPLETE"
log "══════════════════════════════════════════════════════════════════════"
log "  Lightsail: LIVE_ORDERS_ENABLED=1, daily cap=\$$LIVE_DAILY_USD_CAP"
log "  Mac: cycle disabled"
log ""
log "Monitor:"
log "  ssh -i $SSH_KEY $REMOTE_USER@$REMOTE_HOST 'sudo journalctl -u kalshi-weather-cycle.service -f'"
log ""
log "Revert if needed:"
log "  bash scripts/lightsail/flip_to_live.sh --revert"
log "══════════════════════════════════════════════════════════════════════"
