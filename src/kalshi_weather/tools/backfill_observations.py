from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Iterable

from kalshi_weather.clients import NwsWeatherClient
from kalshi_weather.ingestion.adapters import NwsObservationAdapter
from kalshi_weather.ingestion.contracts import RawPayloadRecord
from kalshi_weather.storage import FileRawStore, FileReferenceRegistry, SQLiteStateStore


def _iter_windows(
    *,
    start: datetime,
    end: datetime,
    chunk_hours: int,
) -> Iterable[tuple[datetime, datetime]]:
    cursor = start
    delta = timedelta(hours=max(chunk_hours, 1))
    while cursor < end:
        window_end = min(cursor + delta, end)
        yield cursor, window_end
        cursor = window_end


def _selected_contexts(registry: FileReferenceRegistry, city_ids: set[str] | None):
    seed = registry.load_or_default()
    contexts = registry.iter_city_contexts(seed)
    if not city_ids:
        return seed, contexts
    filtered = [context for context in contexts if context.city_profile.city_id in city_ids]
    return seed, filtered


def main(
    *,
    days: int = 7,
    chunk_hours: int = 12,
    request_limit: int = 200,
    city_ids: set[str] | None = None,
) -> None:
    registry = FileReferenceRegistry()
    _seed, contexts = _selected_contexts(registry, city_ids)
    client = NwsWeatherClient()
    raw_store = FileRawStore("data/raw")
    state_store = SQLiteStateStore("data/state/runtime.sqlite3")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    city_reports = []
    total_saved = 0
    latest_event_times: list[str] = []
    for context in contexts:
        station = context.station
        observations = []
        window_count = 0
        for window_start, window_end in _iter_windows(start=start, end=end, chunk_hours=chunk_hours):
            payload = client.get_observations(
                station.nws_station_api_id,
                start=window_start,
                end=window_end,
                limit=request_limit,
            )
            text = json.dumps(payload, sort_keys=True)
            raw = RawPayloadRecord(
                source_name="nws_observation_backfill",
                source_endpoint=f"{client.BASE_URL}/stations/{station.nws_station_api_id}/observations",
                request_params={
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "limit": request_limit,
                },
                transport_status=200,
                payload_hash=sha256(text.encode("utf-8")).hexdigest(),
                parser_version="nws_observation_v1",
                ingest_time=window_end,
                event_time=None,
                payload=text,
                metadata={"station_id": station.station_id, "city_id": context.city_profile.city_id},
            )
            raw_store.write(raw)
            adapter = NwsObservationAdapter(client, station, limit=request_limit)
            normalized = [env.record for env in adapter.normalize(raw)]
            if normalized:
                state_store.save_observations(normalized)
                observations.extend(normalized)
            window_count += 1
        total_saved += len(observations)
        event_times = sorted({obs.event_time.isoformat() for obs in observations})
        if event_times:
            latest_event_times.append(event_times[-1])
        city_reports.append(
            {
                "city_id": context.city_profile.city_id,
                "station_id": station.station_id,
                "saved_observation_count": len(event_times),
                "oldest_event_time": event_times[0] if event_times else None,
                "latest_event_time": event_times[-1] if event_times else None,
                "window_count": window_count,
            }
        )
    print(
        json.dumps(
            {
                "saved_observation_count": total_saved,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "latest_event_time": max(latest_event_times) if latest_event_times else None,
                "city_reports": city_reports,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill NWS station observations into the runtime store.")
    parser.add_argument("--days", type=int, default=7, help="How many trailing days to backfill.")
    parser.add_argument(
        "--chunk-hours",
        type=int,
        default=12,
        help="Chunk size for each NWS observations request.",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=200,
        help="Maximum observations requested per chunk.",
    )
    parser.add_argument(
        "--city",
        action="append",
        default=[],
        help="Optional city_id filter. Repeat for multiple cities.",
    )
    args = parser.parse_args()
    main(
        days=max(args.days, 1),
        chunk_hours=max(args.chunk_hours, 1),
        request_limit=max(args.request_limit, 1),
        city_ids={city.strip().lower() for city in args.city if city.strip()} or None,
    )
