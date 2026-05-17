#!/bin/zsh
# Run a single weather decision cycle + survey report.
# Designed to be invoked every 30 min by launchd.
#
# Logs go to logs/cron_cycle.log (rotated weekly via launchd plist).

set -uo pipefail

ROOT="/Users/varmakammili/Documents/GitHub/KalshiWeatherTest"
PY="/opt/anaconda3/bin/python3"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/cron_cycle.log"

mkdir -p "$LOG_DIR"

# Rotate log if > 50 MB (was 5 MB — rotated too aggressively, lost selector
# traces within hours. Real audit trail now lives in cycle_selections table
# which persists across rotations regardless.)
if [[ -f "$LOG_FILE" && $(stat -f%z "$LOG_FILE") -gt 52428800 ]]; then
  mv "$LOG_FILE" "$LOG_FILE.$(date +%Y%m%d_%H%M%S)"
fi

cd "$ROOT"
export PYTHONPATH="src"

# Kalshi private API credentials (for live order placement).
# These point at the same key already used by the PolyMarketTestBot live lane.
export KALSHI_API_KEY_ID="2d618372-e5bb-4515-a5a5-0e41b4717ad6"
export KALSHI_PRIVATE_KEY_PATH="/Users/varmakammili/.kalshi/private_key.pem"

# Live order placement flags.
#   LIVE_ORDERS_ENABLED=1  → bot may place real orders (otherwise no-op)
#   LIVE_ORDERS_DRY_RUN=0  → submit real Kalshi orders (1 = preview only)
#   LIVE_DAILY_USD_CAP     → max $/day across all live orders (default $10)
#
# LIVE-MONEY MODE active since 2026-05-16 evening ET (per user request).
# To revert to preview-only: set LIVE_ORDERS_DRY_RUN=1.
# To fully disable live orders: set LIVE_ORDERS_ENABLED=0.
: ${LIVE_ORDERS_ENABLED:=1}
: ${LIVE_ORDERS_DRY_RUN:=0}
# Day-1 cap: $15. 1 contract per market still hardcoded in live_execution.py.
: ${LIVE_DAILY_USD_CAP:=15.0}
export LIVE_ORDERS_ENABLED LIVE_ORDERS_DRY_RUN LIVE_DAILY_USD_CAP

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Heartbeat — overwritten at cycle start and again at cycle end. Watchdog and
# healthcheck consume this for an O(1) "is the bot alive?" answer without
# scanning the multi-MB cron_cycle.log. The file is intentionally small.
HEARTBEAT_FILE="$LOG_DIR/heartbeat.txt"

write_heartbeat() {
  printf 'phase=%s\nutc=%s\nhost_pid=%s\n' "$1" "$(ts)" "$$" > "$HEARTBEAT_FILE"
}

# Inter-scheduler dedup — if BOTH launchd AND cron fire within the same
# minute (clock slop), only one should run. Use a lock file with a short
# TTL to coordinate.
LOCK_FILE="$LOG_DIR/cycle.lock"
# 2026-05-17: raised from 270 → 420. Cycles now take 250-310s after KXLOW
# activation doubled the market count. Old 270s TTL was expiring before
# the cycle finished, allowing the next scheduled slot's cycle to start
# overlapping. Two cycles writing to the same DB and heartbeat is bad
# state. 420 = 7 min, comfortably above MAX_CYCLE_SECONDS (360) plus
# slack for the SIGALRM signal-delivery delay.
LOCK_TTL_SECONDS=420
if [[ -f "$LOCK_FILE" ]]; then
  lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE") ))
  if (( lock_age < LOCK_TTL_SECONDS )); then
    echo "[$(ts)] another cycle holds lock (age ${lock_age}s) — skipping" >> "$LOG_FILE"
    exit 0
  fi
  # Stale lock — clear it
  rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
# Always clean up on exit
trap 'rm -f "$LOCK_FILE"' EXIT

write_heartbeat "cycle_start"
echo "[$(ts)] ────── cycle start ──────" >> "$LOG_FILE"

# Ensure Ollama daemon is up for AFD extraction. Non-fatal if it fails —
# afd_extractor.py degrades gracefully (returns extraction_failed=True).
if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  /usr/local/bin/ollama serve >> "$LOG_DIR/ollama.log" 2>&1 &
  sleep 2
fi

# Run city cycle — full output goes to log, with summary at the end
if "$PY" -m kalshi_weather.tools.run_city_cycle >>"$LOG_FILE" 2>&1; then
  echo "[$(ts)] run_city_cycle OK" >> "$LOG_FILE"
else
  echo "[$(ts)] ✗ run_city_cycle FAILED (exit=$?)" >> "$LOG_FILE"
fi

# Then refresh the survey report
if "$PY" -m kalshi_weather.tools.survey_opportunities >>"$LOG_FILE" 2>&1; then
  echo "[$(ts)] survey_opportunities OK" >> "$LOG_FILE"
else
  echo "[$(ts)] ✗ survey_opportunities FAILED (exit=$?)" >> "$LOG_FILE"
fi

write_heartbeat "cycle_end"
echo "[$(ts)] ────── cycle end ──────" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# Append per-cycle report entry. Pulls real data from cron_cycle.log + DB.
# Never fabricates — if a field can't be derived, it says "unknown".
"$ROOT/scripts/write_cycle_report.sh" 2>>"$LOG_FILE" || true

