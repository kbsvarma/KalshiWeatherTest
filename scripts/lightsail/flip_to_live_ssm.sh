#!/usr/bin/env bash
# Flip Kalshi weather bot PAPER → LIVE on Lightsail, via SSM (no SSH needed).
# Mirror of flip_to_live.sh but uses ``aws ssm send-command`` instead of SSH.
#
# Pre-flight checks (each must pass):
#   1. Lightsail managed-instance is Online in SSM
#   2. At least N successful dry-run cycles in cron_cycle.log
#   3. Mac launchd cycle is currently loaded
#   4. Operator confirmation
#
# Action:
#   1. Disable Mac launchd cycle (primary + backup) — first
#   2. Drop systemd override on Lightsail flipping LIVE_ORDERS_ENABLED=1
#   3. systemctl daemon-reload + restart cycle service
#   4. Trigger one cycle and tail the log
#
# Revert:
#   bash scripts/lightsail/flip_to_live_ssm.sh --revert

set -euo pipefail

INSTANCE="${KALSHI_LIGHTSAIL_SSM:-mi-0d7014ef3ea88eee3}"
REGION="${AWS_REGION:-us-east-1}"
MIN_DRY_RUN_CYCLES="${MIN_DRY_RUN_CYCLES:-3}"
MAC_PRIMARY_PLIST="$HOME/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.plist"
MAC_BACKUP_PLIST="$HOME/Library/LaunchAgents/com.varmakammili.kalshi.weather.cycle.backup.plist"
LIVE_DAILY_USD_CAP="${LIVE_DAILY_USD_CAP:-15.0}"

log() { echo "[flip] $*"; }

run_remote() {
  local script="$1"
  local b64
  b64=$(echo "$script" | base64 | tr -d '\n')
  local cmd_id
  cmd_id=$(aws ssm send-command \
    --instance-ids "$INSTANCE" \
    --document-name AWS-RunShellScript \
    --parameters "{\"commands\":[\"echo '$b64' | base64 -d | bash\"]}" \
    --region "$REGION" \
    --timeout-seconds 300 \
    --query Command.CommandId --output text)
  local status
  for _ in $(seq 1 40); do
    sleep 4
    status=$(aws ssm get-command-invocation --command-id "$cmd_id" \
      --instance-id "$INSTANCE" --region "$REGION" \
      --query Status --output text 2>/dev/null || echo Pending)
    [[ "$status" == "Success" || "$status" == "Failed" ]] && break
  done
  aws ssm get-command-invocation --command-id "$cmd_id" \
    --instance-id "$INSTANCE" --region "$REGION" \
    --query StandardOutputContent --output text
  if [[ "$status" != "Success" ]]; then
    echo "[flip] remote command failed (status=$status)" >&2
    exit 1
  fi
}

if [[ "${1:-}" == "--revert" ]]; then
  log "REVERTING — Lightsail → PAPER, Mac → LIVE"
  run_remote 'sudo rm -f /etc/systemd/system/kalshi-weather-cycle.service.d/live.conf && sudo systemctl daemon-reload && echo "Lightsail override removed"'
  if [[ -f "$MAC_PRIMARY_PLIST" ]]; then
    launchctl load "$MAC_PRIMARY_PLIST" 2>/dev/null || true
    launchctl load "$MAC_BACKUP_PLIST" 2>/dev/null || true
    log "Mac launchd cycle re-enabled"
  fi
  log "Revert complete."
  exit 0
fi

# ── Pre-flight 1: SSM reachable ─────────────────────────────────────────
log "Pre-flight 1/4: Lightsail SSM Online?"
PING=$(aws ssm describe-instance-information --instance-information-filter-list 'key=InstanceIds,valueSet='"$INSTANCE" --region "$REGION" --query 'InstanceInformationList[0].PingStatus' --output text 2>&1)
if [[ "$PING" != "Online" ]]; then
  log "  FAIL: PingStatus=$PING"
  exit 1
fi
log "  OK"

# ── Pre-flight 2: dry-run cycle count ───────────────────────────────────
log "Pre-flight 2/4: ≥ $MIN_DRY_RUN_CYCLES successful dry-run cycles?"
COUNT=$(run_remote 'grep -cF "────── cycle end ──────" /opt/kalshi-weather/logs/cron_cycle.log 2>/dev/null || echo 0' | tr -d '\n[:space:]')
log "  Cycles: $COUNT (need $MIN_DRY_RUN_CYCLES)"
if (( COUNT < MIN_DRY_RUN_CYCLES )); then
  log "  FAIL — wait for more cycles to validate before flipping"
  exit 1
fi

# ── Pre-flight 3: Mac launchd state ─────────────────────────────────────
log "Pre-flight 3/4: Mac launchd cycle loaded?"
if launchctl list 2>/dev/null | grep -q "com.varmakammili.kalshi.weather.cycle$"; then
  log "  OK — Mac is live"
  MAC_LOADED=1
else
  log "  Mac not loaded; proceeding anyway"
  MAC_LOADED=0
fi

# ── Pre-flight 4: operator confirmation ─────────────────────────────────
echo ""
echo "  ⚠  About to flip Kalshi weather bot to LIVE on Lightsail ($INSTANCE)"
echo "     - Mac launchd will be DISABLED (no more Mac-side orders)"
echo "     - Lightsail LIVE_ORDERS_ENABLED=1, LIVE_ORDERS_DRY_RUN=0"
echo "     - Daily cap: \$${LIVE_DAILY_USD_CAP}"
echo ""
read -p "  Type FLIP to proceed: " -r confirm
[[ "$confirm" == "FLIP" ]] || { log "Aborted"; exit 0; }

# ── ACTION 1: stop Mac ──────────────────────────────────────────────────
if (( MAC_LOADED )); then
  log "Action 1/3: disabling Mac launchd cycle"
  launchctl unload "$MAC_PRIMARY_PLIST" 2>/dev/null || true
  launchctl unload "$MAC_BACKUP_PLIST" 2>/dev/null || true
fi

# ── ACTION 2: drop systemd override on Lightsail ────────────────────────
log "Action 2/3: writing systemd live override"
run_remote "sudo mkdir -p /etc/systemd/system/kalshi-weather-cycle.service.d && sudo tee /etc/systemd/system/kalshi-weather-cycle.service.d/live.conf >/dev/null <<EOF
[Service]
Environment=LIVE_ORDERS_ENABLED=1
Environment=LIVE_ORDERS_DRY_RUN=0
Environment=LIVE_DAILY_USD_CAP=$LIVE_DAILY_USD_CAP
EOF
sudo systemctl daemon-reload && echo override-installed"

# ── ACTION 3: trigger validating cycle ──────────────────────────────────
log "Action 3/3: triggering one cycle to validate"
run_remote 'sudo systemctl start kalshi-weather-cycle.service && echo "cycle started (runs ~5 min)"'

log ""
log "════════════════════════════════════════════════════════════════════════"
log "FLIP COMPLETE"
log "════════════════════════════════════════════════════════════════════════"
log "  Lightsail: LIVE_ORDERS_ENABLED=1, daily cap=\$$LIVE_DAILY_USD_CAP"
log "  Mac: cycle disabled"
log ""
log "Monitor:"
log "  bash scripts/lightsail/ssm.sh status"
log "  bash scripts/lightsail/ssm.sh logs"
log "  bash scripts/lightsail/ssm.sh report"
log ""
log "Revert:"
log "  bash scripts/lightsail/flip_to_live_ssm.sh --revert"
log "════════════════════════════════════════════════════════════════════════"
