"""Signal-audit CLI — analyze accumulated signal data for patterns.

Queries the market_recommendations table for time-series analysis of every
signal the bot has ever generated, regardless of whether it was filled.

Examples:
  # Top-line view
  python -m kalshi_weather.tools.signal_audit

  # Show only signals from the last 24 hours
  python -m kalshi_weather.tools.signal_audit --hours 24

  # Drill into a specific city
  python -m kalshi_weather.tools.signal_audit --city phx

  # Track signal evolution for one market over time
  python -m kalshi_weather.tools.signal_audit --market KXHIGHTPHX-26MAY16-T97

  # Group resolved signals by hour-of-day to see which times of day are best
  python -m kalshi_weather.tools.signal_audit --by-hour
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any


def _open_conn() -> sqlite3.Connection:
    return sqlite3.connect("data/state/runtime.sqlite3")


def _since_clause(hours: int | None) -> tuple[str, tuple[Any, ...]]:
    if hours is None:
        return "", ()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return " WHERE as_of_time >= ?", (cutoff,)


def top_summary(hours: int | None = None) -> None:
    """Overall signal counts by kind + window status + fill status."""
    where, params = _since_clause(hours)
    conn = _open_conn()
    suffix = f" (last {hours}h)" if hours else " (all time)"
    print(f"\n=== Signal counts by recommendation kind{suffix} ===")
    for r in conn.execute(
        f"SELECT recommendation_kind, count(*) FROM market_recommendations{where} "
        f"GROUP BY recommendation_kind ORDER BY count(*) DESC", params
    ):
        print(f"  {r[0]:<20} {r[1]:>5}")

    print(f"\n=== By window status{suffix} ===")
    for r in conn.execute(
        f"SELECT COALESCE(window_status,'unknown'), count(*) FROM market_recommendations{where} "
        f"GROUP BY window_status ORDER BY count(*) DESC", params
    ):
        print(f"  {r[0]:<20} {r[1]:>5}")

    print(f"\n=== Fill outcomes for BET/REVIEW signals{suffix} ===")
    fill_where = where + (" AND" if where else " WHERE") + " recommendation_kind IN ('BET','REVIEW')"
    rows = list(conn.execute(
        f"SELECT actually_filled, COALESCE(fill_blocker_reason,'(filled)'), count(*) "
        f"FROM market_recommendations{fill_where} "
        f"GROUP BY actually_filled, fill_blocker_reason ORDER BY count(*) DESC", params
    ))
    if not rows:
        print("  (no BET/REVIEW signals yet)")
    for filled, reason, n in rows:
        marker = "✓" if filled else "✗"
        print(f"  {marker} {reason:<30} {n:>5}")

    print(f"\n=== Counterfactual P&L by kind (resolved only){suffix} ===")
    rows = list(conn.execute(
        f"SELECT recommendation_kind, count(*), "
        f"  SUM(CASE WHEN counterfactual_won=1 THEN 1 ELSE 0 END), "
        f"  ROUND(SUM(counterfactual_pnl_usd), 3) "
        f"FROM market_recommendations{where}"
        + (" AND" if where else " WHERE") + " counterfactual_resolved=1 "
        f"GROUP BY recommendation_kind ORDER BY recommendation_kind", params
    ))
    if not rows:
        print("  (no resolved counterfactuals yet — waiting on settlements)")
    for kind, n, wins, pnl in rows:
        wr = (wins / n * 100) if n else 0
        sign = "+" if (pnl or 0) >= 0 else ""
        print(f"  {kind:<18}  {n:>3} bets  {wr:5.1f}% wins  {sign}${pnl or 0:.3f}")


def by_hour(hours: int | None = None) -> None:
    """Show signal frequency by local hour-of-day."""
    where, params = _since_clause(hours)
    conn = _open_conn()
    suffix = f" (last {hours}h)" if hours else " (all time)"
    print(f"\n=== Signals by local hour-of-day{suffix} ===")
    print(f"  {'HOUR':<4}  {'TOTAL':>5} {'BET':>4} {'REVIEW':>6} {'WATCH':>5} {'SKIP':>4}")
    by_hr: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in conn.execute(
        f"SELECT local_time_of_day_hour, recommendation_kind, count(*) "
        f"FROM market_recommendations{where} GROUP BY local_time_of_day_hour, recommendation_kind",
        params,
    ):
        if r[0] is None:
            continue
        by_hr[int(r[0])][str(r[1])] += int(r[2])
    for hour in sorted(by_hr):
        bucket = by_hr[hour]
        total = sum(bucket.values())
        bet = bucket.get("BET", 0)
        rev = bucket.get("REVIEW", 0)
        watch = bucket.get("WATCH_RECOMMEND", 0)
        skip = bucket.get("SKIP", 0)
        bar = "█" * min(int(total / 2), 40)
        print(f"  {hour:>4}h  {total:>5} {bet:>4} {rev:>6} {watch:>5} {skip:>4}  {bar}")


def by_window_status(hours: int | None = None) -> None:
    """For each window_status, show how recommendations distribute."""
    where, params = _since_clause(hours)
    conn = _open_conn()
    suffix = f" (last {hours}h)" if hours else " (all time)"
    print(f"\n=== Signals by window_status × kind{suffix} ===")
    rows = list(conn.execute(
        f"SELECT COALESCE(window_status,'unknown'), recommendation_kind, count(*), "
        f"  AVG(model_p_yes), AVG(market_price), AVG(provider_spread_f), "
        f"  AVG(minutes_to_settlement_close) "
        f"FROM market_recommendations{where} "
        f"GROUP BY window_status, recommendation_kind ORDER BY window_status, recommendation_kind",
        params,
    ))
    print(f"  {'WINDOW':<12} {'KIND':<18} {'N':>4} {'avg_p_M':>7} {'avg_p_K':>7} {'avg_sprd':>8} {'avg_mn_to_close':>15}")
    for status, kind, n, pm, pk, sp, mtc in rows:
        pm_s = f"{pm:.2f}" if pm is not None else "-"
        pk_s = f"{pk:.2f}" if pk is not None else "-"
        sp_s = f"{sp:.1f}" if sp is not None else "-"
        mtc_s = f"{mtc:.0f}" if mtc is not None else "-"
        print(f"  {status:<12} {kind:<18} {n:>4} {pm_s:>7} {pk_s:>7} {sp_s:>8} {mtc_s:>15}")


def market_evolution(market_ticker: str) -> None:
    """Show all signals for one market over time — see how the model evolved."""
    conn = _open_conn()
    rows = list(conn.execute(
        "SELECT as_of_time, window_status, minutes_to_settlement_close, "
        "  recommendation_kind, model_p_yes, market_price, current_temp_f, "
        "  high_so_far_f, provider_spread_f, actually_filled, fill_blocker_reason "
        "FROM market_recommendations WHERE market_ticker = ? "
        "ORDER BY as_of_time",
        (market_ticker,),
    ))
    if not rows:
        print(f"\n  No signals found for {market_ticker}.")
        return
    print(f"\n=== Signal evolution for {market_ticker} ({len(rows)} signals) ===")
    print(f"  {'TIME (UTC)':<19}  {'WIN':<10} {'MIN-CLOSE':>9} {'KIND':<18} "
          f"{'p_M':>5} {'p_K':>5} {'TEMP':>5} {'HIGH':>5} {'SPRD':>5}  FILL")
    for r in rows:
        as_of = str(r[0])[:19].replace("T", " ")
        win = (r[1] or "?")[:10]
        mtc = r[2]
        kind = (r[3] or "")[:18]
        pm = f"{r[4]:.2f}" if r[4] is not None else "-"
        pk = f"{r[5]:.2f}" if r[5] is not None else "-"
        tmp = f"{r[6]:.0f}" if r[6] is not None else "-"
        hi = f"{r[7]:.0f}" if r[7] is not None else "-"
        sp = f"{r[8]:.1f}" if r[8] is not None else "-"
        fill = "✓ filled" if r[9] else (f"✗ {r[10]}" if r[10] else "skipped")
        mtc_s = f"{int(mtc):>5}" if mtc is not None else "    -"
        print(f"  {as_of:<19}  {win:<10} {mtc_s:>9} {kind:<18} {pm:>5} {pk:>5} {tmp:>5} {hi:>5} {sp:>5}  {fill}")


def recent_signals_for_city(city_id: str, hours: int = 24) -> None:
    """Show all recent signals for one city."""
    conn = _open_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = list(conn.execute(
        "SELECT as_of_time, market_ticker, recommendation_kind, recommended_side, "
        "  model_p_yes, market_price, provider_spread_f, actually_filled, fill_blocker_reason "
        "FROM market_recommendations WHERE city_id = ? AND as_of_time >= ? "
        "ORDER BY as_of_time DESC",
        (city_id, cutoff),
    ))
    if not rows:
        print(f"\n  No signals in last {hours}h for city '{city_id}'.")
        return
    print(f"\n=== Last {hours}h of signals for {city_id} ({len(rows)} rows) ===")
    print(f"  {'TIME (UTC)':<19}  {'TICKER':<28} {'KIND':<18} {'SIDE':<4} "
          f"{'p_M':>5} {'p_K':>5} {'SPRD':>5}  FILL")
    for r in rows:
        as_of = str(r[0])[:19].replace("T", " ")
        ticker = (r[1] or "")[:28]
        kind = (r[2] or "")[:18]
        side = (r[3] or "-")[:4]
        pm = f"{r[4]:.2f}" if r[4] is not None else "-"
        pk = f"{r[5]:.2f}" if r[5] is not None else "-"
        sp = f"{r[6]:.1f}" if r[6] is not None else "-"
        fill = "✓ filled" if r[7] else (f"✗ {r[8]}" if r[8] else "skipped")
        print(f"  {as_of:<19}  {ticker:<28} {kind:<18} {side:<4} {pm:>5} {pk:>5} {sp:>5}  {fill}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit accumulated signal data.")
    parser.add_argument("--hours", type=int, help="Restrict to last N hours.")
    parser.add_argument("--city", help="Show last 24h of signals for one city.")
    parser.add_argument("--market", help="Show signal evolution for one market.")
    parser.add_argument("--by-hour", action="store_true", help="Histogram by local hour-of-day.")
    parser.add_argument("--by-window", action="store_true", help="Stats by window_status × kind.")
    args = parser.parse_args()

    if args.market:
        market_evolution(args.market)
        return
    if args.city:
        recent_signals_for_city(args.city, hours=args.hours or 24)
        return
    if args.by_hour:
        by_hour(hours=args.hours)
        return
    if args.by_window:
        by_window_status(hours=args.hours)
        return
    # Default — top summary
    top_summary(hours=args.hours)


if __name__ == "__main__":
    main()
