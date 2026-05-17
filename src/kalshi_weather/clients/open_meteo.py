"""Open-Meteo client — multi-model NWP ensemble in a single API call.

Open-Meteo aggregates forecasts from ~10 independent numerical weather prediction
models (NOAA GFS, NOAA HRRR, NOAA NBM, ECMWF IFS, ECMWF AIFS, Google GraphCast,
DWD ICON, JMA, ECCC GEM) under a single REST endpoint. This client fetches all
models for a city in one HTTP call.

API docs: https://open-meteo.com/en/docs

No authentication required for the free tier. Limit is ~10,000 calls/day per IP,
well above our usage (8 cities × ~24 cycles/day = ~200 calls/day).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .http import http_get_json


# ----------------------------------------------------------------------------
# Model registry — verified working as of 2026-05-16
# ----------------------------------------------------------------------------
# Each entry: (open_meteo_model_id, internal_provider_id, support_tier)
#   support_tier: "primary" (use full weight), "secondary" (downweight slightly)
#
# Primary models (independent physics or AI):
#   gfs_global          — NOAA GFS 13km global (US base model)
#   gfs_hrrr            — NOAA HRRR 3km mesoscale (best <12h same-day)
#   ncep_nbm_conus      — NOAA National Blend of Models (official US blend)
#   ecmwf_ifs025        — ECMWF IFS 0.25° (global gold standard 24-72h)
#   ecmwf_aifs025_single— ECMWF AIFS (AI model from ECMWF)
#   gfs_graphcast025    — Google DeepMind GraphCast (AI, beats some NWP)
#   icon_seamless       — DWD ICON (German Weather Service)
#   jma_seamless        — Japan Meteorological Agency
#   gem_seamless        — Environment Canada GEM
#
# We deliberately exclude ncep_gfs025 (often nulls), best_match (a derived blend,
# overlaps with our own fusion math), and metno_nordic (Europe-only).

OPEN_METEO_MODELS = (
    ("gfs_global", "OPEN_METEO_GFS", "primary"),
    ("gfs_hrrr", "OPEN_METEO_HRRR", "primary"),
    ("ncep_nbm_conus", "OPEN_METEO_NBM", "primary"),
    ("ecmwf_ifs025", "OPEN_METEO_ECMWF_IFS", "primary"),
    ("ecmwf_aifs025_single", "OPEN_METEO_ECMWF_AIFS", "primary"),
    ("gfs_graphcast025", "OPEN_METEO_GRAPHCAST", "primary"),
    ("icon_seamless", "OPEN_METEO_ICON", "primary"),
    ("jma_seamless", "OPEN_METEO_JMA", "secondary"),
    ("gem_seamless", "OPEN_METEO_GEM", "secondary"),
)

_PROVIDER_BY_API_ID: dict[str, str] = {api_id: provider_id for api_id, provider_id, _ in OPEN_METEO_MODELS}
_TIER_BY_PROVIDER: dict[str, str] = {provider_id: tier for _, provider_id, tier in OPEN_METEO_MODELS}


def provider_ids() -> tuple[str, ...]:
    return tuple(provider_id for _, provider_id, _ in OPEN_METEO_MODELS)


def support_tier(provider_id: str) -> str:
    return _TIER_BY_PROVIDER.get(provider_id, "unsupported")


def provider_for_api_id(api_id: str) -> str | None:
    return _PROVIDER_BY_API_ID.get(api_id)


class OpenMeteoClientError(RuntimeError):
    """Raised when Open-Meteo returns an unexpected payload."""


class OpenMeteoClient:
    """REST client for the Open-Meteo multi-model forecast API."""

    BASE_URL = "https://api.open-meteo.com/v1/forecast"
    HOURLY_VARS = (
        "temperature_2m",
        "cloud_cover",
        "wind_speed_10m",
        "precipitation_probability",
    )

    def fetch_soil_moisture(
        self, *, latitude: float, longitude: float
    ) -> dict | None:
        """Return today's near-surface soil moisture context for a station.

        Uses the single-model (default-blended) forecast endpoint to fetch
        the 0-7cm and 7-28cm soil moisture (m³/m³) and surface temperature.
        Returns a small dict with current_value and 24h-mean, or None on
        any failure — caller must gracefully degrade. Free API, no auth.

        Soil moisture is a physics-based predictor of daytime high: dry
        soil → less latent cooling → hotter highs in summer.
        """
        import statistics
        from .http import http_get_json
        params = {
            "latitude": str(latitude),
            "longitude": str(longitude),
            "hourly": "soil_moisture_0_to_1cm,soil_moisture_1_to_3cm,soil_moisture_3_to_9cm",
            "forecast_days": "1",
            "timezone": "UTC",
        }
        try:
            payload = http_get_json("https://api.open-meteo.com/v1/forecast", params=params)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        hourly = payload.get("hourly") or {}
        # Pick the shallowest layer that actually returned values
        for var_name in (
            "soil_moisture_0_to_1cm",
            "soil_moisture_1_to_3cm",
            "soil_moisture_3_to_9cm",
        ):
            values = hourly.get(var_name)
            if isinstance(values, list) and values:
                # Filter Nones
                clean = [v for v in values if isinstance(v, (int, float))]
                if not clean:
                    continue
                return {
                    "variable": var_name,
                    "current_value": float(clean[0]),
                    "mean_24h": float(statistics.mean(clean)),
                    "min_24h": float(min(clean)),
                    "max_24h": float(max(clean)),
                }
        return None

    def __init__(self, *, models: tuple[str, ...] | None = None, forecast_days: int = 3) -> None:
        # Use API model identifiers (e.g. "gfs_global"), not our internal provider ids.
        self.models = tuple(models) if models else tuple(api_id for api_id, _, _ in OPEN_METEO_MODELS)
        self.forecast_days = max(1, min(7, forecast_days))

    def fetch_ensemble(
        self,
        *,
        latitude: float,
        longitude: float,
        timezone: str,
    ) -> Mapping[str, Any]:
        """Fetch a multi-model ensemble forecast for a single point.

        Returns the raw JSON payload. The payload has one hourly array per
        (variable, model) combination, e.g. ``hourly.temperature_2m_gfs_global``.
        """
        params: dict[str, Any] = {
            "latitude": f"{latitude}",
            "longitude": f"{longitude}",
            "timezone": timezone,
            "hourly": ",".join(self.HOURLY_VARS),
            "models": ",".join(self.models),
            "forecast_days": str(self.forecast_days),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "kn",
            "precipitation_unit": "mm",
        }
        payload = http_get_json(self.BASE_URL, params=params)
        if not isinstance(payload, Mapping):
            raise OpenMeteoClientError("open-meteo returned non-object payload")
        if "hourly" not in payload:
            reason = payload.get("reason", "missing hourly block")
            raise OpenMeteoClientError(f"open-meteo response invalid: {reason}")
        return payload
