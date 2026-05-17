#!/bin/zsh
# Per-cycle reporting — two-stage design.
#
#   STAGE 1 (this script): append a structured row to a JSONL data file.
#                          Every value comes from a real query. No fabrication.
#   STAGE 2 (render_report.sh): read the JSONL and rebuild the human-readable
#                               markdown report.
#
# Called by:
#   - run_weather_cycle.sh after a successful cycle  → records OK
#   - watchdog.sh                                    → records MISSED / FAILED
#
# Args (watchdog only):
#   $1 = "MISSED" | "FAILED"
#   $2 = slot label, e.g. "08:00 AM ET"
#   $3 = reason string

set -uo pipefail

ROOT="/Users/varmakammili/Documents/GitHub/KalshiWeatherTest"
LOG="$ROOT/logs/cron_cycle.log"
DB="$ROOT/data/state/runtime.sqlite3"

today_et=$(TZ="America/New_York" date "+%Y-%m-%d")
REPORT_DIR="$ROOT/logs/cycle_reports"
mkdir -p "$REPORT_DIR"
DATA_FILE="$REPORT_DIR/${today_et}.jsonl"
RENDER_SCRIPT="$ROOT/scripts/render_report.sh"

mode="${1:-OK}"

# JSON helper — escape a string for JSON.
json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip()))' 2>/dev/null \
    || printf '"%s"' "$(echo "$1" | sed 's/"/\\"/g')"
}

now_iso=$(date -u "+%Y-%m-%dT%H:%M:%SZ")

if [[ "$mode" == "MISSED" || "$mode" == "FAILED" ]]; then
  slot="${2:-unknown}"
  reason="${3:-unknown}"
  reason_json=$(echo "$reason" | json_escape)
  printf '{"status":"%s","slot":"%s","recorded_at_utc":"%s","reason":%s}\n' \
    "$mode" "$slot" "$now_iso" "$reason_json" >> "$DATA_FILE"
  "$RENDER_SCRIPT" "$today_et" 2>/dev/null || true
  exit 0
fi

# ── OK mode: parse the most recent cycle from cron_cycle.log ──

last_start_line=$(grep -n "cycle start" "$LOG" | tail -1)
last_end_line=$(grep -n "cycle end" "$LOG" | tail -1)

if [[ -z "$last_start_line" || -z "$last_end_line" ]]; then
  printf '{"status":"REPORT_ERROR","slot":"unknown","recorded_at_utc":"%s","reason":"no cycle markers in log"}\n' \
    "$now_iso" >> "$DATA_FILE"
  "$RENDER_SCRIPT" "$today_et" 2>/dev/null || true
  exit 0
fi

start_lineno=${last_start_line%%:*}
end_lineno=${last_end_line%%:*}
start_utc=$(echo "$last_start_line" | awk -F'[][]' '{print $2}')
end_utc=$(echo "$last_end_line" | awk -F'[][]' '{print $2}')

# Slice this cycle's section to a tempfile.
section_tmp=$(mktemp -t kxw_section.XXXXXX)
trap "rm -f \"$section_tmp\"" EXIT
sed -n "${start_lineno},${end_lineno}p" "$LOG" > "$section_tmp"

if grep -q "run_city_cycle OK" "$section_tmp"; then
  cycle_status="OK"
elif grep -q "run_city_cycle FAILED" "$section_tmp"; then
  cycle_status="FAILED"
else
  cycle_status="UNKNOWN"
fi

market_count=$(grep -oE '"market_count": *[0-9]+' "$section_tmp" | grep -oE '[0-9]+' | sort -rn | head -1)
[[ -z "$market_count" ]] && market_count=0

taker_cand=$(grep -c '"taker_candidate_count": *1' "$section_tmp" | tr -d '[:space:]')
taker_zero=$(grep -c '"taker_candidate_count": *0' "$section_tmp" | tr -d '[:space:]')

# selected tickers (accepted=true entries)
selected_block=$(awk '/"selected": \[/,/^    \]/' "$section_tmp")
selected_count=$(echo "$selected_block" | grep -c '"accepted": true' | tr -d '[:space:]')
selected_tickers=$(echo "$selected_block" | grep -oE 'KX[A-Z0-9]+-[0-9]{2}[A-Z]{3}[0-9]{2}-[TB][0-9.]+' | sort -u | tr '\n' ',' | sed 's/,$//')

rejected_count=$(grep -oE '"rejected_count": *[0-9]+' "$section_tmp" | grep -oE '[0-9]+' | sort -rn | head -1)
[[ -z "$rejected_count" ]] && rejected_count=0

# rejection reasons (top 5)
rej_reasons=$(grep -oE '"rejection_reason": *"[^"]+"' "$section_tmp" \
  | sed 's/"rejection_reason": *"//; s/"$//' \
  | sort | uniq -c | sort -rn | head -5 \
  | awk '{cnt=$1; $1=""; sub(/^ /,""); printf "%s:%d|", $0, cnt}' \
  | sed 's/|$//')

# DB-side counts during cycle window
fills_in_window=$(sqlite3 "$DB" "SELECT count(*) FROM shadow_fills WHERE fill_time >= '${start_utc%Z}' AND fill_time <= '${end_utc%Z}';" 2>/dev/null)
[[ -z "$fills_in_window" ]] && fills_in_window=0
live_in_window=$(sqlite3 "$DB" "SELECT count(*) FROM live_orders WHERE created_at >= '${start_utc%Z}' AND created_at <= '${end_utc%Z}';" 2>/dev/null)
[[ -z "$live_in_window" ]] && live_in_window=0

# Build JSON row via python for clean escaping
python3 - "$start_utc" "$end_utc" "$cycle_status" "$market_count" "$taker_cand" \
              "$taker_zero" "$selected_count" "$rejected_count" \
              "$selected_tickers" "$fills_in_window" "$live_in_window" "$rej_reasons" "$now_iso" \
              "$DATA_FILE" <<'PY'
import json, sys
(start_utc, end_utc, status, mkt, tc, t0, sel, rej,
 tickers_csv, fills, live, rej_csv, now_iso, data_file) = sys.argv[1:]

tickers = [t for t in tickers_csv.split(",") if t]
rej_pairs = []
for chunk in rej_csv.split("|"):
    if ":" in chunk:
        name, n = chunk.rsplit(":", 1)
        try:
            rej_pairs.append({"reason": name, "count": int(n)})
        except ValueError:
            pass

row = {
    "status": status,
    "started_utc": start_utc,
    "ended_utc": end_utc,
    "recorded_at_utc": now_iso,
    "markets_evaluated": int(mkt),
    "taker_candidates": int(tc),
    "taker_rejections": int(t0),
    "selected_count": int(sel),
    "rejected_by_selector": int(rej),
    "selected_tickers": tickers,
    "shadow_fills_in_window": int(fills),
    "live_orders_in_window": int(live),
    "top_rejection_reasons": rej_pairs,
}
with open(data_file, "a") as f:
    f.write(json.dumps(row) + "\n")
PY

# Trigger the render
"$RENDER_SCRIPT" "$today_et" 2>/dev/null || true
