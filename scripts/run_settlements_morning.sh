#!/usr/bin/env bash
# Morning settlement fetch + cycle + survey.
# Runs once daily around 12:30 UTC (after NWS CLI reports are published for
# yesterday's settlements). Backfills settlements first so the counterfactual
# resolver has fresh data when the survey runs.

set -uo pipefail

ROOT="${KALSHI_WEATHER_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
PY="${KALSHI_WEATHER_PYTHON:-/opt/anaconda3/bin/python3}"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/cron_settlements.log"

# shellcheck source=lib/portable.sh
source "$ROOT/scripts/lib/portable.sh"

mkdir -p "$LOG_DIR"

if [[ -f "$LOG_FILE" && $(_file_size "$LOG_FILE") -gt 5242880 ]]; then
  mv "$LOG_FILE" "$LOG_FILE.$(date +%Y%m%d_%H%M%S)"
fi

cd "$ROOT"
export PYTHONPATH="src"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "[$(ts)] ────── morning settlement run start ──────" >> "$LOG_FILE"

# Step 1: pull yesterday's NWS CLI settlements for all 18 cities
"$PY" -m kalshi_weather.tools.fetch_settlements >> "$LOG_FILE" 2>&1
echo "[$(ts)] fetch_settlements done (exit=$?)" >> "$LOG_FILE"

# Step 2: run a fresh decision cycle
"$PY" -m kalshi_weather.tools.run_city_cycle >> "$LOG_FILE" 2>&1
echo "[$(ts)] run_city_cycle done (exit=$?)" >> "$LOG_FILE"

# Step 3: refresh the daily summary — this also resolves counterfactual P&L
"$PY" -m kalshi_weather.tools.survey_opportunities >> "$LOG_FILE" 2>&1
echo "[$(ts)] survey_opportunities done (exit=$?)" >> "$LOG_FILE"

echo "[$(ts)] ────── morning run end ──────" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
