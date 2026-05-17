from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time

from kalshi_weather.analytics import build_provider_reliability_report
from kalshi_weather.clients import NwsWeatherClient
from kalshi_weather.ingestion.adapters import NwsGridForecastAdapter, NwsHourlyForecastAdapter, NwsObservationAdapter
from kalshi_weather.storage import DerivedAnalyticsStore, FileRawStore, FileReferenceRegistry, SQLiteStateStore


def _run_once() -> dict[str, object]:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    raw_store = FileRawStore("data/raw")
    state_store = SQLiteStateStore("data/state/runtime.sqlite3")
    derived_store = DerivedAnalyticsStore("data/derived")

    nws_client = NwsWeatherClient()
    city_reports: list[dict[str, object]] = []
    total_observation_count = 0
    total_forecast_count = 0
    for context in registry.iter_city_contexts(seed):
        obs_adapter = NwsObservationAdapter(nws_client, context.station, limit=8)
        obs_raw = obs_adapter.fetch_raw()
        raw_store.write(obs_raw)
        observations = [env.record for env in obs_adapter.normalize(obs_raw)]
        state_store.save_observations(observations)

        hourly_adapter = NwsHourlyForecastAdapter(nws_client, context.station)
        hourly_raw = hourly_adapter.fetch_raw()
        raw_store.write(hourly_raw)
        forecasts = [env.record for env in hourly_adapter.normalize(hourly_raw)]

        grid_adapter = NwsGridForecastAdapter(nws_client, context.station)
        grid_raw = grid_adapter.fetch_raw()
        raw_store.write(grid_raw)
        forecasts.extend(env.record for env in grid_adapter.normalize(grid_raw))
        state_store.save_forecasts(forecasts)

        calibration_report = build_provider_reliability_report(
            forecasts=state_store.get_all_forecasts(context.station.station_id),
            observations=state_store.get_all_observations(context.station.station_id),
            station=context.station,
        )
        derived_store.write_provider_reliability(context.city_profile.city_id, calibration_report)
        total_observation_count += len(observations)
        total_forecast_count += len(forecasts)
        city_reports.append(
            {
                "city_id": context.city_profile.city_id,
                "observation_count": len(observations),
                "forecast_count": len(forecasts),
                "global_sample_count": calibration_report.get("global_sample_count"),
                "global_unique_run_count": calibration_report.get("global_unique_run_count"),
                "sample_sufficient": calibration_report.get("sample_sufficient"),
                "provider_weights": calibration_report.get("provider_weights", {}),
            }
        )
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "observation_count": total_observation_count,
        "forecast_count": total_forecast_count,
        "city_reports": city_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive official weather state repeatedly for calibration growth.")
    parser.add_argument("--iterations", type=int, default=1, help="How many capture passes to run.")
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=900,
        help="Seconds to sleep between passes when iterations > 1.",
    )
    args = parser.parse_args()

    reports = []
    for index in range(args.iterations):
        reports.append(_run_once())
        if index < args.iterations - 1:
            time.sleep(max(args.sleep_seconds, 1))

    print(
        json.dumps(
            {
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "iterations": args.iterations,
                "reports": reports,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
