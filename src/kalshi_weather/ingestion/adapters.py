from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from kalshi_weather.clients.kalshi_public import KalshiPublicClient
from kalshi_weather.clients.nws import NwsClimateClient, NwsWeatherClient
from kalshi_weather.clients.open_meteo import OpenMeteoClient, OPEN_METEO_MODELS, provider_for_api_id
from kalshi_weather.domain.models import (
    ForecastSnapshot,
    MarketSnapshot,
    ObservationSnapshot,
    OrderbookSnapshot,
    SettlementReportSnapshot,
    StationReference,
    TradeSnapshot,
)
from kalshi_weather.ingestion.contracts import IngestionAdapter, NormalizedEnvelope, RawPayloadRecord
from kalshi_weather.market.orderbook import derive_implied_ask_ladders
from kalshi_weather.market.payloads import market_snapshot_from_payload, trade_snapshot_from_payload
from kalshi_weather.settlement.cli_parser import parse_cli_climate_report
from decimal import Decimal


def _c_to_f(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str((value * 9 / 5) + 32))


def _mps_to_kt(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value * 1.94384449))


def _m_to_mi(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value / 1609.344))


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_DURATION_RE = re.compile(r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?)?$")


def _duration_hours(duration_token: str) -> int:
    match = _DURATION_RE.fullmatch(duration_token)
    if not match:
        return 1
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    total = (days * 24) + hours
    return max(1, total)


def _expand_grid_values(
    entries: list[Mapping[str, Any]],
    converter,
    limit: int = 36,
) -> tuple[tuple[datetime, ...], tuple[Decimal, ...]]:
    expanded: list[tuple[datetime, Decimal]] = []
    for entry in entries:
        valid_time = str(entry.get("validTime") or "")
        if "/" in valid_time:
            start_token, duration_token = valid_time.split("/", 1)
        else:
            start_token, duration_token = valid_time, "PT1H"
        start = _parse_iso(start_token)
        duration_hours = _duration_hours(duration_token)
        value = converter(entry.get("value"))
        if value is None:
            continue
        current = start.replace(minute=0, second=0, microsecond=0)
        for hour in range(duration_hours):
            expanded.append((current, value))
            current = current + timedelta(hours=1)
    deduped: dict[datetime, Decimal] = {}
    for timestamp, value in expanded:
        deduped[timestamp] = value
    ordered = sorted(deduped.items())[:limit]
    return tuple(timestamp for timestamp, _ in ordered), tuple(value for _, value in ordered)


def _identity_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _cloud_pct_from_short_forecast(short_forecast: str | None) -> Decimal:
    if not short_forecast:
        return Decimal("50")
    text = short_forecast.lower()
    if "sunny" in text or "clear" in text:
        return Decimal("10")
    if "mostly clear" in text:
        return Decimal("20")
    if "partly cloudy" in text or "partly sunny" in text:
        return Decimal("40")
    if "mostly cloudy" in text:
        return Decimal("70")
    if "cloudy" in text or "overcast" in text:
        return Decimal("90")
    if "rain" in text or "showers" in text or "thunder" in text:
        return Decimal("100")
    return Decimal("50")


def _parse_wind_speed_to_kt(wind_speed: str | None) -> Decimal:
    if not wind_speed:
        return Decimal("0")
    digits = "".join(ch if ch.isdigit() else " " for ch in wind_speed).split()
    if not digits:
        return Decimal("0")
    mph = Decimal(digits[0])
    return mph * Decimal("0.868976")


class NwsCliAdapter(IngestionAdapter[SettlementReportSnapshot]):
    source_name = "nws_cli"
    parser_version = "cli_parser_v1"

    def __init__(
        self,
        client: NwsClimateClient,
        station: StationReference,
        issuedby: str,
        version: int = 1,
    ) -> None:
        self.client = client
        self.station = station
        self.issuedby = issuedby
        self.version = version

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        text = self.client.fetch_cli_text(
            site=self.station.wfo_office,
            issuedby=self.issuedby,
            version=self.version,
        )
        source_url = (
            f"https://forecast.weather.gov/product.php?site={self.station.wfo_office}"
            f"&issuedby={self.issuedby}&product=CLI&format=TXT&version={self.version}&glossary=0"
        )
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=source_url,
            request_params={"site": self.station.wfo_office, "issuedby": self.issuedby, "version": self.version},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=None,
            payload=text,
            metadata={"station_id": self.station.station_id},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[SettlementReportSnapshot], ...]:
        source_url = raw_payload.source_endpoint
        snapshot = parse_cli_climate_report(
            str(raw_payload.payload),
            station=self.station,
            raw_text_payload_id=raw_payload.payload_hash,
            source_url=source_url,
            report_version=self.version,
        )
        return (
            NormalizedEnvelope(
                raw_payload_id=raw_payload.payload_hash,
                normalized_at=datetime.now(timezone.utc),
                record=snapshot,
            ),
        )


class KalshiSeriesSnapshotAdapter(IngestionAdapter[Mapping[str, Any]]):
    source_name = "kalshi_public_series"
    parser_version = "kalshi_public_series_v1"

    def __init__(self, client: KalshiPublicClient, series_ticker: str) -> None:
        self.client = client
        self.series_ticker = series_ticker

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        payload = self.client.get_series(self.series_ticker)
        text = json.dumps(payload, sort_keys=True)
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=f"{self.client.base_url}/series/{self.series_ticker}",
            request_params={"series_ticker": self.series_ticker},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=None,
            payload=text,
            metadata={"series_ticker": self.series_ticker},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[Mapping[str, Any]], ...]:
        payload = json.loads(str(raw_payload.payload))
        return (
            NormalizedEnvelope(
                raw_payload_id=raw_payload.payload_hash,
                normalized_at=datetime.now(timezone.utc),
                record=payload,
            ),
        )


class NwsObservationAdapter(IngestionAdapter[ObservationSnapshot]):
    source_name = "nws_observation"
    parser_version = "nws_observation_v1"

    def __init__(self, client: NwsWeatherClient, station: StationReference, limit: int = 4) -> None:
        self.client = client
        self.station = station
        self.limit = limit

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        payload = self.client.get_recent_observations(self.station.nws_station_api_id, self.limit)
        text = json.dumps(payload, sort_keys=True)
        event_time = None
        features = payload.get("features", [])
        if features:
            latest_timestamp = features[0].get("properties", {}).get("timestamp")
            if latest_timestamp:
                event_time = _parse_iso(str(latest_timestamp))
        endpoint = f"{self.client.BASE_URL}/stations/{self.station.nws_station_api_id}/observations"
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=endpoint,
            request_params={"limit": self.limit},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=event_time,
            payload=text,
            metadata={"station_id": self.station.station_id},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[ObservationSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        features = payload.get("features", [])
        normalized: list[NormalizedEnvelope[ObservationSnapshot]] = []
        for feature in features:
            properties = feature.get("properties", {})
            timestamp = properties.get("timestamp")
            if not timestamp:
                continue
            cloud_layers = properties.get("cloudLayers") or []
            sky_cover_code = cloud_layers[0].get("amount") if cloud_layers else None
            text_description = properties.get("textDescription") or ""
            quality_flags: list[str] = []
            if properties.get("temperature", {}).get("value") is None:
                quality_flags.append("missing_temperature")
            if properties.get("windSpeed", {}).get("value") is None:
                quality_flags.append("missing_wind_speed")
            observation = ObservationSnapshot(
                station_id=self.station.station_id,
                event_time=_parse_iso(str(timestamp)),
                ingest_time=raw_payload.ingest_time,
                temperature_f=_c_to_f(properties.get("temperature", {}).get("value")),
                dewpoint_f=_c_to_f(properties.get("dewpoint", {}).get("value")),
                wind_dir_deg=properties.get("windDirection", {}).get("value"),
                wind_speed_kt=_mps_to_kt(properties.get("windSpeed", {}).get("value")),
                sky_cover_code=sky_cover_code,
                ceiling_ft=(
                    int(cloud_layers[0]["base"]["value"] * 3.28084)
                    if cloud_layers and cloud_layers[0].get("base", {}).get("value") is not None
                    else None
                ),
                visibility_mi=_m_to_mi(properties.get("visibility", {}).get("value")),
                weather_codes=tuple(
                    filter(
                        None,
                        (text_description.upper().replace(" ", "_"),),
                    )
                ),
                quality_flags=tuple(quality_flags),
                source_payload_id=raw_payload.payload_hash,
            )
            normalized.append(
                NormalizedEnvelope(
                    raw_payload_id=raw_payload.payload_hash,
                    normalized_at=datetime.now(timezone.utc),
                    record=observation,
                    quality_flags=observation.quality_flags,
                )
            )
        return tuple(normalized)


class NwsHourlyForecastAdapter(IngestionAdapter[ForecastSnapshot]):
    source_name = "nws_hourly_forecast"
    parser_version = "nws_hourly_forecast_v1"

    def __init__(self, client: NwsWeatherClient, station: StationReference) -> None:
        self.client = client
        self.station = station

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        point_payload = self.client.get_point_metadata(
            latitude=str(self.station.latitude),
            longitude=str(self.station.longitude),
        )
        forecast_hourly_url = point_payload["properties"]["forecastHourly"]
        forecast_payload = self.client.get_hourly_forecast(forecast_hourly_url)
        combined = {"point": point_payload, "forecast": forecast_payload}
        text = json.dumps(combined, sort_keys=True)
        updated = forecast_payload.get("properties", {}).get("updateTime")
        event_time = _parse_iso(str(updated)) if updated else None
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=str(forecast_hourly_url),
            request_params={"station_id": self.station.station_id},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=event_time,
            payload=text,
            metadata={"station_id": self.station.station_id},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[ForecastSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        point_payload = payload["point"]
        forecast_payload = payload["forecast"]
        periods = forecast_payload.get("properties", {}).get("periods", [])[:36]
        valid_for_times = tuple(_parse_iso(period["startTime"]) for period in periods)
        hourly_temp_path_f = tuple(
            Decimal(str(period["temperature"])) for period in periods
        )
        cloud_cover_path_pct = tuple(
            _cloud_pct_from_short_forecast(period.get("shortForecast")) for period in periods
        )
        wind_path = tuple(
            _parse_wind_speed_to_kt(period.get("windSpeed")) for period in periods
        )
        precipitation_path = tuple(
            Decimal(
                str(
                    (period.get("probabilityOfPrecipitation") or {}).get("value")
                    if (period.get("probabilityOfPrecipitation") or {}).get("value") is not None
                    else 0
                )
            )
            for period in periods
        )
        snapshot = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=_parse_iso(
                str(forecast_payload.get("properties", {}).get("updateTime"))
            ),
            ingest_time=raw_payload.ingest_time,
            valid_for_times=valid_for_times,
            hourly_temp_path_f=hourly_temp_path_f,
            cloud_cover_path_pct=cloud_cover_path_pct,
            wind_path=wind_path,
            precipitation_path=precipitation_path,
            provider_metadata={
                "station_id": self.station.station_id,
                "grid_id": point_payload["properties"]["gridId"],
                "grid_x": point_payload["properties"]["gridX"],
                "grid_y": point_payload["properties"]["gridY"],
                "forecast_hourly_url": raw_payload.source_endpoint,
            },
            source_payload_id=raw_payload.payload_hash,
        )
        return (
            NormalizedEnvelope(
                raw_payload_id=raw_payload.payload_hash,
                normalized_at=datetime.now(timezone.utc),
                record=snapshot,
            ),
        )


class NwsGridForecastAdapter(IngestionAdapter[ForecastSnapshot]):
    source_name = "nws_grid_forecast"
    parser_version = "nws_grid_forecast_v1"

    def __init__(self, client: NwsWeatherClient, station: StationReference) -> None:
        self.client = client
        self.station = station

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        point_payload = self.client.get_point_metadata(
            latitude=str(self.station.latitude),
            longitude=str(self.station.longitude),
        )
        forecast_grid_url = point_payload["properties"]["forecastGridData"]
        forecast_payload = self.client.get_gridpoint_forecast_data(forecast_grid_url)
        combined = {"point": point_payload, "forecast_grid": forecast_payload}
        text = json.dumps(combined, sort_keys=True)
        updated = forecast_payload.get("properties", {}).get("updateTime")
        event_time = _parse_iso(str(updated)) if updated else None
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=str(forecast_grid_url),
            request_params={"station_id": self.station.station_id},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=event_time,
            payload=text,
            metadata={"station_id": self.station.station_id},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[ForecastSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        point_payload = payload["point"]
        forecast_payload = payload["forecast_grid"]
        properties = forecast_payload.get("properties", {})

        valid_times, hourly_temp_path_f = _expand_grid_values(
            (properties.get("temperature") or {}).get("values", []),
            _c_to_f,
        )
        if not valid_times:
            return ()
        _cloud_times, cloud_cover_path_pct = _expand_grid_values(
            (properties.get("skyCover") or {}).get("values", []),
            _identity_decimal,
        )
        _wind_times, wind_path = _expand_grid_values(
            (properties.get("windSpeed") or {}).get("values", []),
            _mps_to_kt,
        )
        _precip_times, precipitation_path = _expand_grid_values(
            (properties.get("probabilityOfPrecipitation") or {}).get("values", []),
            _identity_decimal,
        )

        horizon = len(valid_times)
        cloud_cover_path_pct = (cloud_cover_path_pct + ((Decimal("50"),) * horizon))[:horizon]
        wind_path = (wind_path + ((Decimal("0"),) * horizon))[:horizon]
        precipitation_path = (precipitation_path + ((Decimal("0"),) * horizon))[:horizon]

        snapshot = ForecastSnapshot(
            provider_id="NWS_GRID",
            provider_run_time=_parse_iso(str(properties.get("updateTime"))),
            ingest_time=raw_payload.ingest_time,
            valid_for_times=valid_times,
            hourly_temp_path_f=hourly_temp_path_f,
            cloud_cover_path_pct=cloud_cover_path_pct,
            wind_path=wind_path,
            precipitation_path=precipitation_path,
            provider_metadata={
                "station_id": self.station.station_id,
                "grid_id": point_payload["properties"]["gridId"],
                "grid_x": point_payload["properties"]["gridX"],
                "grid_y": point_payload["properties"]["gridY"],
                "forecast_grid_url": raw_payload.source_endpoint,
            },
            source_payload_id=raw_payload.payload_hash,
        )
        return (
            NormalizedEnvelope(
                raw_payload_id=raw_payload.payload_hash,
                normalized_at=datetime.now(timezone.utc),
                record=snapshot,
            ),
        )


class KalshiOpenMarketsAdapter(IngestionAdapter[MarketSnapshot]):
    source_name = "kalshi_open_markets"
    parser_version = "kalshi_open_markets_v1"

    def __init__(self, client: KalshiPublicClient, series_ticker: str) -> None:
        self.client = client
        self.series_ticker = series_ticker

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        payload = self.client.list_markets(self.series_ticker, status="open", limit=200)
        text = json.dumps(payload, sort_keys=True)
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=f"{self.client.base_url}/markets",
            request_params={"series_ticker": self.series_ticker, "status": "open", "limit": 200},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=None,
            payload=text,
            metadata={"series_ticker": self.series_ticker},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[MarketSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        markets = payload.get("markets", [])
        normalized: list[NormalizedEnvelope[MarketSnapshot]] = []
        for market in markets:
            snapshot = market_snapshot_from_payload(market, raw_payload.payload_hash)
            normalized.append(
                NormalizedEnvelope(
                    raw_payload_id=raw_payload.payload_hash,
                    normalized_at=datetime.now(timezone.utc),
                    record=snapshot,
                )
            )
        return tuple(normalized)


class KalshiOrderbookAdapter(IngestionAdapter[OrderbookSnapshot]):
    source_name = "kalshi_orderbook"
    parser_version = "kalshi_orderbook_v1"

    def __init__(self, client: KalshiPublicClient, market_ticker: str) -> None:
        self.client = client
        self.market_ticker = market_ticker

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        payload = self.client.get_market_orderbook(self.market_ticker)
        text = json.dumps(payload, sort_keys=True)
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=f"{self.client.base_url}/markets/{self.market_ticker}/orderbook",
            request_params={"market_ticker": self.market_ticker},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=None,
            payload=text,
            metadata={"market_ticker": self.market_ticker},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[OrderbookSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        orderbook = payload.get("orderbook_fp", {})
        yes_bids = tuple(
            (Decimal(price), Decimal(size))
            for price, size in orderbook.get("yes_dollars", [])
        )
        no_bids = tuple(
            (Decimal(price), Decimal(size))
            for price, size in orderbook.get("no_dollars", [])
        )
        implied_yes_asks, implied_no_asks = derive_implied_ask_ladders(yes_bids, no_bids)
        snapshot = OrderbookSnapshot(
            market_ticker=self.market_ticker,
            as_of_time=raw_payload.ingest_time,
            seq=0,
            yes_bids_ladder=yes_bids,
            no_bids_ladder=no_bids,
            implied_yes_asks_ladder=implied_yes_asks,
            implied_no_asks_ladder=implied_no_asks,
            checksum_status="rest_snapshot",
            source_refs=(raw_payload.payload_hash,),
        )
        return (
            NormalizedEnvelope(
                raw_payload_id=raw_payload.payload_hash,
                normalized_at=datetime.now(timezone.utc),
                record=snapshot,
            ),
        )


class KalshiTradeAdapter(IngestionAdapter[TradeSnapshot]):
    source_name = "kalshi_trades"
    parser_version = "kalshi_trades_v1"

    def __init__(self, client: KalshiPublicClient, market_ticker: str, limit: int = 200) -> None:
        self.client = client
        self.market_ticker = market_ticker
        self.limit = limit

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        payload = self.client.list_trades(ticker=self.market_ticker, limit=self.limit)
        text = json.dumps(payload, sort_keys=True)
        event_time = None
        trades = payload.get("trades", [])
        if trades:
            created_time = trades[0].get("created_time")
            if created_time:
                event_time = _parse_iso(str(created_time))
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=f"{self.client.base_url}/markets/trades",
            request_params={"ticker": self.market_ticker, "limit": self.limit},
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=event_time,
            payload=text,
            metadata={"market_ticker": self.market_ticker},
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[TradeSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        trades = payload.get("trades", [])
        normalized: list[NormalizedEnvelope[TradeSnapshot]] = []
        for trade in trades:
            snapshot = trade_snapshot_from_payload(trade, raw_payload.payload_hash)
            normalized.append(
                NormalizedEnvelope(
                    raw_payload_id=raw_payload.payload_hash,
                    normalized_at=datetime.now(timezone.utc),
                    record=snapshot,
                )
            )
        return tuple(normalized)


# ============================================================================
# Open-Meteo multi-model ensemble adapter
# ============================================================================
# Pulls 9 independent NWP / AI models in a single API call:
#   NOAA GFS, NOAA HRRR, NOAA NBM, ECMWF IFS, ECMWF AIFS,
#   Google GraphCast, DWD ICON, JMA, ECCC GEM.
# Each model becomes a separate ForecastSnapshot.

def _parse_om_iso(value: str) -> datetime:
    # Open-Meteo returns "YYYY-MM-DDTHH:MM" in the requested local timezone.
    # We convert to UTC at the call site using the timezone offset metadata.
    return datetime.fromisoformat(value)


class OpenMeteoEnsembleAdapter(IngestionAdapter[ForecastSnapshot]):
    """Returns one ForecastSnapshot per model — 9 snapshots per fetch."""

    source_name = "open_meteo_ensemble"
    parser_version = "open_meteo_ensemble_v1"

    def __init__(
        self,
        client: OpenMeteoClient,
        station: StationReference,
        *,
        forecast_days: int = 3,
    ) -> None:
        self.client = client
        self.station = station
        self.forecast_days = forecast_days

    def fetch_raw(self) -> RawPayloadRecord:
        ingest_time = datetime.now(timezone.utc)
        payload = self.client.fetch_ensemble(
            latitude=float(self.station.latitude),
            longitude=float(self.station.longitude),
            timezone=self.station.timezone,
        )
        text = json.dumps(payload, sort_keys=True)
        # Open-Meteo doesn't return an explicit "produced at" timestamp;
        # use ingest_time as the closest reference for event_time.
        return RawPayloadRecord(
            source_name=self.source_name,
            source_endpoint=self.client.BASE_URL,
            request_params={
                "station_id": self.station.station_id,
                "lat": str(self.station.latitude),
                "lon": str(self.station.longitude),
            },
            transport_status=200,
            payload_hash=sha256(text.encode("utf-8")).hexdigest(),
            parser_version=self.parser_version,
            ingest_time=ingest_time,
            event_time=ingest_time,
            payload=text,
            metadata={
                "station_id": self.station.station_id,
                "model_count": len(OPEN_METEO_MODELS),
            },
        )

    def normalize(
        self, raw_payload: RawPayloadRecord
    ) -> tuple[NormalizedEnvelope[ForecastSnapshot], ...]:
        payload = json.loads(str(raw_payload.payload))
        hourly = payload.get("hourly") or {}
        time_strings: list[str] = hourly.get("time") or []
        if not time_strings:
            return ()

        # Local times from Open-Meteo come in the station's local zone (no tzinfo).
        # We attach the timezone supplied in the response so downstream code sees
        # tz-aware datetimes consistent with the rest of the pipeline.
        from zoneinfo import ZoneInfo  # local import keeps top of file lean
        tz_name = payload.get("timezone") or self.station.timezone
        tz = ZoneInfo(tz_name)
        valid_for_times = tuple(_parse_om_iso(ts).replace(tzinfo=tz) for ts in time_strings)

        snapshots: list[NormalizedEnvelope[ForecastSnapshot]] = []
        normalized_at = datetime.now(timezone.utc)

        for api_id, provider_id, _tier in OPEN_METEO_MODELS:
            temp_key = f"temperature_2m_{api_id}"
            cloud_key = f"cloud_cover_{api_id}"
            wind_key = f"wind_speed_10m_{api_id}"
            precip_key = f"precipitation_probability_{api_id}"

            temps_raw = hourly.get(temp_key)
            if not temps_raw:
                # This model wasn't available for this point — skip silently.
                continue

            # Drop trailing Nones (some AI models like GraphCast cap at 36-48h).
            # Align all four arrays to the same length as temperature (the primary signal).
            n = len(temps_raw)
            cloud_raw = (hourly.get(cloud_key) or [])[:n]
            wind_raw = (hourly.get(wind_key) or [])[:n]
            precip_raw = (hourly.get(precip_key) or [])[:n]

            # Filter out any indices where temperature is None (model didn't run for that hour).
            kept_indices = [i for i, t in enumerate(temps_raw) if t is not None]
            if not kept_indices:
                continue
            kept_times = tuple(valid_for_times[i] for i in kept_indices)
            kept_temps = tuple(Decimal(str(temps_raw[i])) for i in kept_indices)
            kept_clouds = tuple(
                Decimal(str(cloud_raw[i])) if i < len(cloud_raw) and cloud_raw[i] is not None else Decimal("50")
                for i in kept_indices
            )
            kept_winds = tuple(
                Decimal(str(wind_raw[i])) if i < len(wind_raw) and wind_raw[i] is not None else Decimal("0")
                for i in kept_indices
            )
            kept_precip = tuple(
                Decimal(str(precip_raw[i])) if i < len(precip_raw) and precip_raw[i] is not None else Decimal("0")
                for i in kept_indices
            )

            snapshot = ForecastSnapshot(
                provider_id=provider_id,
                provider_run_time=raw_payload.ingest_time,
                ingest_time=raw_payload.ingest_time,
                valid_for_times=kept_times,
                hourly_temp_path_f=kept_temps,
                cloud_cover_path_pct=kept_clouds,
                wind_path=kept_winds,
                precipitation_path=kept_precip,
                provider_metadata={
                    "station_id": self.station.station_id,
                    "open_meteo_api_id": api_id,
                    "tz": tz_name,
                    "hours_returned": len(kept_indices),
                },
                source_payload_id=raw_payload.payload_hash,
            )
            snapshots.append(
                NormalizedEnvelope(
                    raw_payload_id=raw_payload.payload_hash,
                    normalized_at=normalized_at,
                    record=snapshot,
                )
            )

        return tuple(snapshots)
