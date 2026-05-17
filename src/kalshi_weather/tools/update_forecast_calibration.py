from __future__ import annotations

import json

from kalshi_weather.analytics import build_provider_reliability_report
from kalshi_weather.storage import DerivedAnalyticsStore, FileReferenceRegistry, SQLiteStateStore


def main() -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    state_store = SQLiteStateStore("data/state/runtime.sqlite3")
    derived_store = DerivedAnalyticsStore("data/derived")

    city_reports = []
    for context in registry.iter_city_contexts(seed):
        report = build_provider_reliability_report(
            forecasts=state_store.get_all_forecasts(context.station.station_id),
            observations=state_store.get_all_observations(context.station.station_id),
            station=context.station,
        )
        path = derived_store.write_provider_reliability(context.city_profile.city_id, report)
        city_reports.append(
            {
                "city_id": context.city_profile.city_id,
                "written_to": str(path),
                "report": report,
            }
        )
    print(
        json.dumps(
            {
                "city_reports": city_reports,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
