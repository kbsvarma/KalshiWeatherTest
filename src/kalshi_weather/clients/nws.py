from __future__ import annotations

from html import unescape
import json
from collections.abc import Mapping
from datetime import datetime
import re
from typing import Any

from .http import http_get_json, http_get_text


class NwsClimateClientError(RuntimeError):
    """Raised when climate text products cannot be fetched."""


class NceiDailySummariesClientError(RuntimeError):
    """Raised when NCEI daily summaries cannot be fetched."""


class NwsClimateClient:
    BASE_URL = "https://forecast.weather.gov/product.php"
    API_BASE_URL = "https://api.weather.gov"
    PRODUCT_PRE_RE = re.compile(
        r"<pre[^>]*class=[\"']glossaryProduct[\"'][^>]*>(.*?)</pre>",
        re.IGNORECASE | re.DOTALL,
    )
    ANY_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.IGNORECASE | re.DOTALL)

    @classmethod
    def _extract_cli_text(cls, payload: str) -> str:
        if "<html" not in payload.lower():
            return payload
        match = cls.PRODUCT_PRE_RE.search(payload) or cls.ANY_PRE_RE.search(payload)
        if not match:
            raise NwsClimateClientError("weather.gov product page did not contain CLI text")
        return unescape(match.group(1)).strip()

    def fetch_cli_text(
        self,
        site: str,
        issuedby: str,
        version: int = 1,
        glossary: int = 0,
    ) -> str:
        payload = http_get_text(
            self.BASE_URL,
            params={
                "site": site,
                "issuedby": issuedby,
                "product": "CLI",
                "format": "TXT",
                "version": version,
                "glossary": glossary,
            },
        )
        return self._extract_cli_text(payload)

    def list_products(
        self,
        *,
        product_type: str,
        office: str | None = None,
        location: str | None = None,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"type": product_type, "limit": limit}
        if office:
            params["office"] = office
        if location:
            params["location"] = location
        return http_get_json(f"{self.API_BASE_URL}/products", params=params)

    def get_product(self, product_id: str) -> Mapping[str, Any]:
        return http_get_json(f"{self.API_BASE_URL}/products/{product_id}")


class NceiDailySummariesClient:
    BASE_URL = "https://www.ncei.noaa.gov/access/services/data/v1"

    def fetch_daily_summaries(
        self,
        *,
        station_id: str,
        start_date: str,
        end_date: str,
        units: str = "standard",
    ) -> list[Mapping[str, Any]]:
        text = http_get_text(
            self.BASE_URL,
            params={
                "dataset": "daily-summaries",
                "stations": station_id,
                "startDate": start_date,
                "endDate": end_date,
                "format": "json",
                "units": units,
            },
        )
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise NceiDailySummariesClientError("expected list response from NCEI daily summaries")
        return [item for item in payload if isinstance(item, Mapping)]


class NwsWeatherClient:
    BASE_URL = "https://api.weather.gov"

    def get_latest_observation(self, station_id: str) -> Mapping[str, Any]:
        return http_get_json(f"{self.BASE_URL}/stations/{station_id}/observations/latest")

    def get_recent_observations(
        self, station_id: str, limit: int = 4
    ) -> Mapping[str, Any]:
        return http_get_json(
            f"{self.BASE_URL}/stations/{station_id}/observations",
            params={"limit": limit},
        )

    def get_observations(
        self,
        station_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if start is not None:
            params["start"] = start.isoformat().replace("+00:00", "Z")
        if end is not None:
            params["end"] = end.isoformat().replace("+00:00", "Z")
        return http_get_json(
            f"{self.BASE_URL}/stations/{station_id}/observations",
            params=params,
        )

    def get_point_metadata(self, latitude: str, longitude: str) -> Mapping[str, Any]:
        return http_get_json(f"{self.BASE_URL}/points/{latitude},{longitude}")

    def get_hourly_forecast(self, forecast_hourly_url: str) -> Mapping[str, Any]:
        return http_get_json(forecast_hourly_url)

    def get_gridpoint_forecast_data(self, forecast_grid_url: str) -> Mapping[str, Any]:
        return http_get_json(forecast_grid_url)
