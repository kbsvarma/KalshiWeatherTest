"""NCEI GHCN-Daily bulk-access client.

Public, no-auth endpoint that returns the full daily-summary CSV for a single
station ID (e.g. USW00094728 for NY Central Park). We use this to compute
30-year climatology priors per city per month — feeds T1.2 of the scientific
roadmap.

The endpoint URL: https://www.ncei.noaa.gov/data/global-historical-climatology
-network-daily/access/{station_id}.csv

Files are typically 1–3 MB per station. Designed for one-time fetch + cache,
not per-cycle calls.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from io import StringIO
from typing import Iterable

from .http import http_get_text


GHCN_BULK_URL_TEMPLATE = (
    "https://www.ncei.noaa.gov/data/"
    "global-historical-climatology-network-daily/access/{station_id}.csv"
)


class NceiGhcnClientError(RuntimeError):
    """Raised when GHCN data cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class GhcnDailyRecord:
    """One day of climate observations for one station, in friendly units."""

    station_id: str
    observation_date: date
    tmax_f: float | None  # daily maximum temperature in °F
    tmin_f: float | None  # daily minimum temperature in °F


class NceiGhcnClient:
    """Bulk-CSV client for GHCN-Daily station files."""

    @classmethod
    def fetch_station_csv(cls, station_id: str) -> str:
        """Return the raw CSV body for one station. Raises on network failure."""
        url = GHCN_BULK_URL_TEMPLATE.format(station_id=station_id)
        try:
            return http_get_text(url)
        except Exception as exc:
            raise NceiGhcnClientError(
                f"GHCN fetch failed for {station_id}: {exc}"
            ) from exc

    @classmethod
    def parse_records(
        cls,
        csv_text: str,
        *,
        station_id: str,
        min_year: int | None = None,
    ) -> Iterable[GhcnDailyRecord]:
        """Yield GhcnDailyRecord rows from a CSV body.

        `min_year` filters to records with year >= min_year (e.g. 1995 for a
        30-year window ending 2024).

        Temperature conversion: GHCN stores TMAX/TMIN in tenths of °C as
        integers, with '' or '-9999' meaning missing. We convert to °F and
        skip records with no usable TMAX.
        """
        reader = csv.DictReader(StringIO(csv_text))
        for row in reader:
            date_str = row.get("DATE") or ""
            if len(date_str) != 10:
                continue
            try:
                obs_date = date.fromisoformat(date_str)
            except ValueError:
                continue
            if min_year is not None and obs_date.year < min_year:
                continue

            tmax_f = _ghcn_tenth_c_to_f(row.get("TMAX"))
            tmin_f = _ghcn_tenth_c_to_f(row.get("TMIN"))
            if tmax_f is None and tmin_f is None:
                continue
            yield GhcnDailyRecord(
                station_id=station_id,
                observation_date=obs_date,
                tmax_f=tmax_f,
                tmin_f=tmin_f,
            )


def _ghcn_tenth_c_to_f(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw == "-9999":
        return None
    try:
        tenths_c = int(raw)
    except ValueError:
        return None
    celsius = tenths_c / 10.0
    return celsius * 9.0 / 5.0 + 32.0
