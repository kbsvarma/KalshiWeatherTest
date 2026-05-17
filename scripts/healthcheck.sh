#!/bin/zsh
# Bot liveness check. Run this BEFORE answering any "is the bot ok" question.
# Exits 0 if healthy, 1 if down.
#
# Healthy means:
#   - Both launchd jobs are loaded
#   - Most recent launchd exit code is 0 (not 127, not anything else)
#   - Last "cycle end" in cron_cycle.log is < 35 minutes ago
#
# Usage:
#   scripts/healthcheck.sh           # human-readable status + exit code
#   scripts/healthcheck.sh --quiet   # exit code only

set -uo pipefail

ROOT="/Users/varmakammili/Documents/GitHub/KalshiWeatherTest"
LOG="$ROOT/logs/cron_cycle.log"
STDERR_CYCLE="$ROOT/logs/launchd_cycle.stderr.log"
STDERR_SETTLE="$ROOT/logs/launchd_settlements.stderr.log"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

health=0
report() { (( QUIET )) || echo "$@"; }

# 1. launchd jobs loaded?
# Use grep -E with anchored end-of-line so cycle doesn't match cycle.backup too.
cycle_line=$(launchctl list 2>/dev/null | grep -E 'com\.varmakammili\.kalshi\.weather\.cycle$' || true)
settle_line=$(launchctl list 2>/dev/null | grep com.varmakammili.kalshi.weather.settlements || true)

if [[ -z "$cycle_line" ]]; then
  report "✗ CYCLE job NOT LOADED in launchd"
  health=1
else
  cycle_exit=$(echo "$cycle_line" | awk '{print $2}')
  if [[ "$cycle_exit" != "0" && "$cycle_exit" != "-" ]]; then
    report "✗ CYCLE last exit=$cycle_exit (nonzero — failing)"
    health=1
  else
    report "✓ CYCLE loaded, last exit=$cycle_exit"
  fi
fi

if [[ -z "$settle_line" ]]; then
  report "✗ SETTLEMENTS job NOT LOADED in launchd"
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
last_end=$(grep "cycle end" "$LOG" 2>/dev/null | tail -1 | awk -F'[][]' '{print $2}')
if [[ -z "$last_end" ]]; then
  report "✗ No 'cycle end' found in $LOG — bot has never completed a cycle"
  health=1
else
  # Convert "2026-05-17T11:05:09Z" to epoch
  last_epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$last_end" "+%s" 2>/dev/null)
  now_epoch=$(date -u "+%s")
  age_min=$(( (now_epoch - last_epoch) / 60 ))
  # Convert to ET for display
  last_et=$(TZ="America/New_York" date -j -f "%s" "$last_epoch" "+%I:%M:%S %p ET")
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
    mtime_epoch=$(stat -f "%m" "$f")
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
