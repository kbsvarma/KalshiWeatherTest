#!/usr/bin/env bash
# Run a single weather decision cycle + survey report.
# Designed to be invoked every 30 min by launchd (macOS) or systemd (Linux).
#
# Logs go to logs/cron_cycle.log (rotated weekly).
#
# Cloud-portable as of 2026-05-18: ROOT and PY are env-overridable so the
# same script works on Mac (defaults) and Lightsail/EC2 (env-injected by
# systemd unit). Platform-specific commands (stat, date) go through
# ``scripts/lib/portable.sh``.

set -uo pipefail

ROOT="${KALSHI_WEATHER_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
PY="${KALSHI_WEATHER_PYTHON:-/opt/anaconda3/bin/python3}"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/cron_cycle.log"

# shellcheck source=lib/portable.sh
source "$ROOT/scripts/lib/portable.sh"

mkdir -p "$LOG_DIR"

# Rotate log if > 50 MB (was 5 MB — rotated too aggressively, lost selector
# traces within hours. Real audit trail now lives in cycle_selections table
# which persists across rotations regardless.)
if [[ -f "$LOG_FILE" && $(_file_size "$LOG_FILE") -gt 52428800 ]]; then
  mv "$LOG_FILE" "$LOG_FILE.$(date +%Y%m%d_%H%M%S)"
fi

cd "$ROOT"
export PYTHONPATH="src"

# Kalshi private API credentials (for live order placement).
# Env-overridable so the cloud unit can supply its own paths/secrets
# (e.g., from SSM Parameter Store on Lightsail).
: ${KALSHI_API_KEY_ID:="2d618372-e5bb-4515-a5a5-0e41b4717ad6"}
: ${KALSHI_PRIVATE_KEY_PATH:="/Users/varmakammili/.kalshi/private_key.pem"}
export KALSHI_API_KEY_ID KALSHI_PRIVATE_KEY_PATH

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
# Cap history:
#   2026-05-16: $10 (initial live-money launch)
#   2026-05-17 eve: raised to $20 to capture marginal trades
#   2026-05-18: lowered back to $15 after fixing _daily_live_spend bug
#     — the cap had been silently non-functional (read wrong payload field,
#     always saw $0 spent), so the $20 setting was never actually tested
#     under a working gate. Returning to $15 as the safer baseline now
#     that the gate ACTUALLY enforces a ceiling. Revisit after a week of
#     real-cap data.
: ${LIVE_DAILY_USD_CAP:=15.0}
export LIVE_ORDERS_ENABLED LIVE_ORDERS_DRY_RUN LIVE_DAILY_USD_CAP

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Heartbeat — overwritten at cycle start and again at cycle end. Watchdog and
# healthcheck consume this for an O(1) "is the bot alive?" answer without
# scanning the multi-MB cron_cycle.log. The file is intentionally small.
HEARTBEAT_FILE="$LOG_DIR/heartbeat.txt"

write_heartbeat() {
  # Atomic write: tmp-file + rename. The previous ``> "$HEARTBEAT_FILE"``
  # truncates then writes — readers (watchdog, healthcheck, dashboard) can
  # land in the truncate window and read empty or partial content. rename(2)
  # is atomic on POSIX: readers always see either the full old file or the
  # full new file, never a half-written state. Bug audit 2026-05-18.
  local tmp="${HEARTBEAT_FILE}.tmp.$$"
  printf 'phase=%s\nutc=%s\nhost_pid=%s\n' "$1" "$(ts)" "$$" > "$tmp"
  mv -f "$tmp" "$HEARTBEAT_FILE"
}

# Inter-scheduler dedup — if BOTH launchd AND cron fire within the same
# minute (clock slop), only one should run. Use a lock to coordinate.
# 2026-05-17: raised from 270 → 420. Cycles now take 250-310s after KXLOW
# activation doubled the market count. Old 270s TTL was expiring before
# the cycle finished, allowing the next scheduled slot's cycle to start
# overlapping. Two cycles writing to the same DB and heartbeat is bad
# state.
# 2026-05-18: raised 420 → 540 → 660 → 960 in lockstep with
# MAX_CYCLE_SECONDS (240 → 480 → 600 → 900). Lock TTL must stay
# strictly above MAX_CYCLE_SECONDS so the lock doesn't release while
# a cycle is still executing. 960 = MAX_CYCLE_SECONDS + 60s for SIGALRM
# signal-delivery delay + heartbeat/cleanup overhead.
LOCK_TTL_SECONDS=960
# Atomic-mkdir lock. Replaces the previous "test -f $LOCK_FILE then echo $$ > $LOCK_FILE"
# pattern (2026-05-18 audit), which is a classic TOCTOU race: two cycles starting
# within the same millisecond could both see no lock, both write the file, and
# both run. mkdir(2) is atomic — exactly one caller succeeds, the rest fail with
# EEXIST. Stale lock-dir is handled the same way as before (TTL'd then rm-rf'd).
LOCK_DIR="$LOG_DIR/cycle.lockdir"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  # We got the lock cleanly. Stash our pid inside so the watchdog can find us.
  echo $$ > "$LOCK_DIR/pid"
else
  # Lock already held — check age. If stale (past TTL), clobber and retry once.
  lock_age=$(( $(date +%s) - $(_file_mtime "$LOCK_DIR") ))
  if (( lock_age >= LOCK_TTL_SECONDS )); then
    echo "[$(ts)] stale lock (age ${lock_age}s ≥ ${LOCK_TTL_SECONDS}s TTL) — clobbering" >> "$LOG_FILE"
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      echo "[$(ts)] lost race after stale-clobber — another cycle grabbed it; skipping" >> "$LOG_FILE"
      exit 0
    fi
    echo $$ > "$LOCK_DIR/pid"
  else
    echo "[$(ts)] another cycle holds lock (age ${lock_age}s) — skipping" >> "$LOG_FILE"
    exit 0
  fi
fi
# Always clean up on exit
trap 'rm -rf "$LOCK_DIR"' EXIT

# Cooldown gate — added 2026-05-18 PM after we caught that BOTH the primary
# (:00/:30) and backup (:15/:45) launchd plists were running real work every
# slot, doubling the effective cycle rate to every 15 min. The backup was
# supposed to be FAILOVER only ("run if primary missed"), but the lock-file
# check it relied on always finds the lock cleared because the primary
# cycle finishes (~7-8 min) before the backup's 15-min offset arrives.
#
# This skip-if-recent gate makes the script idempotent regardless of which
# scheduler invokes it: if a successful "cycle end" was logged within the
# last 20 min, we're in the middle of a healthy 30-min cadence and the
# backup should bow out. If 20+ min have passed (primary missed entirely),
# the backup runs as designed.
#
# Effects:
#   Normal case (primary fires at :00, :30): backup at :15/:45 skips → 2 cycles/hr
#   Failover case (primary :00 missed): backup at :15 runs → cadence shifts to :15/:45
# Cooldown gate semantics:
#   This must be > (backup-slot offset from primary end) so the backup
#   scheduler skips when primary just ran. AND it must be < (primary slot
#   interval - worst-case cycle duration) so the next primary doesn't
#   get starved when cycles run long.
#
#   Backup fires at :15 / :45. If primary ends at :07-:12 (5-12 min cycle),
#   backup age is 3-8 min when it fires → needs cooldown > 8 min.
#   Next primary fires at :30 / :00. If cycle ran 12 min and ended at :12,
#   primary age is 18 min when it fires → needs cooldown < 18 min.
#
# 2026-05-17: 270 → 420 (KXLOW activation slowed cycles)
# 2026-05-18 AM: 420 → 540 → 660 → 960 (after timeout creeps)
# 2026-05-18 PM-late: 1200 (added cooldown gate; was too generous)
# 2026-05-19: 1200 → 720 (12 min). 20 min cooldown was starving the
#   :30 primary whenever cycles ran > 10 min (which became the norm
#   tonight on Mac with the larger state DB). 12 min comfortably
#   skips :15/:45 backups while always letting :00/:30 primaries fire.
COOLDOWN_SECONDS=720
# CRITICAL: must match ONLY the real ────── cycle end ────── marker, NOT
# the "last cycle ended ... — skipping" log we write below. Earlier version
# used grep "cycle end" which also matched "cycle ended" via substring, so
# every skip event re-anchored the cooldown clock and the primary cycle
# silently stopped firing for ~45 min. Use fixed-string match with the
# exact marker phrase to make this impossible.
#
# FORCE_RUN=1 bypasses the cooldown. Use for manual user-initiated runs
# (operator wants to bet NOW). The cooldown exists to stop the BACKUP
# scheduler from double-firing the slot, not to throttle deliberate
# operator action.
if [[ "${FORCE_RUN:-0}" != "1" ]]; then
  last_end_utc=$(grep -F "────── cycle end ──────" "$LOG_FILE" 2>/dev/null | tail -1 | awk -F'[][]' '{print $2}')
  if [[ -n "$last_end_utc" ]]; then
    last_end_epoch=$(_iso_to_epoch "$last_end_utc")
    cycle_age=$(( $(date +%s) - last_end_epoch ))
    if (( cycle_age < COOLDOWN_SECONDS )); then
      echo "[$(ts)] last cycle ended ${cycle_age}s ago (< ${COOLDOWN_SECONDS}s cooldown) — skipping" >> "$LOG_FILE"
      exit 0
    fi
  fi
else
  echo "[$(ts)] FORCE_RUN=1 — bypassing cooldown" >> "$LOG_FILE"
fi

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

