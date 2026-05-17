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

# T1.2/T1.3 visibility — query the decisions saved in this cycle window
# for aggregate stats. Pulled from real DB rows; "—" when no data.
priors_stats=$(sqlite3 "$DB" "SELECT payload_json FROM decisions WHERE as_of_time >= '${start_utc%Z}' AND as_of_time <= '${end_utc%Z}';" 2>/dev/null \
  | python3 -c "
import json, sys, statistics
clim_hits = pers_hits = pers_inflated = 0
shrink_deltas = []
pers_gaps = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    ps = d.get('path_state') or {}
    if ps.get('p_climatology') is not None:
        clim_hits += 1
        try:
            pre = float(ps.get('p_pre_shrinkage') or 0)
            post = float(ps.get('p_yes') or 0)
            shrink_deltas.append(abs(pre - post))
        except Exception:
            pass
    if ps.get('persistence_gap_f') is not None:
        pers_hits += 1
        try:
            g = float(ps.get('persistence_gap_f'))
            pers_gaps.append(abs(g))
        except Exception:
            pass
        try:
            if float(ps.get('persistence_uncertainty_addon') or 0) > 0:
                pers_inflated += 1
        except Exception:
            pass
mean_shrink = round(statistics.mean(shrink_deltas), 4) if shrink_deltas else 0
max_shrink = round(max(shrink_deltas), 4) if shrink_deltas else 0
mean_gap = round(statistics.mean(pers_gaps), 2) if pers_gaps else 0
max_gap = round(max(pers_gaps), 2) if pers_gaps else 0
print(f'{clim_hits}|{mean_shrink}|{max_shrink}|{pers_hits}|{pers_inflated}|{mean_gap}|{max_gap}')
" 2>/dev/null || echo "0|0|0|0|0|0|0")
clim_hits=$(echo "$priors_stats" | cut -d'|' -f1)
mean_shrink=$(echo "$priors_stats" | cut -d'|' -f2)
max_shrink=$(echo "$priors_stats" | cut -d'|' -f3)
pers_hits=$(echo "$priors_stats" | cut -d'|' -f4)
pers_inflated=$(echo "$priors_stats" | cut -d'|' -f5)
mean_pers_gap=$(echo "$priors_stats" | cut -d'|' -f6)
max_pers_gap=$(echo "$priors_stats" | cut -d'|' -f7)

# Build JSON row via python for clean escaping
python3 - "$start_utc" "$end_utc" "$cycle_status" "$market_count" "$taker_cand" \
              "$taker_zero" "$selected_count" "$rejected_count" \
              "$selected_tickers" "$fills_in_window" "$live_in_window" "$rej_reasons" "$now_iso" \
              "$clim_hits" "$mean_shrink" "$max_shrink" "$pers_hits" "$pers_inflated" \
              "$mean_pers_gap" "$max_pers_gap" \
              "$DATA_FILE" <<'PY'
import json, sys
(start_utc, end_utc, status, mkt, tc, t0, sel, rej,
 tickers_csv, fills, live, rej_csv, now_iso,
 clim_hits, mean_shrink, max_shrink, pers_hits, pers_inflated, mean_pers_gap, max_pers_gap,
 data_file) = sys.argv[1:]

tickers = [t for t in tickers_csv.split(",") if t]
rej_pairs = []
for chunk in rej_csv.split("|"):
    if ":" in chunk:
        name, n = chunk.rsplit(":", 1)
        try:
            rej_pairs.append({"reason": name, "count": int(n)})
        except ValueError:
            pass

def _f(s):
    try: return float(s)
    except Exception: return 0.0

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
    # T1.2 climatology shrinkage observability
    "climatology_hit_count": int(clim_hits),
    "climatology_mean_shrink_delta": _f(mean_shrink),
    "climatology_max_shrink_delta": _f(max_shrink),
    # T1.3 persistence observability
    "persistence_hit_count": int(pers_hits),
    "persistence_inflation_triggered_count": int(pers_inflated),
    "persistence_mean_abs_gap_f": _f(mean_pers_gap),
    "persistence_max_abs_gap_f": _f(max_pers_gap),
}
with open(data_file, "a") as f:
    f.write(json.dumps(row) + "\n")
PY

# Trigger the render
"$RENDER_SCRIPT" "$today_et" 2>/dev/null || true
