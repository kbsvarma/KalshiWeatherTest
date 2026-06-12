#!/usr/bin/env bash
# Watchdog — runs at :08 and :38 (8 min after each scheduled cycle).
# Independent of the cycle wrapper. Two jobs:
#   1. Detect if the most recent scheduled cycle (at :00 or :30) actually ran.
#      If not, write a MISSED/FAILED entry to today's cycle report with the
#      reason from launchd/systemd stderr.
#   2. Drop a flag file when the bot is down.
#
# Platform-portable: ROOT env-overridable, stat/date via portable.sh,
# launchctl calls guarded by ``_is_darwin``.

set -uo pipefail

ROOT="${KALSHI_WEATHER_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
HEALTHCHECK="$ROOT/scripts/healthcheck.sh"
REPORTER="$ROOT/scripts/write_cycle_report.sh"
LOG="$ROOT/logs/cron_cycle.log"
STDERR_CYCLE="$ROOT/logs/launchd_cycle.stderr.log"
FLAG="$ROOT/logs/BOT_DOWN.flag"
WATCH_LOG="$ROOT/logs/watchdog.log"

# shellcheck source=lib/portable.sh
source "$ROOT/scripts/lib/portable.sh"

ts() { TZ="America/New_York" date "+%Y-%m-%d %I:%M:%S %p ET"; }

# Determine the slot this watchdog is checking.
# Watchdog fires at :08 or :38 ET. The slot it's checking is the :00 or :30
# that JUST passed — i.e. up to 8 min in the past.
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
slot_epoch=$(_et_slot_to_epoch "$slot_date" "${slot_hour_24}:${slot_min}")

# Find the most recent cycle_end in the log AFTER the slot time.
# ALSO check if a cycle is CURRENTLY RUNNING for this slot via heartbeat —
# cycles can take 5+ min when KXLOW + KXHIGH are both active (36 series),
# and the watchdog used to false-alarm when the cycle was still running.
slot_ran="no"
HEARTBEAT="$ROOT/logs/heartbeat.txt"
if [[ -n "$slot_epoch" ]]; then
  # Literal marker — must NOT match "cycle ended" in skip-log lines (same
  # bug class as the 2026-05-18 PM cooldown regression).
  last_end_utc=$(grep -F "────── cycle end ──────" "$LOG" 2>/dev/null | tail -1 | awk -F'[][]' '{print $2}')
  if [[ -n "$last_end_utc" ]]; then
    last_end_epoch=$(_iso_to_epoch "$last_end_utc")
    # Case A: cycle ended after slot — success
    if (( last_end_epoch >= slot_epoch )); then
      slot_ran="yes"
    fi
    # Case A2: a cycle ended within the cooldown window BEFORE the slot.
    # The wrapper's cooldown gate (COOLDOWN_SECONDS in run_weather_cycle.sh)
    # legitimately skips the scheduled slot in this case, and the slot's
    # intent — "ensure recent work" — was satisfied by the prior cycle.
    # Without this case we false-alarm whenever an off-schedule cycle
    # (manual kickstart, or backup-scheduler failover) lands shortly
    # before a scheduled slot. Caught by 2026-05-18 PM audit after
    # the 02:30 PM ET slot was marked FAILED following a 2:21 kickstart
    # that ended at 2:29:41 (22s before the slot).
    #
    # Must stay in sync with COOLDOWN_SECONDS in run_weather_cycle.sh
    # (720s = 12 min as of 2026-05-19). The test_config_consistency.py
    # file pins this — change both values together or the test will fail.
    COOLDOWN_SLOT_COVERAGE_SECONDS=720
    if [[ "$slot_ran" == "no" ]]; then
      pre_slot_age=$(( slot_epoch - last_end_epoch ))
      if (( pre_slot_age > 0 && pre_slot_age < COOLDOWN_SLOT_COVERAGE_SECONDS )); then
        # Treat as success — keep the value as "yes" so downstream branches
        # match. The log line below preserves the diagnostic distinction.
        slot_ran="yes"
        echo "[$(ts)] slot=$slot_et covered by pre-slot cycle (ended ${pre_slot_age}s before slot)" >> "$WATCH_LOG"
      fi
    fi
  fi
  # Case B: cycle is currently in-flight. Heartbeat shows phase=cycle_start
  # with a fresh utc timestamp. Don't false-alarm.
  #
  # 2026-05-18 fix: previously required ``hb_epoch >= slot_epoch`` AND
  # ``age < 360s``. Both gates were too tight:
  #   1) slot_epoch computed via ``date -j -f`` was occasionally a few
  #      seconds ahead of the actual cycle_start timestamp (observed +5s
  #      drift), causing false FAILEDs for the :00 and :30 slots when the
  #      heartbeat landed at e.g. 14:00:03 vs slot_epoch 14:00:05.
  #   2) 360s cap was tighter than MAX_CYCLE_SECONDS (now 600s) — cycles
  #      legitimately running 5:20 would flip from "in_progress" to
  #      "FAILED" mid-cycle.
  #
  # Relaxed: heartbeat needs to be within ``MAX_CYCLE_SECONDS+60`` (660s)
  # of now AND not from a previous slot (>60s before slot_epoch rules out
  # the cycle that ran 15+ min ago). The lock file already prevents two
  # cycles from overlapping, so we don't need a second guard here.
  if [[ "$slot_ran" == "no" && -f "$HEARTBEAT" ]]; then
    if grep -q "phase=cycle_start" "$HEARTBEAT" 2>/dev/null; then
      hb_utc=$(grep "^utc=" "$HEARTBEAT" | head -1 | cut -d= -f2)
      if [[ -n "$hb_utc" ]]; then
        hb_epoch=$(_iso_to_epoch "$hb_utc")
        now_epoch=$(date "+%s")
        # Allow 60s of slop between slot_epoch and hb_epoch (clock skew,
        # launchd fire timing). And cap "in progress" at MAX_CYCLE_SECONDS
        # + 120s slack = 1020s. (MAX_CYCLE_SECONDS=900 in run_city_cycle.py.)
        if (( hb_epoch >= slot_epoch - 60 && (now_epoch - hb_epoch) < 1020 )); then
          slot_ran="in_progress"
        fi
      fi
    fi
  fi
fi

# If cycle is still running, exit quietly — no false alarm.
if [[ "$slot_ran" == "in_progress" ]]; then
  echo "[$(ts)] slot=$slot_et cycle in progress (heartbeat fresh), no action" >> "$WATCH_LOG"
  exit 0
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
    hb_mtime=$(_file_mtime "$HEARTBEAT")
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
        # Clear the stale lock so next cycle can run. The lock changed from
        # file → dir on 2026-05-18 (atomic-mkdir TOCTOU fix). Handle both
        # legacy file + new dir paths so old in-flight installs don't break.
        rm -rf "$ROOT/logs/cycle.lockdir"
        rm -f  "$ROOT/logs/cycle.lock"
      else
        kill_result="pid_gone"
      fi
      "$REPORTER" "FAILED" "$slot_et" "stuck_cycle_age=${age_sec}s_pid=${stuck_pid}_kill=${kill_result}" 2>>"$WATCH_LOG" || true
      echo "[$(ts)] STUCK CYCLE: pid=$stuck_pid age=${age_sec}s kill=$kill_result" >> "$WATCH_LOG"
    fi
  fi
  exit 0
fi

# Slot did NOT run. Before classifying as FAILED, check whether the wrapper
# DID fire for this slot but intentionally skipped due to lock-held or
# cooldown. Those are legitimate skips, not failures.
#
# 2026-05-23: false-FAILED at 2:00 PM ET happened because the 1:58 manual
# cycle was still running when the scheduled 2:00 slot fired. The wrapper
# logged "another cycle holds lock — skipping" and exited 0. The watchdog
# saw launchd exit=0 with no "cycle end" and marked it FAILED.
CRON_LOG="$ROOT/logs/cron_cycle.log"
if [[ -f "$CRON_LOG" ]]; then
  # Look at the last 30 lines for a skip that landed in this slot's window.
  # Slot window = slot_epoch ± 90s (launchd jitter + clock skew).
  slot_window_start=$(( slot_epoch - 90 ))
  slot_window_end=$(( slot_epoch + 90 ))
  if tail -30 "$CRON_LOG" | python3 -c "
import sys, re, datetime
ws, we = ${slot_window_start}, ${slot_window_end}
for line in sys.stdin:
    m = re.match(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]', line)
    if not m:
        continue
    if 'skipping' not in line:
        continue
    try:
        t = int(datetime.datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        continue
    if ws <= t <= we:
        sys.exit(0)  # found a skip in this slot's window
sys.exit(1)
" 2>/dev/null; then
    echo "[$(ts)] slot=$slot_et SKIPPED (wrapper skipped via lock/cooldown — not a failure)" >> "$WATCH_LOG"
    exit 0
  fi
fi

# Look at the last line of stderr; if recent, that's the failure.
reason="unknown"
if [[ -f "$STDERR_CYCLE" ]]; then
  stderr_mtime=$(_file_mtime "$STDERR_CYCLE")
  now_epoch=$(date -u "+%s")
  age_min=$(( (now_epoch - stderr_mtime) / 60 ))
  if (( age_min < 10 )); then
    reason=$(tail -1 "$STDERR_CYCLE")
  fi
fi

# Also check scheduler-specific exit code for additional context.
# Darwin: launchctl. Linux: systemd via `systemctl show` on the cycle unit
# (whichever name the install used — try both).
if _is_darwin; then
  # 2026-05-28: pin to the PRIMARY cycle agent (exact match) so that
  # when the backup agent is also loaded we don't get a multi-line
  # exit-code that breaks downstream rendering (e.g., "0\n0" → reason
  # field carries a stray newline → dashboard cycle log renders an
  # orphan row reading just "0)" between the real rows).
  exit_code=$(launchctl list 2>/dev/null | awk '$3=="com.varmakammili.kalshi.weather.cycle"{print $2; exit}')
  if [[ -n "$exit_code" && "$exit_code" != "0" && "$exit_code" != "-" ]]; then
    reason="${reason} (launchd exit=${exit_code})"
  fi
else
  # systemd path — show the last ExecMainStatus of the cycle service if it exists.
  for unit in kalshi-weather-cycle.service kxw-cycle.service; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^$unit"; then
      exit_code=$(systemctl show -p ExecMainStatus --value "$unit" 2>/dev/null)
      if [[ -n "$exit_code" && "$exit_code" != "0" ]]; then
        reason="${reason} (systemd exit=${exit_code})"
      fi
      break
    fi
  done
fi

# Decide MISSED vs FAILED
mode="MISSED"
if [[ "$reason" != "unknown" ]]; then
  mode="FAILED"
fi

# Diagnostic capture — when we decide FAILED/MISSED, record the full state
# so the next race condition is debuggable instead of a guess. The midnight
# 2026-05-18 false alarm went undiagnosed because we couldn't reconstruct
# heartbeat/last_end/slot epochs after the fact.
{
  now_epoch_dbg=$(date "+%s")
  echo "[$(ts)] DIAG ${mode} slot=${slot_et}"
  echo "  slot_epoch=${slot_epoch:-unset} now_epoch=${now_epoch_dbg}"
  echo "  last_end_utc=${last_end_utc:-none} last_end_epoch=${last_end_epoch:-0}"
  if [[ -f "$HEARTBEAT" ]]; then
    hb_mtime_dbg=$(_file_mtime "$HEARTBEAT")
    echo "  heartbeat_mtime=${hb_mtime_dbg} age_s=$((now_epoch_dbg - hb_mtime_dbg))"
    echo "  heartbeat_contents:"
    sed 's/^/    /' "$HEARTBEAT"
  else
    echo "  heartbeat: missing"
  fi
  echo "  reason=${reason}"
} >> "$WATCH_LOG"

# Write the cycle report entry
"$REPORTER" "$mode" "$slot_et" "$reason" 2>>"$WATCH_LOG" || true

# Log + flag only — no notifications. Status surfaces via the daily report.
echo "[$(ts)] ${mode} slot=${slot_et} reason=${reason}" >> "$WATCH_LOG"
echo "$(ts) — ${mode} slot=${slot_et}" > "$FLAG"
