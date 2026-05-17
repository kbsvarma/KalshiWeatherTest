"""Fetch daily NWS Climate Reports (CLI) for all registered cities and persist
actual daily high/low temperatures to the market_settlements table.

Run nightly (or after settlement close-time). Used for:
  - Joining back to decisions for counterfactual P&L
  - Empirical bias correction of forecast providers
  - Live-gate `settlement_validation_ready` accumulation
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

from kalshi_weather.clients import NwsClimateClient
from kalshi_weather.clients.nws import NwsClimateClientError
from kalshi_weather.settlement.cli_parser import (
    SettlementCliParseError,
    parse_cli_climate_report,
)
from kalshi_weather.storage import FileReferenceRegistry, SQLiteStateStore


def fetch_one(
    *,
    nws: NwsClimateClient,
    store: SQLiteStateStore,
    city_id: str,
    station,
    target_date: date,
) -> tuple[bool, str]:
    """Fetch CLI report for one city/date pair. Returns (success, note)."""
    site = station.wfo_office
    issuedby = station.climate_product_id[3:] if station.climate_product_id.startswith("CLI") else station.climate_product_id
    try:
        text = nws.fetch_cli_text(site=site, issuedby=issuedby)
    except NwsClimateClientError as exc:
        return False, f"fetch_failed: {exc}"
    except Exception as exc:
        return False, f"fetch_error: {exc}"

    if not text or not text.strip():
        return False, "empty_cli_response"

    payload_id = sha256(text.encode("utf-8")).hexdigest()
    try:
        snapshot = parse_cli_climate_report(text, station, raw_text_payload_id=payload_id)
    except SettlementCliParseError as exc:
        return False, f"parse_failed: {exc}"
    except Exception as exc:
        return False, f"parse_error: {exc}"

    # CLI reports are typically issued for *yesterday*. Verify the date matches
    # the target by reading the local-standard window's date.
    snapshot_date = snapshot.local_standard_window_start.date()
    if snapshot_date != target_date:
        return False, f"report_for_different_date ({snapshot_date} != {target_date})"

    high_f = float(snapshot.max_temp_f) if snapshot.max_temp_f is not None else None
    low_f = float(snapshot.min_temp_f) if snapshot.min_temp_f is not None else None

    if high_f is None and low_f is None:
        return False, "no_temp_fields_in_report"

    store.save_market_settlement(
        city_id=city_id,
        station_id=station.station_id,
        local_date=target_date.isoformat(),
        daily_high_f=high_f,
        daily_low_f=low_f,
        source="NWS_CLI",
        source_payload_id=payload_id,
        extra={
            "issue_time": snapshot.issue_time.isoformat() if snapshot.issue_time else None,
            "climate_product_id": snapshot.climate_product_id,
        },
    )
    return True, f"saved high={high_f} low={low_f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch NWS CLI settlements for all cities.")
    parser.add_argument(
        "--date",
        help="Target local date (YYYY-MM-DD). Defaults to yesterday in UTC.",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=0,
        help="Also fetch this many extra days going backward (for backfill).",
    )
    parser.add_argument(
        "--city-id",
        action="append",
        help="Limit to specific city_id(s). Repeatable.",
    )
    args = parser.parse_args()

    if args.date:
        anchor = date.fromisoformat(args.date)
    else:
        anchor = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    target_dates = [anchor - timedelta(days=i) for i in range(args.days_back + 1)]

    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    nws = NwsClimateClient()

    # Use city_profile → station mapping; only fetch once per (city, date) pair
    # even if a city has multiple series in the registry.
    by_city = {}
    for ctx in registry.iter_city_contexts(seed):
        by_city.setdefault(ctx.city_profile.city_id, ctx.station)
    if args.city_id:
        wanted = {c.lower() for c in args.city_id}
        by_city = {k: v for k, v in by_city.items() if k.lower() in wanted}

    print(f"Fetching settlements for {len(by_city)} cities × {len(target_dates)} dates")
    saved = 0
    skipped = 0
    for target_date in target_dates:
        for city_id, station in by_city.items():
            existing = store.get_market_settlement(city_id, target_date.isoformat())
            if existing and existing.get("daily_high_f") is not None:
                skipped += 1
                continue
            ok, note = fetch_one(
                nws=nws,
                store=store,
                city_id=city_id,
                station=station,
                target_date=target_date,
            )
            marker = "✓" if ok else "✗"
            print(f"  {marker} {target_date}  {city_id:5}  {note}")
            if ok:
                saved += 1

    print(f"\nDone. Saved: {saved}, Skipped (already-present): {skipped}")


if __name__ == "__main__":
    main()
