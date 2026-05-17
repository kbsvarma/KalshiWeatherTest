"""SPC (Storm Prediction Center) day-1 convective outlook client.

The SPC issues categorical convective risk outlooks several times per day,
published as GeoJSON polygons. Categories: TSTM, MRGL, SLGT, ENH, MDT, HIGH.
When a city's lat/lon falls inside an ENH+ polygon, our forecast confidence
should drop — convection is non-linear and our daily-max prediction is
unreliable.

Endpoint:
  https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson
"""
from __future__ import annotations

from dataclasses import dataclass

from .http import http_get_json


SPC_DAY1_GEOJSON_URL = (
    "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson"
)

# Severity ranks for comparison: higher = more dangerous
_RISK_RANK = {
    "TSTM": 1,
    "MRGL": 2,
    "SLGT": 3,
    "ENH": 4,
    "MDT": 5,
    "HIGH": 6,
}


@dataclass(frozen=True, slots=True)
class SpcOutlookForCity:
    """SPC categorical risk at a single lat/lon, or None if outside any polygon."""

    category: str | None  # e.g. "ENH", "SLGT", or None
    rank: int  # 0 if no risk
    fetched_ok: bool


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray-cast point-in-polygon for a single ring of (lon, lat) coords."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lon: float, lat: float, coordinates: list) -> bool:
    """coordinates per GeoJSON Polygon: [outer_ring, hole1, hole2, ...]."""
    if not coordinates:
        return False
    if not _point_in_ring(lon, lat, coordinates[0]):
        return False
    # If inside outer ring, check it's not in any hole
    for hole in coordinates[1:]:
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def fetch_spc_outlook_for_cities(
    cities: dict[str, tuple[float, float]],
) -> dict[str, SpcOutlookForCity]:
    """Return per-city SPC categorical risk for today.

    Args:
      cities: mapping city_id -> (lat, lon)

    Result is always populated for every input city. On fetch failure, all
    cities return SpcOutlookForCity(category=None, rank=0, fetched_ok=False).
    """
    try:
        geojson = http_get_json(SPC_DAY1_GEOJSON_URL)
    except Exception:
        return {
            city_id: SpcOutlookForCity(category=None, rank=0, fetched_ok=False)
            for city_id in cities
        }

    features = geojson.get("features") if isinstance(geojson, dict) else []
    if not isinstance(features, list):
        features = []

    out: dict[str, SpcOutlookForCity] = {}
    for city_id, (lat, lon) in cities.items():
        best_rank = 0
        best_category: str | None = None
        for feature in features:
            geom = feature.get("geometry") if isinstance(feature, dict) else None
            if not isinstance(geom, dict):
                continue
            label = (
                (feature.get("properties") or {}).get("LABEL")
                or (feature.get("properties") or {}).get("DN")
                or ""
            )
            rank = _RISK_RANK.get(str(label).upper().strip(), 0)
            if rank <= best_rank:
                continue
            gtype = geom.get("type")
            coords = geom.get("coordinates")
            if gtype == "Polygon" and isinstance(coords, list):
                if _point_in_polygon(lon, lat, coords):
                    best_rank = rank
                    best_category = str(label).upper().strip()
            elif gtype == "MultiPolygon" and isinstance(coords, list):
                for poly in coords:
                    if _point_in_polygon(lon, lat, poly):
                        best_rank = rank
                        best_category = str(label).upper().strip()
                        break
        out[city_id] = SpcOutlookForCity(
            category=best_category,
            rank=best_rank,
            fetched_ok=True,
        )
    return out
