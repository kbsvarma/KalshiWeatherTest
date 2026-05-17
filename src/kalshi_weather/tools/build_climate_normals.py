"""Build per-city climatology normals from NCEI GHCN-Daily.

For each city in the registry, fetches the last 30 years of daily highs from
the station's GHCN-Daily file and computes (mean, std) per calendar month.
Writes one cache file per city to:

    data/reference/climate_normals/{city_id}.json

Schema of each cache file:
    {
      "city_id": "nyc",
      "station_id": "USW00094728",
      "window_years": 30,
      "min_year": 1995,
      "max_year": 2024,
      "sample_counts_by_month": {"1": 930, "2": 840, ...},
      "tmax_f_by_month": {
          "1": {"mean": 39.7, "std": 9.4, "p10": 27, "p90": 53},
          ...
      },
      "tmin_f_by_month": {"1": {"mean": ..., "std": ...}, ...}
    }

This is a one-shot tool. Re-run annually (or whenever station data changes).
Skips cities where the station ID is unknown or the file 404s — does NOT
fabricate data.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

from kalshi_weather.clients.ncei import NceiGhcnClient, NceiGhcnClientError
from kalshi_weather.storage import FileReferenceRegistry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute and cache per-month climatology normals (TMAX, TMIN) "
                    "for every city in the registry."
    )
    parser.add_argument(
        "--window-years",
        type=int,
        default=30,
        help="How many years of historical data to use (default: 30).",
    )
    parser.add_argument(
        "--city-id",
        action="append",
        dest="city_ids",
        help="Limit to specific cities (repeatable). Default: all.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/reference/climate_normals",
        help="Where to write the cache files.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    today = date.today()
    min_year = today.year - args.window_years
    max_year = today.year - 1  # most recent complete year

    selected = {c.lower() for c in (args.city_ids or [])}

    # Build station-id index from registry
    station_by_id = {st.station_id: st for st in seed.stations}

    results: list[dict] = []
    for profile in seed.city_profiles:
        if selected and profile.city_id not in selected:
            continue
        station = station_by_id.get(profile.station_id)
        if station is None:
            results.append({
                "city_id": profile.city_id,
                "status": "skipped",
                "reason": f"station {profile.station_id} not in registry",
            })
            continue
        ncei_id = station.ncei_station_id
        if not ncei_id:
            results.append({
                "city_id": profile.city_id,
                "status": "skipped",
                "reason": "no ncei_station_id on station",
            })
            continue

        print(f"[NCEI] {profile.city_id}: fetching GHCN for {ncei_id}...", flush=True)
        try:
            csv_text = NceiGhcnClient.fetch_station_csv(ncei_id)
        except NceiGhcnClientError as exc:
            results.append({
                "city_id": profile.city_id,
                "ncei_station_id": ncei_id,
                "status": "fetch_failed",
                "reason": str(exc)[:200],
            })
            continue

        tmax_by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
        tmin_by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
        record_count = 0
        for record in NceiGhcnClient.parse_records(
            csv_text, station_id=ncei_id, min_year=min_year,
        ):
            if record.observation_date.year > max_year:
                continue
            if record.tmax_f is not None:
                tmax_by_month[record.observation_date.month].append(record.tmax_f)
            if record.tmin_f is not None:
                tmin_by_month[record.observation_date.month].append(record.tmin_f)
            record_count += 1

        if record_count < 365:  # less than a year of data — refuse to publish
            results.append({
                "city_id": profile.city_id,
                "ncei_station_id": ncei_id,
                "status": "insufficient_data",
                "record_count": record_count,
            })
            continue

        cache = {
            "city_id": profile.city_id,
            "station_id": ncei_id,
            "window_years": args.window_years,
            "min_year": min_year,
            "max_year": max_year,
            "sample_counts_by_month": {
                str(m): len(values) for m, values in tmax_by_month.items()
            },
            "tmax_f_by_month": _summarize(tmax_by_month),
            "tmin_f_by_month": _summarize(tmin_by_month),
        }

        out_path = output_dir / f"{profile.city_id}.json"
        out_path.write_text(json.dumps(cache, indent=2, sort_keys=True))

        july = cache["tmax_f_by_month"].get("7", {})
        print(
            f"  ✓ {profile.city_id}: {record_count} records, "
            f"July TMAX mean={july.get('mean'):.1f}°F std={july.get('std'):.1f}°F",
            flush=True,
        )
        results.append({
            "city_id": profile.city_id,
            "ncei_station_id": ncei_id,
            "status": "ok",
            "record_count": record_count,
            "cache_file": str(out_path),
        })

    print("---")
    print(json.dumps({
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "skipped": sum(1 for r in results if r.get("status") in ("skipped", "insufficient_data")),
        "failed": sum(1 for r in results if r.get("status") == "fetch_failed"),
        "results": results,
    }, indent=2))


def _summarize(values_by_month: dict[int, list[float]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for month, values in values_by_month.items():
        if len(values) < 30:
            out[str(month)] = {
                "mean": None,
                "std": None,
                "p10": None,
                "p90": None,
                "sample_count": len(values),
            }
            continue
        values_sorted = sorted(values)
        n = len(values_sorted)
        out[str(month)] = {
            "mean": round(statistics.mean(values), 2),
            "std": round(statistics.stdev(values), 2) if len(values) > 1 else 0.0,
            "p10": round(values_sorted[max(0, int(n * 0.10) - 1)], 1),
            "p90": round(values_sorted[min(n - 1, int(n * 0.90))], 1),
            "sample_count": n,
        }
    return out


if __name__ == "__main__":
    main()
