"""Validation harness — replay settled days, score p_yes against reality.

For every settled (city, local_date) in market_settlements:
  1. Reconstruct the forecast set as it existed at a fixed decision time
     (default 10:00 local — morning, before the heating peak, the window
     where most live entries happen).
  2. Build the forecast distribution through the REAL pipeline
     (build_forecast_distribution, including provider bias adjustments
     from the freshly built calibration report).
  3. Compute p_yes for synthetic thresholds at actual_high + offsets
     (-4 … +4 °F) under the ">=" operator (greater-style markets).
  4. Score: Brier score + per-bucket calibration curve
     (predicted p vs empirical hit rate).

A perfectly calibrated model shows bucket hit-rates ≈ bucket means and a
low Brier score. Systematic cool bias shows up as hit-rate >> predicted
in the warm-threshold buckets.

Usage:
  PYTHONPATH=src python3 -m kalshi_weather.tools.validate_calibration [--hour 10]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from kalshi_weather.analytics import (
    build_provider_reliability_report,
    extract_provider_bias_adjustments,
    extract_provider_day_max_sigmas,
    extract_provider_reliability_weights,
)
from kalshi_weather.engines.forecast import build_forecast_distribution
from kalshi_weather.settlement.rule_parser import SettlementRule
from kalshi_weather.storage import FileReferenceRegistry, SQLiteStateStore

THRESHOLD_OFFSETS = (-4, -3, -2, -1, 0, 1, 2, 3, 4)


def _p_yes_at(distribution, threshold: Decimal) -> Decimal:
    """P(high >= threshold) from the distribution PMF."""
    total = Decimal("0")
    for temp, prob in zip(distribution.support_temps_f, distribution.pmf, strict=False):
        if Decimal(temp) >= threshold:
            total += prob
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hour", type=int, default=10,
                        help="local decision hour (default 10 AM)")
    parser.add_argument("--no-bias", action="store_true",
                        help="disable provider bias adjustments (baseline)")
    args = parser.parse_args()

    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    contexts = {c.city_profile.city_id: c for c in registry.iter_city_contexts(seed)}

    # Settled ground truth
    with store._connect() as conn:
        settled = [
            (r[0], r[1], float(r[2]))
            for r in conn.execute(
                "SELECT city_id, local_date, daily_high_f FROM market_settlements "
                "WHERE daily_high_f IS NOT NULL ORDER BY local_date"
            )
        ]
    print(f"settled city-days: {len(settled)}", file=sys.stderr)

    # Per-city calibration report (the real pipeline input), built once.
    calibration_by_city: dict[str, dict] = {}

    samples = []  # (p_yes float, outcome 0/1, offset)
    n_days_scored = 0
    for city_id, local_date, actual_high in settled:
        ctx = contexts.get(city_id)
        if ctx is None:
            continue
        station = ctx.station
        tz = ZoneInfo(station.timezone)
        decision_time = datetime.fromisoformat(f"{local_date}T{args.hour:02d}:00:00").replace(tzinfo=tz)

        # Forecasts available at decision time (latest run per provider before it)
        all_forecasts = store.get_latest_forecasts_as_of(station.station_id, decision_time) \
            if hasattr(store, "get_latest_forecasts_as_of") else None
        if all_forecasts is None:
            # Fallback: filter manually from full history
            raw = store.get_all_forecasts(station.station_id)
            by_provider: dict[str, object] = {}
            for snap in raw:
                if snap.provider_run_time <= decision_time:
                    prev = by_provider.get(snap.provider_id)
                    if prev is None or snap.provider_run_time > prev.provider_run_time:
                        by_provider[snap.provider_id] = snap
            all_forecasts = list(by_provider.values())
        if len(all_forecasts) < 3:
            continue

        if city_id not in calibration_by_city:
            calibration_by_city[city_id] = build_provider_reliability_report(
                forecasts=store.get_all_forecasts(station.station_id),
                observations=store.get_all_observations(station.station_id),
                station=station,
            )
        report = calibration_by_city[city_id]
        lead_hours = 6.0  # 10 AM → ~4 PM peak
        # season_key must be passed to mirror live behavior — decision.py
        # derives it from as_of_time; with None the per-(season, lead-bucket)
        # reports are bypassed and the diluted global stats get used.
        month = decision_time.month
        season_key = ("DJF" if month in (12, 1, 2) else "MAM" if month in (3, 4, 5)
                      else "JJA" if month in (6, 7, 8) else "SON")
        reliability = extract_provider_reliability_weights(report, season_key=season_key, lead_hours=lead_hours)
        bias_adjustments = (
            {} if args.no_bias
            else extract_provider_bias_adjustments(report, season_key=season_key, lead_hours=lead_hours)
        )
        sigma_overrides = extract_provider_day_max_sigmas(report, season_key=season_key, lead_hours=lead_hours)

        for offset in THRESHOLD_OFFSETS:
            threshold = Decimal(str(round(actual_high))) + Decimal(offset)
            window_start = datetime.fromisoformat(f"{local_date}T01:00:00").replace(tzinfo=tz)
            window_end = window_start + timedelta(hours=23, minutes=59)
            rule = SettlementRule(
                settlement_rule_id=f"validate-{city_id}-{local_date}-{offset}",
                market_ticker=f"VALIDATE-{city_id}-{offset}",
                settlement_variable="daily_high_temperature_f",
                operator=">=",
                threshold_f=threshold,
                inclusive_flag=True,
                station_id=station.station_id,
                source_kind="validation",
                source_locator="validate_calibration",
                local_standard_window_start=window_start,
                local_standard_window_end=window_end,
                parser_version="validate_v1",
                validation_status="synthetic",
                ambiguity_flags=(),
                threshold_high_f=None,
            )
            try:
                result = build_forecast_distribution(
                    snapshots=list(all_forecasts),
                    settlement_rule=rule,
                    city_profile=ctx.city_profile,
                    as_of_time=decision_time,
                    provider_reliability=reliability,
                    provider_bias_adjustments=bias_adjustments,
                    provider_sigma_overrides=sigma_overrides,
                )
            except Exception:
                continue
            p = float(_p_yes_at(result.distribution, threshold))
            outcome = 1 if actual_high >= float(threshold) else 0
            samples.append((p, outcome, offset))
        n_days_scored += 1

    print(f"days scored: {n_days_scored}, samples: {len(samples)}", file=sys.stderr)

    # Brier score
    brier = sum((p - o) ** 2 for p, o, _ in samples) / max(1, len(samples))

    # Calibration curve (deciles)
    buckets = defaultdict(list)
    for p, o, _ in samples:
        buckets[min(9, int(p * 10))].append((p, o))
    curve = []
    for b in sorted(buckets):
        rows = buckets[b]
        curve.append({
            "bucket": f"{b/10:.1f}-{(b+1)/10:.1f}",
            "n": len(rows),
            "mean_predicted": round(sum(p for p, _ in rows) / len(rows), 3),
            "empirical_hit_rate": round(sum(o for _, o in rows) / len(rows), 3),
        })

    out = {
        "decision_hour_local": args.hour,
        "bias_adjustments_enabled": not args.no_bias,
        "days_scored": n_days_scored,
        "samples": len(samples),
        "brier_score": round(brier, 4),
        "calibration_curve": curve,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
