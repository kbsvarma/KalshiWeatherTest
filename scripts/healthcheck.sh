#!/usr/bin/env bash
# Bot liveness check. Run this BEFORE answering any "is the bot ok" question.
# Exits 0 if healthy, 1 if down.
#
# Healthy means:
#   - Cycle and settlements jobs are loaded (launchd on macOS, systemd on Linux)
#   - Most recent exit code is 0
#   - Last "cycle end" in cron_cycle.log is < 35 minutes ago
#
# Usage:
#   scripts/healthcheck.sh           # human-readable status + exit code
#   scripts/healthcheck.sh --quiet   # exit code only

set -uo pipefail

ROOT="${KALSHI_WEATHER_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
LOG="$ROOT/logs/cron_cycle.log"
STDERR_CYCLE="$ROOT/logs/launchd_cycle.stderr.log"
STDERR_SETTLE="$ROOT/logs/launchd_settlements.stderr.log"

# shellcheck source=lib/portable.sh
source "$ROOT/scripts/lib/portable.sh"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

health=0
report() { (( QUIET )) || echo "$@"; }

# 1. Cycle + settlements jobs loaded? (launchd on Darwin, systemd on Linux.)
if _is_darwin; then
  # Use grep -E with anchored end-of-line so cycle doesn't match cycle.backup too.
  cycle_line=$(launchctl list 2>/dev/null | grep -E 'com\.varmakammili\.kalshi\.weather\.cycle$' || true)
  settle_line=$(launchctl list 2>/dev/null | grep com.varmakammili.kalshi.weather.settlements || true)
else
  # systemd: oneshot services are scheduled by .timer units. They're
  # "active" only during a run, "inactive" between runs. So we check
  # the TIMER (which is "active" if scheduled to fire) rather than the
  # service itself. Last exit comes from the service's ExecMainStatus.
  _systemd_line() {
    local svc="$1"
    local timer="${svc%.service}.timer"
    if ! systemctl list-unit-files 2>/dev/null | grep -q "^$svc"; then
      echo ""
      return
    fi
    local timer_active service_active main_pid exit_status
    timer_active=$(systemctl is-active "$timer" 2>/dev/null || echo missing)
    service_active=$(systemctl is-active "$svc" 2>/dev/null || echo unknown)
    main_pid=$(systemctl show -p MainPID --value "$svc" 2>/dev/null)
    exit_status=$(systemctl show -p ExecMainStatus --value "$svc" 2>/dev/null)
    if [[ "$service_active" == "active" && -n "$main_pid" && "$main_pid" != "0" ]]; then
      # Currently running
      echo "$main_pid 0 $svc"
    elif [[ "$timer_active" == "active" ]]; then
      # Scheduled and waiting — that's healthy for a oneshot. Surface the
      # last exit code (0 if last run succeeded).
      echo "- ${exit_status:-0} $svc"
    else
      # No timer, no run — treat as not loaded.
      echo ""
    fi
  }
  cycle_line=$(_systemd_line kalshi-weather-cycle.service)
  [[ -z "$cycle_line" ]] && cycle_line=$(_systemd_line kxw-cycle.service)
  settle_line=$(_systemd_line kalshi-weather-settlements.service)
  [[ -z "$settle_line" ]] && settle_line=$(_systemd_line kxw-settlements.service)
fi

if [[ -z "$cycle_line" ]]; then
  report "✗ CYCLE job NOT LOADED (launchd on macOS / systemd on Linux)"
  health=1
else
  cycle_pid=$(echo "$cycle_line" | awk '{print $1}')
  cycle_exit=$(echo "$cycle_line" | awk '{print $2}')
  # When a job is currently running, launchctl shows its PID in col 1
  # while col 2 still has the PREVIOUS run's exit code. A stale exit code
  # from a previously-killed run (e.g., -15 from a kickstart -k) does NOT
  # mean the current run is failing. Treat in-progress as healthy.
  if [[ "$cycle_pid" != "-" && "$cycle_pid" != "0" && -n "$cycle_pid" ]]; then
    report "✓ CYCLE in progress (PID $cycle_pid, prev exit=$cycle_exit ignored)"
  elif [[ "$cycle_exit" != "0" && "$cycle_exit" != "-" ]]; then
    report "✗ CYCLE last exit=$cycle_exit (nonzero — failing)"
    health=1
  else
    report "✓ CYCLE loaded, last exit=$cycle_exit"
  fi
fi

if [[ -z "$settle_line" ]]; then
  report "✗ SETTLEMENTS job NOT LOADED (launchd on macOS / systemd on Linux)"
  health=1
else
  settle_exit=$(echo "$settle_line" | awk '{print $2}')
  if [[ "$settle_exit" != "0" && "$settle_exit" != "-" ]]; then
    report "✗ SETTLEMENTS last exit=$settle_exit (nonzero — failing)"
    health=1
  else
    report "✓ SETTLEMENTS loaded, last exit=$settle_exit"
  fi
fi

# 2. Last cycle_end timestamp
# Use the literal marker so we don't match "cycle ended" in skip log lines.
# Bug class fixed 2026-05-18 PM (same pattern as the cooldown regression).
last_end=$(grep -F "────── cycle end ──────" "$LOG" 2>/dev/null | tail -1 | awk -F'[][]' '{print $2}')
if [[ -z "$last_end" ]]; then
  report "✗ No 'cycle end' found in $LOG — bot has never completed a cycle"
  health=1
else
  # Convert "2026-05-17T11:05:09Z" to epoch
  last_epoch=$(_iso_to_epoch "$last_end")
  now_epoch=$(date -u "+%s")
  age_min=$(( (now_epoch - last_epoch) / 60 ))
  # Convert to ET for display
  last_et=$(_epoch_to_et_display "$last_epoch" "+%I:%M:%S %p ET")
  if (( age_min > 35 )); then
    report "✗ LAST CYCLE: $last_et — $age_min min ago (>35 min = DOWN)"
    health=1
  else
    report "✓ LAST CYCLE: $last_et — $age_min min ago"
  fi
fi

# 3. Recent stderr noise
for f in "$STDERR_CYCLE" "$STDERR_SETTLE"; do
  if [[ -f "$f" ]]; then
    mtime_epoch=$(_file_mtime "$f")
    now_epoch=$(date -u "+%s")
    age_min=$(( (now_epoch - mtime_epoch) / 60 ))
    if (( age_min < 60 )); then
      last_err=$(tail -1 "$f")
      if [[ -n "$last_err" ]]; then
        report "⚠ recent stderr in $(basename "$f") ${age_min}m ago: $last_err"
        # Don't fail just for stderr (could be benign), but surface it
      fi
    fi
  fi
done

# 4. Summary
if (( health == 0 )); then
  report "─── BOT HEALTHY ───"
else
  report "─── BOT DOWN — investigate ───"
fi

exit $health
