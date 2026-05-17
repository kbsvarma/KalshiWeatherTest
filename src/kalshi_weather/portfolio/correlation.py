from __future__ import annotations

from decimal import Decimal
from math import sqrt

from kalshi_weather.domain.models import ShadowPosition


CITY_REGION_PRIORS: dict[str, str] = {
    # Original 8
    "nyc": "northeast_metro",
    "phl": "northeast_corridor",
    "bos": "northeast_metro",
    "chi": "midwest_metro",
    "aus": "texas_inland",
    "den": "front_range",
    "mia": "florida_metro",
    "lax": "west_coast_metro",
    # Phase 1 expansion (2026-05-16): 5 more cities
    "phx": "southwest_desert",
    "lv":  "southwest_desert",
    "dal": "south_plains",
    "atl": "southeast_inland",
    "dc":  "mid_atlantic",
    # Phase 2 expansion (2026-05-16): 5 more cities
    "hou": "gulf_coast",
    "nola": "gulf_coast",
    "sat": "south_plains",        # shares cluster with DAL
    "okc": "south_plains",        # shares cluster with DAL/SAT
    "msp": "north_continental",
}

# Regimes where multiple cities tend to move together: heat domes, strong ridging,
# and broad synoptic anomalies raise cross-city correlations significantly.
_HIGH_CORRELATION_REGIMES = frozenset({
    "CLEAR_STABLE_HEATING",  # widespread high pressure — cities co-move with the ridge
})
_SHOCK_REGIMES = frozenset({
    "CONVECTIVE_SHOCK",
    "CLOUD_SUPPRESSION_RISK",
    "MARINE_INTRUSION",
})


def _pairwise_correlation(left_city: str, right_city: str) -> Decimal:
    if left_city == right_city:
        return Decimal("1")
    left_region = CITY_REGION_PRIORS.get(left_city, left_city)
    right_region = CITY_REGION_PRIORS.get(right_city, right_city)
    region_pair = frozenset((left_region, right_region))
    if left_region == right_region:
        return Decimal("0.65")
    if region_pair == frozenset(("northeast_metro", "northeast_corridor")):
        return Decimal("0.65")
    if region_pair in {
        frozenset(("northeast_metro", "midwest_metro")),
        frozenset(("northeast_corridor", "midwest_metro")),
    }:
        return Decimal("0.35")
    if region_pair == frozenset(("texas_inland", "front_range")):
        return Decimal("0.25")
    if "west_coast_metro" in region_pair and (
        "northeast_metro" in region_pair or "northeast_corridor" in region_pair
    ):
        return Decimal("0.10")
    if region_pair == frozenset(("west_coast_metro", "midwest_metro")):
        return Decimal("0.12")
    if region_pair == frozenset(("west_coast_metro", "front_range")):
        return Decimal("0.18")
    if "florida_metro" in region_pair and (
        "northeast_metro" in region_pair or "northeast_corridor" in region_pair
    ):
        return Decimal("0.20")
    if "florida_metro" in region_pair and "midwest_metro" in region_pair:
        return Decimal("0.18")

    # ── Phase 1 expansion region pairs ──────────────────────────────────────
    # Two desert cities (Phoenix + Las Vegas) — share same synoptic regime often.
    if region_pair == frozenset(("southwest_desert", "southwest_desert")):
        return Decimal("0.55")
    # Desert ↔ West Coast — partial overlap (Pacific ridging).
    if region_pair == frozenset(("southwest_desert", "west_coast_metro")):
        return Decimal("0.30")
    # Desert ↔ Front Range (Denver) — both interior west.
    if region_pair == frozenset(("southwest_desert", "front_range")):
        return Decimal("0.35")
    # Desert ↔ Texas inland — shared subtropical ridging in summer.
    if region_pair == frozenset(("southwest_desert", "texas_inland")):
        return Decimal("0.30")
    # Desert ↔ South plains (Dallas) — adjacent regimes.
    if region_pair == frozenset(("southwest_desert", "south_plains")):
        return Decimal("0.32")
    # South plains (DAL) ↔ Texas inland (AUS) — same airmass family.
    if region_pair == frozenset(("south_plains", "texas_inland")):
        return Decimal("0.60")
    # South plains ↔ Front Range (Denver) — Plains/Rockies frontal coupling.
    if region_pair == frozenset(("south_plains", "front_range")):
        return Decimal("0.32")
    # South plains ↔ Midwest — both downstream of Rockies.
    if region_pair == frozenset(("south_plains", "midwest_metro")):
        return Decimal("0.40")
    # Southeast inland (ATL) ↔ Florida (MIA) — shared subtropical regime in summer.
    if region_pair == frozenset(("southeast_inland", "florida_metro")):
        return Decimal("0.50")
    # Southeast inland ↔ Mid-Atlantic (DC) — eastern seaboard linkage.
    if region_pair == frozenset(("southeast_inland", "mid_atlantic")):
        return Decimal("0.45")
    # Southeast inland ↔ Northeast — frontal passages connect them.
    if region_pair in {
        frozenset(("southeast_inland", "northeast_metro")),
        frozenset(("southeast_inland", "northeast_corridor")),
    }:
        return Decimal("0.30")
    # Mid-Atlantic (DC) ↔ Northeast — same coastal climate, very correlated.
    if region_pair in {
        frozenset(("mid_atlantic", "northeast_metro")),
        frozenset(("mid_atlantic", "northeast_corridor")),
    }:
        return Decimal("0.65")
    # Mid-Atlantic ↔ Midwest — common Great Lakes / continental flow.
    if region_pair == frozenset(("mid_atlantic", "midwest_metro")):
        return Decimal("0.35")
    # Mid-Atlantic ↔ Florida — both eastern seaboard but distant.
    if region_pair == frozenset(("mid_atlantic", "florida_metro")):
        return Decimal("0.28")

    # ── Phase 2 expansion region pairs ──────────────────────────────────────
    # Two Gulf coast cities (Houston + New Orleans) — very strongly correlated.
    if region_pair == frozenset(("gulf_coast", "gulf_coast")):
        return Decimal("0.70")
    # Gulf coast ↔ Florida — shared subtropical Atlantic/Gulf regime.
    if region_pair == frozenset(("gulf_coast", "florida_metro")):
        return Decimal("0.50")
    # Gulf coast ↔ South plains (TX/OK) — adjacent regimes, strong link.
    if region_pair == frozenset(("gulf_coast", "south_plains")):
        return Decimal("0.55")
    # Gulf coast ↔ Texas inland (AUS) — adjacent and similar latitudes.
    if region_pair == frozenset(("gulf_coast", "texas_inland")):
        return Decimal("0.55")
    # Gulf coast ↔ Southeast inland (ATL) — both humid subtropical.
    if region_pair == frozenset(("gulf_coast", "southeast_inland")):
        return Decimal("0.45")
    # Gulf coast ↔ Mid-Atlantic — both eastern but distant latitudes.
    if region_pair == frozenset(("gulf_coast", "mid_atlantic")):
        return Decimal("0.25")
    # Gulf coast ↔ Northeast — distant, decoupled most of the year.
    if region_pair in {
        frozenset(("gulf_coast", "northeast_metro")),
        frozenset(("gulf_coast", "northeast_corridor")),
    }:
        return Decimal("0.15")
    # Gulf coast ↔ Midwest — partial linkage via Plains states.
    if region_pair == frozenset(("gulf_coast", "midwest_metro")):
        return Decimal("0.22")
    # Gulf coast ↔ Front Range — fairly decoupled.
    if region_pair == frozenset(("gulf_coast", "front_range")):
        return Decimal("0.18")
    # Gulf coast ↔ Desert — partial summer ridging overlap.
    if region_pair == frozenset(("gulf_coast", "southwest_desert")):
        return Decimal("0.22")
    # Gulf coast ↔ West coast — far apart, mostly decoupled.
    if region_pair == frozenset(("gulf_coast", "west_coast_metro")):
        return Decimal("0.12")
    # North continental (MSP) ↔ Midwest — same air mass family, strong link.
    if region_pair == frozenset(("north_continental", "midwest_metro")):
        return Decimal("0.60")
    # North continental ↔ Front Range — both downstream of Canadian air masses.
    if region_pair == frozenset(("north_continental", "front_range")):
        return Decimal("0.45")
    # North continental ↔ South plains — air masses connect via plains.
    if region_pair == frozenset(("north_continental", "south_plains")):
        return Decimal("0.40")
    # North continental ↔ Northeast — frontal passages connect them.
    if region_pair in {
        frozenset(("north_continental", "northeast_metro")),
        frozenset(("north_continental", "northeast_corridor")),
    }:
        return Decimal("0.40")
    # North continental ↔ Mid-Atlantic — same continental airmass behavior.
    if region_pair == frozenset(("north_continental", "mid_atlantic")):
        return Decimal("0.38")
    # North continental ↔ Southeast — air masses connect via mid-continent.
    if region_pair == frozenset(("north_continental", "southeast_inland")):
        return Decimal("0.30")
    # North continental ↔ Gulf coast — distant, frontal passages occasionally couple.
    if region_pair == frozenset(("north_continental", "gulf_coast")):
        return Decimal("0.22")
    # North continental ↔ Desert / West coast — mostly decoupled.
    if region_pair in {
        frozenset(("north_continental", "southwest_desert")),
        frozenset(("north_continental", "west_coast_metro")),
        frozenset(("north_continental", "florida_metro")),
    }:
        return Decimal("0.15")
    return Decimal("0.22")


def _regime_correlation_multiplier(
    left_regime: str | None,
    right_regime: str | None,
) -> Decimal:
    """Adjust correlation upward when both cities share a common synoptic driver.

    During widespread ridging both cities heat together (multiplier > 1).
    During independent local shocks (convection, marine layer) they decouple
    from each other so the base correlation is more reliable (multiplier = 1).
    """
    if not left_regime or not right_regime:
        return Decimal("1")
    both_ridge = left_regime in _HIGH_CORRELATION_REGIMES and right_regime in _HIGH_CORRELATION_REGIMES
    if both_ridge:
        # Widespread clear-sky ridging: cross-city correlations spike.
        return Decimal("1.40")
    either_shock = left_regime in _SHOCK_REGIMES or right_regime in _SHOCK_REGIMES
    if either_shock:
        # At least one city has a local shock — decouples somewhat from the other.
        return Decimal("0.85")
    return Decimal("1")


def build_static_correlation_matrix(city_ids: list[str]) -> dict[tuple[str, str], Decimal]:
    matrix: dict[tuple[str, str], Decimal] = {}
    for left in city_ids:
        for right in city_ids:
            matrix[(left, right)] = _pairwise_correlation(left, right)
    return matrix


def build_regime_adjusted_correlation_matrix(
    city_ids: list[str],
    regime_by_city: dict[str, str] | None = None,
) -> dict[tuple[str, str], Decimal]:
    """Return a correlation matrix with regime-based multipliers applied."""
    matrix: dict[tuple[str, str], Decimal] = {}
    regimes = regime_by_city or {}
    for left in city_ids:
        for right in city_ids:
            base = _pairwise_correlation(left, right)
            if left == right:
                matrix[(left, right)] = base
                continue
            multiplier = _regime_correlation_multiplier(regimes.get(left), regimes.get(right))
            # Cap at 0.90 to avoid asserting near-perfect correlation.
            matrix[(left, right)] = min(Decimal("0.90"), base * multiplier)
    return matrix


def portfolio_risk_units(
    positions: list[tuple[str, ShadowPosition]],
    correlation_matrix: dict[tuple[str, str], Decimal],
) -> Decimal:
    if not positions:
        return Decimal("0")
    total = Decimal("0")
    for left_city, left_position in positions:
        for right_city, right_position in positions:
            corr = correlation_matrix.get((left_city, right_city), Decimal("0"))
            total += (
                left_position.open_quantity_fp
                * right_position.open_quantity_fp
                * corr
            )
    return Decimal(str(sqrt(float(total))))
