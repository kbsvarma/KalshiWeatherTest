#!/bin/zsh
# Watchdog — runs at :02 and :32 (2 min after each scheduled cycle).
# Independent of the cycle wrapper. Two jobs:
#   1. Detect if the most recent scheduled cycle (at :00 or :30) actually ran.
#      If not, write a MISSED/FAILED entry to today's cycle report with the
#      real reason from launchd stderr.
#   2. Fire a macOS notification + flag file when the bot is down.

set -uo pipefail

ROOT="/Users/varmakammili/Documents/GitHub/KalshiWeatherTest"
HEALTHCHECK="$ROOT/scripts/healthcheck.sh"
REPORTER="$ROOT/scripts/write_cycle_report.sh"
LOG="$ROOT/logs/cron_cycle.log"
STDERR_CYCLE="$ROOT/logs/launchd_cycle.stderr.log"
FLAG="$ROOT/logs/BOT_DOWN.flag"
WATCH_LOG="$ROOT/logs/watchdog.log"

ts() { TZ="America/New_York" date "+%Y-%m-%d %I:%M:%S %p ET"; }

# Determine the slot this watchdog is checking.
# Watchdog fires at :02 or :32 ET. The slot it's checking is the :00 or :30
# that JUST passed — i.e. up to 2 min in the past.
minute_now=$(TZ="America/New_York" date "+%M")
if (( 10#$minute_now < 30 )); then
  slot_min="00"
else
  slot_min="30"
fi
slot_hour_24=$(TZ="America/New_York" date "+%H")
slot_date=$(TZ="America/New_York" date "+%Y-%m-%d")
slot_et_display=$(TZ="America/New_York" date "+%I:%M %p" | sed "s/:..  */:${slot_min} /")
slot_et="${slot_et_display% *} ${slot_et_display##* } ET"

# Compute the slot's UTC epoch
slot_epoch=$(TZ="America/New_York" date -j -f "%Y-%m-%d %H:%M" "${slot_date} ${slot_hour_24}:${slot_min}" "+%s" 2>/dev/null || echo "")

# Find the most recent cycle_end in the log AFTER the slot time
slot_ran="no"
if [[ -n "$slot_epoch" ]]; then
  last_end_utc=$(grep "cycle end" "$LOG" 2>/dev/null | tail -1 | awk -F'[][]' '{print $2}')
  if [[ -n "$last_end_utc" ]]; then
    last_end_epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$last_end_utc" "+%s" 2>/dev/null || echo 0)
    # Slot ran if a cycle ended between slot_time and now.
    if (( last_end_epoch >= slot_epoch )); then
      slot_ran="yes"
    fi
  fi
fi

if [[ "$slot_ran" == "yes" ]]; then
  # Cycle ran — wrapper already wrote its own OK report entry. Nothing for us.
  # Clear down flag if it existed.
  if [[ -f "$FLAG" ]]; then
    rm -f "$FLAG" "$FLAG.notified" 2>/dev/null
    echo "[$(ts)] RECOVERED (slot $slot_et ran)" >> "$WATCH_LOG"
  fi
  # Stuck-cycle recovery: if the heartbeat says cycle_start but the cycle
  # never finished within its budget (240s SIGALRM, plus 60s slack = 300s),
  # the Python process may be blocked inside a C-level call that swallowed
  # the alarm signal. Force-kill the recorded pid so the next slot can
  # run. This is a real failure mode caught in production reliability lit.
  HEARTBEAT="$ROOT/logs/heartbeat.txt"
  if [[ -f "$HEARTBEAT" ]] && grep -q "phase=cycle_start" "$HEARTBEAT" 2>/dev/null; then
    hb_mtime=$(stat -f "%m" "$HEARTBEAT" 2>/dev/null || echo 0)
    now_epoch=$(date "+%s")
    age_sec=$(( now_epoch - hb_mtime ))
    if (( age_sec > 300 )); then
      stuck_pid=$(grep -o "host_pid=[0-9]*" "$HEARTBEAT" | cut -d= -f2)
      kill_result="none"
      if [[ -n "$stuck_pid" ]] && kill -0 "$stuck_pid" 2>/dev/null; then
        # First try SIGTERM (clean shutdown), then SIGKILL after 5s
        kill -TERM "$stuck_pid" 2>/dev/null
        sleep 5
        if kill -0 "$stuck_pid" 2>/dev/null; then
          kill -KILL "$stuck_pid" 2>/dev/null
          kill_result="SIGKILL"
        else
          kill_result="SIGTERM"
        fi
        # Clear the stale lock file too so next cycle can run
        rm -f "$ROOT/logs/cycle.lock"
      else
        kill_result="pid_gone"
      fi
      "$REPORTER" "FAILED" "$slot_et" "stuck_cycle_age=${age_sec}s_pid=${stuck_pid}_kill=${kill_result}" 2>>"$WATCH_LOG" || true
      echo "[$(ts)] STUCK CYCLE: pid=$stuck_pid age=${age_sec}s kill=$kill_result" >> "$WATCH_LOG"
    fi
  fi
  exit 0
fi

# Slot did NOT run. Determine reason from launchd stderr.
# Look at the last line of stderr; if recent, that's the failure.
reason="unknown"
if [[ -f "$STDERR_CYCLE" ]]; then
  stderr_mtime=$(stat -f "%m" "$STDERR_CYCLE")
  now_epoch=$(date -u "+%s")
  age_min=$(( (now_epoch - stderr_mtime) / 60 ))
  if (( age_min < 10 )); then
    reason=$(tail -1 "$STDERR_CYCLE")
  fi
fi

# Also check launchctl exit code for additional context
exit_code=$(launchctl list 2>/dev/null | grep com.varmakammili.kalshi.weather.cycle | awk '{print $2}')
if [[ -n "$exit_code" && "$exit_code" != "0" && "$exit_code" != "-" ]]; then
  reason="${reason} (launchd exit=${exit_code})"
fi

# Decide MISSED vs FAILED
mode="MISSED"
if [[ "$reason" != "unknown" ]]; then
  mode="FAILED"
fi

# Write the cycle report entry
"$REPORTER" "$mode" "$slot_et" "$reason" 2>>"$WATCH_LOG" || true

# Log + flag only — no notifications. Status surfaces via the daily report.
echo "[$(ts)] ${mode} slot=${slot_et} reason=${reason}" >> "$WATCH_LOG"
echo "$(ts) — ${mode} slot=${slot_et}" > "$FLAG"
