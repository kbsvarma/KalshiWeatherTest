from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from kalshi_weather.domain.models import CityProfile, SeriesDefinition, StationReference


@dataclass(frozen=True, slots=True)
class RegistrySeed:
    version: str
    created_at: datetime
    stations: tuple[StationReference, ...]
    city_profiles: tuple[CityProfile, ...]
    series_definitions: tuple[SeriesDefinition, ...]


@dataclass(frozen=True, slots=True)
class CityContext:
    city_profile: CityProfile
    station: StationReference
    series_definition: SeriesDefinition


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(val) for key, val in value.items()}
    return value


class FileReferenceRegistry:
    CURRENT_SEED_VERSION = "seed_v4"

    def __init__(self, path: Path | str = "data/reference/registry.json") -> None:
        self.path = Path(path)

    def write_seed(self, seed: RegistrySeed) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": seed.version,
            "created_at": seed.created_at.isoformat(),
            "stations": [_serialize(asdict(item)) for item in seed.stations],
            "city_profiles": [_serialize(asdict(item)) for item in seed.city_profiles],
            "series_definitions": [
                _serialize(asdict(item)) for item in seed.series_definitions
            ],
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return self.path

    def load(self) -> RegistrySeed:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return RegistrySeed(
            version=str(payload["version"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            stations=tuple(self._station_from_payload(item) for item in payload["stations"]),
            city_profiles=tuple(self._city_profile_from_payload(item) for item in payload["city_profiles"]),
            series_definitions=tuple(
                self._series_definition_from_payload(item) for item in payload["series_definitions"]
            ),
        )

    def load_or_default(self) -> RegistrySeed:
        if self.path.exists():
            seed = self._upgrade_seed(self.load())
            self.write_seed(seed)
            return seed
        seed = self.default_seed()
        self.write_seed(seed)
        return seed

    def iter_city_contexts(self, seed: RegistrySeed | None = None) -> tuple[CityContext, ...]:
        """Yield one CityContext per series_definition.

        A city with multiple series (e.g., daily HIGH + daily LOW) yields
        multiple contexts that share the same station and city_profile but
        differ in series_definition. The downstream runner processes each
        context independently so both series get evaluated each cycle.
        """
        active_seed = seed or self.load_or_default()
        station_by_id = {station.station_id: station for station in active_seed.stations}
        profile_by_city_id = {p.city_id: p for p in active_seed.city_profiles}
        contexts: list[CityContext] = []
        for series in active_seed.series_definitions:
            city_profile = profile_by_city_id.get(series.city_id)
            if city_profile is None:
                continue
            station = station_by_id.get(city_profile.station_id)
            if station is None:
                continue
            contexts.append(
                CityContext(
                    city_profile=city_profile,
                    station=station,
                    series_definition=series,
                )
            )
        return tuple(contexts)

    def context_by_city_id(self, seed: RegistrySeed | None = None) -> dict[str, CityContext]:
        return {
            context.city_profile.city_id: context for context in self.iter_city_contexts(seed)
        }

    def context_by_series_ticker(self, seed: RegistrySeed | None = None) -> dict[str, CityContext]:
        return {
            context.series_definition.series_ticker: context for context in self.iter_city_contexts(seed)
        }

    def context_for_market_ticker(
        self,
        market_ticker: str,
        seed: RegistrySeed | None = None,
    ) -> CityContext | None:
        series_ticker = market_ticker.split("-", 1)[0]
        return self.context_by_series_ticker(seed).get(series_ticker)

    def _upgrade_seed(self, seed: RegistrySeed) -> RegistrySeed:
        default_seed = self.default_seed()
        default_station_ids = {station.station_id for station in default_seed.stations}
        default_city_ids = {city.city_id for city in default_seed.city_profiles}
        default_series_tickers = {series.series_ticker for series in default_seed.series_definitions}
        seed_series_tickers = {series.series_ticker for series in seed.series_definitions}
        # An "upgraded" seed must keep all defaults but may have additional
        # cities/series on top. The version string is allowed to differ as long
        # as the loaded seed is a superset of the default.
        loaded_versions_supersets_default = (
            default_station_ids.issubset({station.station_id for station in seed.stations})
            and default_city_ids.issubset({city.city_id for city in seed.city_profiles})
            and default_series_tickers.issubset(seed_series_tickers)
        )
        if loaded_versions_supersets_default:
            return seed

        station_by_id = {station.station_id: station for station in seed.stations}
        for station in default_seed.stations:
            station_by_id[station.station_id] = station

        city_by_id = {city.city_id: city for city in seed.city_profiles}
        for city in default_seed.city_profiles:
            city_by_id[city.city_id] = city

        # IMPORTANT: series merge must key by series_ticker (unique per market),
        # NOT by city_id (which collapses multi-series-per-city cases like
        # daily HIGH + daily LOW sharing the same city).
        series_by_ticker = {series.series_ticker: series for series in seed.series_definitions}
        for series in default_seed.series_definitions:
            series_by_ticker.setdefault(series.series_ticker, series)

        def _ordered(default_items: tuple[Any, ...], merged_by_key: dict[str, Any], key_fn: Any) -> tuple[Any, ...]:
            items: list[Any] = []
            seen: set[str] = set()
            for item in default_items:
                key = key_fn(item)
                merged = merged_by_key.get(key)
                if merged is not None:
                    items.append(merged)
                    seen.add(key)
            for item in merged_by_key.values():
                key = key_fn(item)
                if key in seen:
                    continue
                items.append(item)
            return tuple(items)

        return RegistrySeed(
            version=default_seed.version,
            created_at=seed.created_at,
            stations=_ordered(default_seed.stations, station_by_id, lambda item: item.station_id),
            city_profiles=_ordered(default_seed.city_profiles, city_by_id, lambda item: item.city_id),
            series_definitions=_ordered(
                default_seed.series_definitions,
                series_by_ticker,
                lambda item: item.series_ticker,
            ),
        )

    @staticmethod
    def _station_from_payload(payload: dict[str, Any]) -> StationReference:
        return StationReference(
            station_id=str(payload["station_id"]),
            station_name=str(payload["station_name"]),
            metar_code=str(payload["metar_code"]),
            nws_station_api_id=str(payload["nws_station_api_id"]),
            climate_product_id=str(payload["climate_product_id"]),
            latitude=Decimal(str(payload["latitude"])),
            longitude=Decimal(str(payload["longitude"])),
            timezone=str(payload["timezone"]),
            wfo_office=str(payload["wfo_office"]),
            grid_x=int(payload["grid_x"]),
            grid_y=int(payload["grid_y"]),
            climate_timezone_basis=str(payload["climate_timezone_basis"]),
            ncei_station_id=(
                str(payload["ncei_station_id"])
                if payload.get("ncei_station_id") is not None
                else None
            ),
            source_urls=tuple(payload.get("source_urls") or ()),
        )

    @staticmethod
    def _city_profile_from_payload(payload: dict[str, Any]) -> CityProfile:
        return CityProfile(
            city_id=str(payload["city_id"]),
            display_name=str(payload["display_name"]),
            station_id=str(payload["station_id"]),
            region_cluster=str(payload["region_cluster"]),
            marine_sensitive_flag=bool(payload["marine_sensitive_flag"]),
            onshore_wind_sectors=tuple(int(value) for value in payload.get("onshore_wind_sectors") or ()),
            typical_peak_hour_local_by_season={
                str(key): int(value)
                for key, value in (payload.get("typical_peak_hour_local_by_season") or {}).items()
            },
            heating_window_by_season={
                str(key): (int(value[0]), int(value[1]))
                for key, value in (payload.get("heating_window_by_season") or {}).items()
            },
            cloud_shock_cap_f=Decimal(str(payload["cloud_shock_cap_f"])),
            storm_shock_cap_f=Decimal(str(payload["storm_shock_cap_f"])),
            marine_intrusion_cap_f=Decimal(str(payload["marine_intrusion_cap_f"])),
            wind_shift_cap_f=Decimal(str(payload["wind_shift_cap_f"])),
            calibration_buckets_version=str(payload["calibration_buckets_version"]),
        )

    @staticmethod
    def _series_definition_from_payload(payload: dict[str, Any]) -> SeriesDefinition:
        active_to = payload.get("active_to")
        return SeriesDefinition(
            series_ticker=str(payload["series_ticker"]),
            title=str(payload["title"]),
            city_id=str(payload["city_id"]),
            category=str(payload["category"]),
            frequency=str(payload["frequency"]),
            active_from=datetime.fromisoformat(payload["active_from"]),
            active_to=datetime.fromisoformat(active_to) if active_to else None,
            source_provenance=tuple(payload.get("source_provenance") or ()),
        )

    @staticmethod
    def default_seed() -> RegistrySeed:
        created_at = datetime.now(timezone.utc)
        stations = (
            StationReference(
                station_id="nyc-central-park",
                station_name="Central Park",
                metar_code="KNYC",
                nws_station_api_id="KNYC",
                climate_product_id="CLINYC",
                latitude=Decimal("40.7829"),
                longitude=Decimal("-73.9654"),
                timezone="America/New_York",
                wfo_office="OKX",
                grid_x=34,
                grid_y=38,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00094728",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHNY",
                    "https://forecast.weather.gov/product.php?site=OKX&issuedby=NYC&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="phl-airport",
                station_name="Philadelphia International Airport",
                metar_code="KPHL",
                nws_station_api_id="KPHL",
                climate_product_id="CLIPHL",
                latitude=Decimal("39.87327"),
                longitude=Decimal("-75.22678"),
                timezone="America/New_York",
                wfo_office="PHI",
                grid_x=48,
                grid_y=72,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00013739",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHPHIL",
                    "https://forecast.weather.gov/product.php?site=PHI&issuedby=PHL&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="aus-bergstrom",
                station_name="Austin-Bergstrom International Airport",
                metar_code="KAUS",
                nws_station_api_id="KAUS",
                climate_product_id="CLIAUS",
                latitude=Decimal("30.18304"),
                longitude=Decimal("-97.67987"),
                timezone="America/Chicago",
                wfo_office="EWX",
                grid_x=158,
                grid_y=87,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00013904",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHAUS",
                    "https://forecast.weather.gov/product.php?site=EWX&issuedby=AUS&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="den-airport",
                station_name="Denver International Airport",
                metar_code="KDEN",
                nws_station_api_id="KDEN",
                climate_product_id="CLIDEN",
                latitude=Decimal("39.84658"),
                longitude=Decimal("-104.65622"),
                timezone="America/Denver",
                wfo_office="BOU",
                grid_x=75,
                grid_y=66,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00003017",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHDEN",
                    "https://forecast.weather.gov/product.php?site=BOU&issuedby=DEN&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="bos-logan",
                station_name="Boston Logan International Airport",
                metar_code="KBOS",
                nws_station_api_id="KBOS",
                climate_product_id="CLIBOS",
                latitude=Decimal("42.36056"),
                longitude=Decimal("-71.01056"),
                timezone="America/New_York",
                wfo_office="BOX",
                grid_x=73,
                grid_y=90,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00014739",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHTBOS",
                    "https://forecast.weather.gov/product.php?site=BOX&issuedby=BOS&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="mia-airport",
                station_name="Miami International Airport",
                metar_code="KMIA",
                nws_station_api_id="KMIA",
                climate_product_id="CLIMIA",
                latitude=Decimal("25.79056"),
                longitude=Decimal("-80.31639"),
                timezone="America/New_York",
                wfo_office="MFL",
                grid_x=105,
                grid_y=51,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00012839",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHMIA",
                    "https://forecast.weather.gov/product.php?site=MFL&issuedby=MIA&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="chi-midway",
                station_name="Chicago Midway Airport",
                metar_code="KMDW",
                nws_station_api_id="KMDW",
                climate_product_id="CLIMDW",
                latitude=Decimal("41.78417"),
                longitude=Decimal("-87.75528"),
                timezone="America/Chicago",
                wfo_office="LOT",
                grid_x=72,
                grid_y=69,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00014819",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHCHI",
                    "https://forecast.weather.gov/product.php?site=LOT&issuedby=MDW&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
            StationReference(
                station_id="lax-airport",
                station_name="Los Angeles International Airport",
                metar_code="KLAX",
                nws_station_api_id="KLAX",
                climate_product_id="CLILAX",
                latitude=Decimal("33.93806"),
                longitude=Decimal("-118.38889"),
                timezone="America/Los_Angeles",
                wfo_office="LOX",
                grid_x=149,
                grid_y=41,
                climate_timezone_basis="LOCAL_STANDARD_TIME",
                ncei_station_id="USW00023174",
                source_urls=(
                    "https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHLAX",
                    "https://forecast.weather.gov/product.php?site=LOX&issuedby=LAX&product=CLI&format=TXT&version=1&glossary=0",
                ),
            ),
        )
        city_profiles = (
            CityProfile(
                city_id="nyc",
                display_name="New York City",
                station_id="nyc-central-park",
                region_cluster="northeast_metro",
                marine_sensitive_flag=True,
                onshore_wind_sectors=(80, 90, 100, 110, 120, 130, 140, 150),
                typical_peak_hour_local_by_season={"MAM": 15, "JJA": 16, "SON": 15, "DJF": 14},
                heating_window_by_season={
                    "MAM": (9, 17),
                    "JJA": (9, 18),
                    "SON": (9, 16),
                    "DJF": (10, 15),
                },
                cloud_shock_cap_f=Decimal("5"),
                storm_shock_cap_f=Decimal("7"),
                marine_intrusion_cap_f=Decimal("6"),
                wind_shift_cap_f=Decimal("4"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="phl",
                display_name="Philadelphia",
                station_id="phl-airport",
                region_cluster="northeast_corridor",
                marine_sensitive_flag=True,
                onshore_wind_sectors=(90, 100, 110, 120, 130, 140, 150),
                typical_peak_hour_local_by_season={"MAM": 15, "JJA": 16, "SON": 15, "DJF": 14},
                heating_window_by_season={
                    "MAM": (9, 17),
                    "JJA": (9, 18),
                    "SON": (9, 16),
                    "DJF": (10, 15),
                },
                cloud_shock_cap_f=Decimal("5"),
                storm_shock_cap_f=Decimal("7"),
                marine_intrusion_cap_f=Decimal("4"),
                wind_shift_cap_f=Decimal("4"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="aus",
                display_name="Austin",
                station_id="aus-bergstrom",
                region_cluster="texas_inland",
                marine_sensitive_flag=False,
                onshore_wind_sectors=(),
                typical_peak_hour_local_by_season={"MAM": 16, "JJA": 17, "SON": 16, "DJF": 15},
                heating_window_by_season={
                    "MAM": (10, 18),
                    "JJA": (9, 19),
                    "SON": (10, 18),
                    "DJF": (10, 17),
                },
                cloud_shock_cap_f=Decimal("6"),
                storm_shock_cap_f=Decimal("8"),
                marine_intrusion_cap_f=Decimal("1"),
                wind_shift_cap_f=Decimal("5"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="den",
                display_name="Denver",
                station_id="den-airport",
                region_cluster="front_range",
                marine_sensitive_flag=False,
                onshore_wind_sectors=(),
                typical_peak_hour_local_by_season={"MAM": 15, "JJA": 16, "SON": 15, "DJF": 14},
                heating_window_by_season={
                    "MAM": (9, 17),
                    "JJA": (9, 18),
                    "SON": (10, 17),
                    "DJF": (10, 16),
                },
                cloud_shock_cap_f=Decimal("6"),
                storm_shock_cap_f=Decimal("7"),
                marine_intrusion_cap_f=Decimal("1"),
                wind_shift_cap_f=Decimal("6"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="bos",
                display_name="Boston",
                station_id="bos-logan",
                region_cluster="northeast_metro",
                marine_sensitive_flag=True,
                onshore_wind_sectors=(80, 90, 100, 110, 120, 130, 140, 150, 160),
                typical_peak_hour_local_by_season={"MAM": 15, "JJA": 15, "SON": 14, "DJF": 13},
                heating_window_by_season={
                    "MAM": (9, 17),
                    "JJA": (9, 17),
                    "SON": (9, 16),
                    "DJF": (10, 15),
                },
                cloud_shock_cap_f=Decimal("5"),
                storm_shock_cap_f=Decimal("6"),
                marine_intrusion_cap_f=Decimal("7"),
                wind_shift_cap_f=Decimal("4"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="mia",
                display_name="Miami",
                station_id="mia-airport",
                region_cluster="florida_metro",
                marine_sensitive_flag=True,
                onshore_wind_sectors=(50, 60, 70, 80, 90, 100, 110, 120, 130, 140),
                typical_peak_hour_local_by_season={"MAM": 15, "JJA": 15, "SON": 15, "DJF": 14},
                heating_window_by_season={
                    "MAM": (9, 17),
                    "JJA": (9, 17),
                    "SON": (9, 17),
                    "DJF": (10, 16),
                },
                cloud_shock_cap_f=Decimal("6"),
                storm_shock_cap_f=Decimal("7"),
                marine_intrusion_cap_f=Decimal("7"),
                wind_shift_cap_f=Decimal("5"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="chi",
                display_name="Chicago",
                station_id="chi-midway",
                region_cluster="midwest_metro",
                marine_sensitive_flag=True,
                onshore_wind_sectors=(30, 40, 50, 60, 70, 80, 90, 100, 110),
                typical_peak_hour_local_by_season={"MAM": 15, "JJA": 16, "SON": 15, "DJF": 14},
                heating_window_by_season={
                    "MAM": (9, 17),
                    "JJA": (9, 18),
                    "SON": (9, 16),
                    "DJF": (10, 15),
                },
                cloud_shock_cap_f=Decimal("5"),
                storm_shock_cap_f=Decimal("7"),
                marine_intrusion_cap_f=Decimal("4"),
                wind_shift_cap_f=Decimal("4"),
                calibration_buckets_version="seed_v1",
            ),
            CityProfile(
                city_id="lax",
                display_name="Los Angeles",
                station_id="lax-airport",
                region_cluster="west_coast_metro",
                marine_sensitive_flag=True,
                onshore_wind_sectors=(200, 210, 220, 230, 240, 250, 260, 270, 280),
                typical_peak_hour_local_by_season={"MAM": 14, "JJA": 14, "SON": 14, "DJF": 14},
                heating_window_by_season={
                    "MAM": (10, 17),
                    "JJA": (10, 17),
                    "SON": (10, 16),
                    "DJF": (10, 15),
                },
                cloud_shock_cap_f=Decimal("6"),
                storm_shock_cap_f=Decimal("5"),
                marine_intrusion_cap_f=Decimal("7"),
                wind_shift_cap_f=Decimal("5"),
                calibration_buckets_version="seed_v1",
            ),
        )
        series_definitions = (
            SeriesDefinition(
                series_ticker="KXHIGHNY",
                title="Highest temperature in NYC",
                city_id="nyc",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHPHIL",
                title="Highest temperature in Philadelphia",
                city_id="phl",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHAUS",
                title="Highest temperature in Austin",
                city_id="aus",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHDEN",
                title="Highest temperature in Denver",
                city_id="den",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHTBOS",
                title="Boston Maximum Daily Temperature",
                city_id="bos",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHMIA",
                title="Highest temperature in Miami",
                city_id="mia",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHCHI",
                title="Highest temperature in Chicago",
                city_id="chi",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
            SeriesDefinition(
                series_ticker="KXHIGHLAX",
                title="Highest temperature in Los Angeles",
                city_id="lax",
                category="Climate and Weather",
                frequency="daily",
                active_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                active_to=None,
                source_provenance=("kalshi_public_series", "manual_seed_live_city_set"),
            ),
        )
        return RegistrySeed(
            version=FileReferenceRegistry.CURRENT_SEED_VERSION,
            created_at=created_at,
            stations=stations,
            city_profiles=city_profiles,
            series_definitions=series_definitions,
        )
