from __future__ import annotations

import json

from kalshi_weather.analytics import build_nowcast_validation_report
from kalshi_weather.storage import FileReferenceRegistry, SQLiteStateStore


def main() -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    city_reports = {}
    for context in registry.iter_city_contexts(seed):
        observations = store.get_all_observations(context.station.station_id)
        forecasts = store.get_latest_forecasts(context.station.station_id)
        city_reports[context.city_profile.city_id] = build_nowcast_validation_report(
            observations,
            forecasts,
            context.city_profile,
            station_timezone=context.station.timezone,
        )
    print(json.dumps({"city_reports": city_reports}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
