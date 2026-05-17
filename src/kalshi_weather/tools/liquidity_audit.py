"""Liquidity audit — analyze Kalshi orderbook depth per market over time.

Reads `orderbook_snapshots` and computes per-market liquidity profiles so we
can identify which cities/markets have actual depth worth trading vs which
are dead books.

Examples:
  # Latest snapshot per market — quick "what's tradeable right now"
  python -m kalshi_weather.tools.liquidity_audit

  # By city — which cities have the most depth on average
  python -m kalshi_weather.tools.liquidity_audit --by-city

  # By hour-of-day across all snapshots
  python -m kalshi_weather.tools.liquidity_audit --by-hour

  # Drill into one market's liquidity over time
  python -m kalshi_weather.tools.liquidity_audit --market KXHIGHTPHX-26MAY16-T97
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _open() -> sqlite3.Connection:
    return sqlite3.connect("data/state/runtime.sqlite3")


def _city_from_ticker(ticker: str) -> str:
    """Extract city code from Kalshi weather ticker."""
    # KXHIGHTPHX-26MAY16-T97 → phx
    # KXHIGHNY-26MAY16-T76 → ny
    if not ticker.startswith("KX"):
        return "?"
    base = ticker.split("-")[0]
    # Remove KXHIGH or KXLOW prefix
    for prefix in ("KXHIGHT", "KXHIGH", "KXLOWT", "KXLOW"):
        if base.startswith(prefix):
            return base[len(prefix):].lower()
    return base[4:].lower()


def _ladder_stats(ladder: list[list]) -> tuple[int, float, float]:
    """Return (level_count, total_shares, total_dollars) for a price-size ladder."""
    if not ladder:
        return (0, 0.0, 0.0)
    levels = 0
    total_shares = 0.0
    total_dollars = 0.0
    for entry in ladder:
        try:
            price = float(entry[0])
            size = float(entry[1])
        except (IndexError, ValueError, TypeError):
            continue
        if size > 0:
            levels += 1
            total_shares += size
            total_dollars += price * size
    return levels, total_shares, total_dollars


def _summarize_snapshot(payload: dict) -> dict:
    """Compute per-side liquidity stats from a single orderbook snapshot."""
    yb_lvl, yb_shr, yb_usd = _ladder_stats(payload.get("yes_bids_ladder") or [])
    nb_lvl, nb_shr, nb_usd = _ladder_stats(payload.get("no_bids_ladder") or [])
    ya_lvl, ya_shr, ya_usd = _ladder_stats(payload.get("implied_yes_asks_ladder") or [])
    na_lvl, na_shr, na_usd = _ladder_stats(payload.get("implied_no_asks_ladder") or [])

    # Best implied ask = lowest price in implied_yes_asks_ladder, etc.
    def _best(ladder):
        if not ladder:
            return None
        try:
            return min(float(e[0]) for e in ladder if float(e[1]) > 0)
        except Exception:
            return None

    best_yes_ask = _best(payload.get("implied_yes_asks_ladder"))
    best_no_ask = _best(payload.get("implied_no_asks_ladder"))

    return {
        "yes_bid_levels": yb_lvl,
        "yes_bid_shares": yb_shr,
        "yes_bid_dollars": yb_usd,
        "no_bid_levels": nb_lvl,
        "no_bid_shares": nb_shr,
        "no_bid_dollars": nb_usd,
        "yes_ask_levels": ya_lvl,
        "yes_ask_shares": ya_shr,
        "no_ask_levels": na_lvl,
        "no_ask_shares": na_shr,
        "best_yes_ask": best_yes_ask,
        "best_no_ask": best_no_ask,
        "two_sided": best_yes_ask is not None and best_no_ask is not None,
        "one_sided_side": (
            "yes_only" if best_yes_ask is not None and best_no_ask is None
            else "no_only" if best_no_ask is not None and best_yes_ask is None
            else "both" if best_yes_ask is not None and best_no_ask is not None
            else "empty"
        ),
    }


def latest_snapshot_per_market(hours: int = 24) -> dict[str, dict]:
    """Return the most-recent snapshot per market_ticker within the window."""
    conn = _open()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT market_ticker, as_of_time, payload_json FROM orderbook_snapshots "
        "WHERE as_of_time >= ? ORDER BY as_of_time DESC",
        (cutoff,),
    ).fetchall()
    seen: dict[str, dict] = {}
    for ticker, as_of, raw in rows:
        if ticker in seen:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        stats = _summarize_snapshot(payload)
        stats["market_ticker"] = ticker
        stats["as_of_time"] = as_of
        stats["city"] = _city_from_ticker(ticker)
        seen[ticker] = stats
    return seen


def report_latest(hours: int = 24) -> None:
    """Show liquidity for every market in the recent window."""
    snaps = latest_snapshot_per_market(hours=hours)
    if not snaps:
        print(f"\n  No snapshots in last {hours}h.")
        return
    print(f"\n=== Latest orderbook per market (last {hours}h, {len(snaps)} markets) ===")
    print(f"  {'MARKET':<32} {'CITY':<5} {'SIDE':<9} {'YES_ASK':>7} {'NO_ASK':>7} "
          f"{'YES_LVLS':>8} {'YES_SHRS':>9} {'NO_LVLS':>7} {'NO_SHRS':>9}")
    # Sort by 2-sided first, then by total depth descending
    items = list(snaps.values())
    items.sort(key=lambda s: (
        0 if s["two_sided"] else 1,
        -(s["yes_ask_shares"] + s["no_ask_shares"]),
    ))
    for s in items:
        ya = f"${s['best_yes_ask']:.2f}" if s["best_yes_ask"] is not None else "—"
        na = f"${s['best_no_ask']:.2f}" if s["best_no_ask"] is not None else "—"
        side = s["one_sided_side"]
        marker = "✓✓" if s["two_sided"] else ("✓" if side != "empty" else "✗")
        print(f"  {s['market_ticker']:<32} {s['city']:<5} {marker}{side:<7} "
              f"{ya:>7} {na:>7} {s['yes_ask_levels']:>8} {s['yes_ask_shares']:>9.0f} "
              f"{s['no_ask_levels']:>7} {s['no_ask_shares']:>9.0f}")


def report_by_city(hours: int = 24) -> None:
    """Aggregate liquidity by city."""
    snaps = latest_snapshot_per_market(hours=hours)
    by_city: dict[str, list[dict]] = defaultdict(list)
    for s in snaps.values():
        by_city[s["city"]].append(s)
    print(f"\n=== Liquidity ranked by city (last {hours}h) ===")
    print(f"  {'CITY':<6} {'MKTS':>4} {'2-SIDED':>7} {'1-SIDED':>7} {'AVG_YES_SHRS':>12} {'AVG_NO_SHRS':>11} {'AVG_LVLS':>8}")
    city_rows = []
    for city, snapshots in by_city.items():
        n = len(snapshots)
        two_sided = sum(1 for s in snapshots if s["two_sided"])
        one_sided = sum(1 for s in snapshots if not s["two_sided"] and s["one_sided_side"] != "empty")
        avg_yes_shrs = sum(s["yes_ask_shares"] for s in snapshots) / n
        avg_no_shrs = sum(s["no_ask_shares"] for s in snapshots) / n
        avg_lvls = sum(s["yes_ask_levels"] + s["no_ask_levels"] for s in snapshots) / n
        city_rows.append((city, n, two_sided, one_sided, avg_yes_shrs, avg_no_shrs, avg_lvls))
    # Sort by 2-sided count descending, then total depth
    city_rows.sort(key=lambda x: (-x[2], -(x[4] + x[5])))
    for city, n, two, one, ay, an, al in city_rows:
        print(f"  {city:<6} {n:>4} {two:>7} {one:>7} {ay:>12.0f} {an:>11.0f} {al:>8.1f}")


def report_by_hour(hours: int = 24) -> None:
    """Show how liquidity varies by hour-of-day UTC."""
    conn = _open()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT as_of_time, payload_json FROM orderbook_snapshots WHERE as_of_time >= ?",
        (cutoff,),
    ).fetchall()
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for as_of, raw in rows:
        try:
            dt = datetime.fromisoformat(as_of)
            stats = _summarize_snapshot(json.loads(raw))
            by_hour[dt.hour].append(stats)
        except Exception:
            continue
    print(f"\n=== Liquidity by UTC hour-of-day (last {hours}h) ===")
    print(f"  {'HOUR':<4} {'N_SNAPS':>7} {'%TWO-SIDED':>10} {'AVG_DEPTH':>9}")
    for hour in sorted(by_hour):
        snaps = by_hour[hour]
        n = len(snaps)
        two = sum(1 for s in snaps if s["two_sided"])
        avg_d = sum(s["yes_ask_shares"] + s["no_ask_shares"] for s in snaps) / n if n else 0
        pct = (two / n * 100) if n else 0
        bar = "█" * min(int(pct / 2), 40)
        print(f"  {hour:>2}h  {n:>7} {pct:>9.1f}% {avg_d:>9.0f}  {bar}")


def market_evolution(market_ticker: str) -> None:
    """Show how one market's liquidity evolved over time."""
    conn = _open()
    rows = conn.execute(
        "SELECT as_of_time, payload_json FROM orderbook_snapshots "
        "WHERE market_ticker = ? ORDER BY as_of_time",
        (market_ticker,),
    ).fetchall()
    if not rows:
        print(f"\n  No snapshots for {market_ticker}.")
        return
    print(f"\n=== Liquidity evolution for {market_ticker} ({len(rows)} snapshots) ===")
    print(f"  {'TIME (UTC)':<19}  {'YES_ASK':>7} {'NO_ASK':>7} {'YES_LVLS':>8} {'NO_LVLS':>7} {'YES_SHRS':>9} {'NO_SHRS':>9}  SIDED")
    for as_of, raw in rows:
        try:
            s = _summarize_snapshot(json.loads(raw))
        except Exception:
            continue
        ya = f"${s['best_yes_ask']:.2f}" if s["best_yes_ask"] is not None else "—"
        na = f"${s['best_no_ask']:.2f}" if s["best_no_ask"] is not None else "—"
        marker = "✓✓" if s["two_sided"] else "—"
        when = str(as_of)[:19].replace("T", " ")
        print(f"  {when:<19}  {ya:>7} {na:>7} {s['yes_ask_levels']:>8} {s['no_ask_levels']:>7} "
              f"{s['yes_ask_shares']:>9.0f} {s['no_ask_shares']:>9.0f}  {marker}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Kalshi orderbook liquidity.")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window (default 24h).")
    parser.add_argument("--by-city", action="store_true", help="Aggregate by city.")
    parser.add_argument("--by-hour", action="store_true", help="Aggregate by UTC hour-of-day.")
    parser.add_argument("--market", help="Show one market's liquidity over time.")
    args = parser.parse_args()

    if args.market:
        market_evolution(args.market)
    elif args.by_city:
        report_by_city(hours=args.hours)
    elif args.by_hour:
        report_by_hour(hours=args.hours)
    else:
        report_latest(hours=args.hours)


if __name__ == "__main__":
    main()
