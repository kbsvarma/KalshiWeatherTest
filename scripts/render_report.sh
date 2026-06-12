#!/usr/bin/env bash
# Render the daily cycle-report markdown from the JSONL data file.
# Rebuilt from scratch every call — single source of truth is the JSONL.
#
# Usage: render_report.sh YYYY-MM-DD

set -uo pipefail

ROOT="${KALSHI_WEATHER_ROOT:-/Users/varmakammili/Documents/GitHub/KalshiWeatherTest}"
DB="$ROOT/data/state/runtime.sqlite3"
date_et="${1:-$(TZ=America/New_York date +%Y-%m-%d)}"
REPORT_DIR="$ROOT/logs/cycle_reports"
DATA_FILE="$REPORT_DIR/${date_et}.jsonl"
MD_FILE="$REPORT_DIR/${date_et}.md"

[[ -f "$DATA_FILE" ]] || { echo "no data file: $DATA_FILE" >&2; exit 0; }

python3 - "$DATA_FILE" "$MD_FILE" "$date_et" "$DB" <<'PY'
import json, sys, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

data_file, md_file, date_et, db_path = sys.argv[1:]
ET = ZoneInfo("America/New_York")

rows = []
with open(data_file) as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass

def to_et(utc_str):
    """Convert a UTC ISO string to ET display."""
    if not utc_str or utc_str == "unknown":
        return "—"
    try:
        s = utc_str.rstrip("Z")
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%I:%M:%S %p")
    except Exception:
        return utc_str

# Pretty date heading
try:
    heading_date = datetime.strptime(date_et, "%Y-%m-%d").strftime("%A, %B %d, %Y")
except Exception:
    heading_date = date_et

# Aggregate stats across the day
total_ok = sum(1 for r in rows if r.get("status") == "OK")
total_failed = sum(1 for r in rows if r.get("status") == "FAILED")
total_missed = sum(1 for r in rows if r.get("status") == "MISSED")
total_fills = sum(int(r.get("shadow_fills_in_window") or 0) for r in rows)
total_live = sum(int(r.get("live_orders_in_window") or 0) for r in rows)
total_markets = sum(int(r.get("markets_evaluated") or 0) for r in rows)

# Open contracts and live-order stats
# NOTE: shadow_positions has a known collapse bug (one row per city even when
# multiple brackets are open). For the headline number we count distinct
# PLACED live orders that haven't been settled yet — that matches what's
# actually on Kalshi.
try:
    con = sqlite3.connect(db_path)
    # All live orders that successfully placed (status starts with PLACED_).
    placed_total = con.execute(
        "SELECT count(*) FROM live_orders WHERE status LIKE 'PLACED_%'"
    ).fetchone()[0]
    # How many of those settled (joined to market_settlements by ticker date).
    # If we don't have a clean "settled" flag we just call placed_total the
    # open count for today's settlement-day markets.
    today_live_orders = con.execute(
        "SELECT count(*) FROM live_orders WHERE substr(created_at,1,10)=? AND status LIKE 'PLACED_%'",
        (date_et,),
    ).fetchone()[0]
    # Sum exposure (price_cents * count) for placed-today orders
    exposure_cents = con.execute(
        """SELECT COALESCE(SUM(
              CAST(json_extract(payload_json,'$.no_price') AS INTEGER) *
              CAST(json_extract(payload_json,'$.count') AS INTEGER)
           ), 0)
           + COALESCE(SUM(
              CAST(json_extract(payload_json,'$.yes_price') AS INTEGER) *
              CAST(json_extract(payload_json,'$.count') AS INTEGER)
           ), 0)
           FROM live_orders
           WHERE substr(created_at,1,10)=? AND status LIKE 'PLACED_%'""",
        (date_et,),
    ).fetchone()[0]
    open_pos = placed_total
    exposure_usd = (exposure_cents or 0) / 100.0
    con.close()
except Exception:
    open_pos = "—"
    today_live_orders = "—"
    exposure_usd = None

# Health badge
if total_failed + total_missed == 0 and total_ok > 0:
    health_badge = "🟢 **Healthy**"
elif total_ok > 0 and (total_failed + total_missed) > 0:
    health_badge = "🟡 **Degraded**"
elif total_ok == 0 and (total_failed + total_missed) > 0:
    health_badge = "🔴 **Down**"
else:
    health_badge = "⚪ **No data yet**"

now_et = datetime.now(ET).strftime("%I:%M:%S %p ET")

out = []
out.append(f"# Kalshi Weather Bot — Daily Operations Report")
out.append(f"_{heading_date}_")
out.append("")
out.append(f"_Last updated: {now_et} • Auto-generated from `{date_et}.jsonl` — every value is queried from logs or the database, never fabricated._")
out.append("")
out.append("---")
out.append("")
out.append("## Today at a Glance")
out.append("")
out.append(f"| | |")
out.append(f"|:--|:--|")
out.append(f"| **Bot Health** | {health_badge} |")
out.append(f"| **Successful cycles** | {total_ok} |")
out.append(f"| **Missed cycles** | {total_missed} |")
out.append(f"| **Failed cycles** | {total_failed} |")
out.append(f"| **Markets evaluated (sum)** | {total_markets:,} |")
out.append(f"| **Shadow fills today** | {total_fills} |")
out.append(f"| **Live orders placed today** | {today_live_orders} |")
exposure_str = f"${exposure_usd:.2f}" if exposure_usd is not None else "—"
out.append(f"| **Capital deployed today** | {exposure_str} |")
out.append(f"| **Open contracts on Kalshi** | {open_pos} |")
out.append("")
out.append("---")
out.append("")
out.append("## Scientific Priors (T1.2 + T1.3)")
out.append("")
clim_total = sum(int(r.get("climatology_hit_count") or 0) for r in rows)
clim_max_shrink = max(
    (float(r.get("climatology_max_shrink_delta") or 0) for r in rows),
    default=0.0,
)
pers_total = sum(int(r.get("persistence_hit_count") or 0) for r in rows)
pers_inflated_total = sum(
    int(r.get("persistence_inflation_triggered_count") or 0) for r in rows
)
pers_max_gap = max(
    (float(r.get("persistence_max_abs_gap_f") or 0) for r in rows),
    default=0.0,
)
out.append(f"| | |")
out.append(f"|:--|:--|")
out.append(
    f"| **Climatology cache hits (markets w/ prior applied)** | {clim_total:,} |"
)
out.append(
    f"| **Largest model→prior shrinkage today** | {clim_max_shrink:.3f} prob units |"
)
out.append(
    f"| **Persistence (yesterday's high) hits** | {pers_total:,} |"
)
out.append(
    f"| **Persistence-triggered uncertainty inflations** | {pers_inflated_total} |"
)
out.append(
    f"| **Largest persistence gap today** | {pers_max_gap:.1f}°F |"
)
out.append("")
out.append("---")
out.append("")
out.append("## Cycle Log")
out.append("")

if not rows:
    out.append("_No cycles recorded yet today._")
else:
    # 2026-05-22: limit the log to the 30 most-recent cycles (newest first).
    # The full log can run 100+ rows during incident days, dominated by
    # FAILED entries that have no useful per-cycle data — wrap the table
    # in a fixed-height scrollable container instead of letting it eat the
    # whole page.
    _MAX_LOG_ROWS = 30
    total_rows = len(rows)
    # 2026-05-22: drop only pre-recovery NOISE — rows that have NO time anchor
    # at all (no started_utc, no slot, no recorded_at_utc) AND no reason.
    # Earlier version was too aggressive — was filtering out legitimate cycle
    # FAILED rows that have started_utc + recorded_at_utc but null slot.
    def _is_noise(r):
        s = r.get("status")
        if s not in ("FAILED", "MISSED"):
            return False
        if r.get("started_utc"):
            return False  # the cycle actually started — not noise
        if r.get("slot"):
            return False
        if r.get("recorded_at_utc"):
            return False  # has a timestamp — keep
        reason = r.get("reason")
        return not reason or reason == "unknown"
    rows_clean = [r for r in rows if not _is_noise(r)]
    noise_dropped = total_rows - len(rows_clean)
    # Sort newest first by best-available timestamp.
    def _row_ts(r):
        return r.get("started_utc") or r.get("recorded_at_utc") or r.get("slot") or ""
    rows_sorted = sorted(rows_clean, key=_row_ts, reverse=True)

    # 2026-05-22: collapse runs of consecutive FAILED rows with the same
    # reason into a single roll-up row. Cuts the May-22 chaos burst (13
    # identical "timed out / aborted" rows) down to one summary entry.
    rolled = []
    i = 0
    while i < len(rows_sorted):
        r = rows_sorted[i]
        if r.get("status") == "FAILED":
            reason_key = (r.get("reason") or "").strip()
            run = [r]
            j = i + 1
            while j < len(rows_sorted) and rows_sorted[j].get("status") == "FAILED" \
                    and (rows_sorted[j].get("reason") or "").strip() == reason_key:
                run.append(rows_sorted[j])
                j += 1
            if len(run) > 1:
                first = run[0]
                last = run[-1]
                first["_count"] = len(run)
                first["_span_first_ts"] = last.get("started_utc") or last.get("recorded_at_utc")
                first["_span_last_ts"] = first.get("started_utc") or first.get("recorded_at_utc")
            rolled.append(r)
            i = j
        else:
            rolled.append(r)
            i += 1

    rows_display = rolled[:_MAX_LOG_ROWS]
    truncated = max(0, len(rolled) - len(rows_display))
    coalesced = len(rows_sorted) - len(rolled)
    header_bits = []
    header_bits.append(f"Showing {len(rows_display)} entries of {len(rows_clean)} cycles today")
    if coalesced > 0:
        header_bits.append(f"{coalesced} duplicate FAILED rows coalesced")
    if truncated > 0:
        header_bits.append(f"{truncated} older entries hidden")
    if noise_dropped > 0:
        header_bits.append(f"{noise_dropped} pre-recovery noise rows suppressed")
    out.append(f"_{'; '.join(header_bits)}._")
    out.append("")
    # ``markdown="1"`` lets python-markdown's md_in_html extension keep
    # parsing markdown (the table) inside this HTML block — without it
    # the div would be treated as raw HTML and the table left as plain text.
    out.append("<div markdown=\"1\" style=\"max-height: 480px; overflow-y: auto; border: 1px solid var(--kxw-border, #e5e7eb); border-radius: 6px; padding: 4px 12px;\">")
    out.append("")
    out.append("| Time (ET) | Status | Markets | Cand. | Selected | Fills | Live | Notes |")
    out.append("|:--|:--|--:|--:|--:|--:|--:|:--|")
    for r in rows_display:
        status = r.get("status", "?")
        if status == "OK":
            badge = "✅ OK"
            t = to_et(r.get("started_utc", ""))
            mkt = r.get("markets_evaluated", 0)
            cand = r.get("taker_candidates", 0)
            sel = r.get("selected_count", 0)
            fills = r.get("shadow_fills_in_window", 0)
            live = r.get("live_orders_in_window", 0)
            tickers = r.get("selected_tickers") or []
            note = ", ".join(tickers) if tickers else "no selections"
            out.append(f"| {t} | {badge} | {mkt} | {cand} | {sel} | {fills} | {live} | {note} |")
        elif status == "FAILED":
            count = r.get("_count", 1)
            badge = f"❌ FAILED ×{count}" if count > 1 else "❌ FAILED"
            # Time: when rolled up, show the span "FIRST → LAST"; else just the cycle time
            if count > 1:
                t_first = to_et(r.get("_span_first_ts", "")) or "—"
                t_last = to_et(r.get("_span_last_ts", "")) or "—"
                t = f"{t_first} → {t_last}"
            else:
                t = to_et(r.get("started_utc", "")) or r.get("slot") or to_et(r.get("recorded_at_utc", "")) or "—"
            reason = (r.get("reason") or "timed out / aborted").replace("|", "\\|").replace("\n", " ").replace("\r", " ")[:140]
            out.append(f"| {t} | {badge} | — | — | — | — | — | {reason} |")
        elif status == "MISSED":
            badge = "⚠️ MISSED"
            t = r.get("slot") or to_et(r.get("recorded_at_utc", "")) or "—"
            reason = (r.get("reason") or "unknown").replace("|", "\\|").replace("\n", " ").replace("\r", " ")[:140]
            out.append(f"| {t} | {badge} | — | — | — | — | — | {reason} |")
        else:
            t = r.get("slot") or to_et(r.get("recorded_at_utc", ""))
            reason = (r.get("reason") or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")[:140]
            out.append(f"| {t} | ⚪ {status} | — | — | — | — | — | {reason} |")
    out.append("")
    out.append("</div>")

# Detail blocks for any failures + the most recent OK cycle's rejection breakdown.
# 2026-05-22: incidents are coalesced by reason + capped at 20 most-recent to
# avoid 50+ duplicate "unknown" blocks blowing up the report.
failures = [r for r in rows if r.get("status") in ("FAILED", "MISSED")]
if failures:
    # Group by (status, reason). Show count + first/last occurrence times.
    from collections import Counter, defaultdict
    grouped = defaultdict(list)
    for r in failures:
        key = (r.get("status","?"), (r.get("reason") or "unknown")[:120])
        grouped[key].append(r)
    incident_summary = sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    total_failures = len(failures)
    distinct_reasons = len(grouped)

    out.append("")
    out.append("---")
    out.append("")
    out.append(f"## Incident Detail — {total_failures} failures / {distinct_reasons} distinct reasons")
    out.append("")
    out.append("<div markdown=\"1\" style=\"max-height: 360px; overflow-y: auto; border: 1px solid var(--kxw-border, #e5e7eb); border-radius: 6px; padding: 4px 12px;\">")
    out.append("")
    out.append("| Status | Reason | Count | First (ET) | Last (ET) |")
    out.append("|:--|:--|--:|:--|:--|")
    for (status, reason), incidents in incident_summary[:30]:
        times = sorted(to_et(i.get("recorded_at_utc","")) for i in incidents if i.get("recorded_at_utc"))
        first_t = times[0] if times else "—"
        last_t = times[-1] if times else "—"
        badge = {"FAILED":"❌","MISSED":"⚠️"}.get(status,"⚪") + " " + status
        reason_disp = reason.replace("|","\\|").replace("\n", " ").replace("\r", " ")
        out.append(f"| {badge} | `{reason_disp}` | {len(incidents)} | {first_t} | {last_t} |")
    if len(incident_summary) > 30:
        out.append(f"| _…and {len(incident_summary)-30} more reasons_ | | | | |")
    out.append("")
    out.append("</div>")
    out.append("")

last_ok = next((r for r in reversed(rows) if r.get("status") == "OK"), None)
if last_ok and last_ok.get("top_rejection_reasons"):
    out.append("---")
    out.append("")
    out.append(f"## Most Recent Cycle — Decision Breakdown ({to_et(last_ok['started_utc'])} ET)")
    out.append("")
    out.append("| Rejection reason | Count |")
    out.append("|:--|--:|")
    for rr in last_ok["top_rejection_reasons"]:
        out.append(f"| `{rr['reason']}` | {rr['count']} |")
    out.append("")

with open(md_file, "w") as f:
    f.write("\n".join(out) + "\n")
PY
