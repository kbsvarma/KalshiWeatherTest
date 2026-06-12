from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import ceil, exp, sqrt
from typing import Mapping

from kalshi_weather.domain.models import CityProfile, ForecastDistribution, ForecastSnapshot, SettlementRule


def _decimal(value: float) -> Decimal:
    return Decimal(f"{value:.6f}")


@dataclass(frozen=True, slots=True)
class ForecastEngineResult:
    distribution: ForecastDistribution
    provider_maxima_f: dict[str, Decimal]
    provider_sigmas_f: dict[str, Decimal]
    provider_weights: dict[str, Decimal]
    provider_support_status: dict[str, str]
    provider_spread_f: Decimal
    provider_threshold_straddle_f: Decimal
    provider_bias_adjustments_f: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class ForecastProviderSpec:
    lead_bucket_weights: tuple[Decimal, Decimal, Decimal]
    base_sigma_f: Decimal
    support_status: str


# Provider lead-bucket weights are (>12h weight, 6-12h weight, <6h weight).
# These are PRIOR weights — actual empirical weights from `forecast_calibration.py`
# override these per (provider, season, lead_bucket) once enough samples exist.
#
# Base sigma values reflect the model's intrinsic uncertainty (operational
# meteorology consensus):
#   - HRRR / NBM     ~1.8°F  (best for <12h same-day)
#   - GFS / ECMWF    ~2.0°F  (global gold standard for 24-72h)
#   - ICON / GEM     ~2.1°F  (good regional sharpness)
#   - JMA            ~2.3°F  (US-coverage secondary)
#   - GraphCast/AIFS ~2.5°F  (AI models, less validated on extreme tails)
#   - NWS            ~2.2°F  (blended derivative of GFS/NAM)

SUPPORTED_PROVIDER_SPECS = {
    # ── Existing NWS providers (kept) ───────────────────────────────────────
    "NWS": ForecastProviderSpec(
        lead_bucket_weights=(Decimal("0.35"), Decimal("0.35"), Decimal("0.25")),
        base_sigma_f=Decimal("2.2"),
        support_status="mvp_supported",
    ),
    "NWS_GRID": ForecastProviderSpec(
        lead_bucket_weights=(Decimal("0.30"), Decimal("0.40"), Decimal("0.35")),
        base_sigma_f=Decimal("2.0"),
        support_status="mvp_supported",
    ),
    # ── NOAA models via Open-Meteo ──────────────────────────────────────────
    "OPEN_METEO_GFS": ForecastProviderSpec(
        lead_bucket_weights=(Decimal("0.35"), Decimal("0.30"), Decimal("0.20")),
        base_sigma_f=Decimal("2.0"),
        support_status="mvp_supported",
    ),
    "OPEN_METEO_HRRR": ForecastProviderSpec(
        # HRRR's strength is <12h. Heavier weight there, lighter beyond 12h.
        lead_bucket_weights=(Decimal("0.20"), Decimal("0.40"), Decimal("0.45")),
        base_sigma_f=Decimal("1.8"),
        support_status="mvp_supported",
    ),
    "OPEN_METEO_NBM": ForecastProviderSpec(
        # NBM is NOAA's official multi-model blend — sharp at all leads.
        lead_bucket_weights=(Decimal("0.40"), Decimal("0.40"), Decimal("0.35")),
        base_sigma_f=Decimal("1.8"),
        support_status="mvp_supported",
    ),
    # ── ECMWF models ────────────────────────────────────────────────────────
    "OPEN_METEO_ECMWF_IFS": ForecastProviderSpec(
        # Global gold standard for 24-72h; downweight slightly <6h.
        lead_bucket_weights=(Decimal("0.45"), Decimal("0.40"), Decimal("0.25")),
        base_sigma_f=Decimal("2.0"),
        support_status="mvp_supported",
    ),
    "OPEN_METEO_ECMWF_AIFS": ForecastProviderSpec(
        # ECMWF's AI model. Competitive at medium range; less validated at <6h.
        lead_bucket_weights=(Decimal("0.30"), Decimal("0.30"), Decimal("0.20")),
        base_sigma_f=Decimal("2.5"),
        support_status="mvp_supported",
    ),
    # ── Other AI / global models ────────────────────────────────────────────
    "OPEN_METEO_GRAPHCAST": ForecastProviderSpec(
        # Google DeepMind GraphCast. Strong at 24-120h, weaker near-term.
        lead_bucket_weights=(Decimal("0.30"), Decimal("0.25"), Decimal("0.15")),
        base_sigma_f=Decimal("2.5"),
        support_status="mvp_supported",
    ),
    "OPEN_METEO_ICON": ForecastProviderSpec(
        # German Weather Service ICON.
        lead_bucket_weights=(Decimal("0.30"), Decimal("0.30"), Decimal("0.25")),
        base_sigma_f=Decimal("2.1"),
        support_status="mvp_supported",
    ),
    "OPEN_METEO_JMA": ForecastProviderSpec(
        # Japan Meteorological Agency — secondary US coverage.
        lead_bucket_weights=(Decimal("0.20"), Decimal("0.20"), Decimal("0.15")),
        base_sigma_f=Decimal("2.3"),
        support_status="mvp_supported",
    ),
    "OPEN_METEO_GEM": ForecastProviderSpec(
        # Environment Canada GEM — strong for northern US.
        lead_bucket_weights=(Decimal("0.30"), Decimal("0.30"), Decimal("0.25")),
        base_sigma_f=Decimal("2.1"),
        support_status="mvp_supported",
    ),
}
UNSUPPORTED_FALLBACK_SPEC = ForecastProviderSpec(
    lead_bucket_weights=(Decimal("0.20"), Decimal("0.20"), Decimal("0.20")),
    base_sigma_f=Decimal("2.8"),
    support_status="unsupported_fallback",
)


def _provider_spec(provider_id: str) -> ForecastProviderSpec:
    return SUPPORTED_PROVIDER_SPECS.get(provider_id, UNSUPPORTED_FALLBACK_SPEC)


def _lead_bucket_weight(provider_id: str, lead_hours: float) -> Decimal:
    more_than_12, six_to_12, less_than_6 = _provider_spec(provider_id).lead_bucket_weights
    if lead_hours > 12:
        return more_than_12
    if lead_hours >= 6:
        return six_to_12
    return less_than_6


def _season_key(as_of_time: datetime) -> str:
    month = as_of_time.month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _heating_window_values(
    path: tuple[Decimal, ...],
    valid_times: tuple[datetime, ...],
    window_start: datetime,
    window_end: datetime,
) -> tuple[Decimal, ...]:
    """Return path values whose valid_for_times fall inside the heating window."""
    if not path or not valid_times:
        return path
    result = tuple(
        v for v, t in zip(path, valid_times, strict=False)
        if window_start <= t <= window_end
    )
    return result if result else path


def _provider_bias_adjustment(
    snapshot: ForecastSnapshot,
    city_profile: CityProfile,
    settlement_rule: SettlementRule,
    as_of_time: datetime,
    provider_max: Decimal,
    empirical_bias_adjustment: Decimal | None = None,
) -> Decimal:
    # Use heating-window hours (not just the first forecast hour) for cloud/wind/precip.
    # Daily maximum temperature is determined by daytime conditions, not morning conditions.
    window_start = settlement_rule.local_standard_window_start
    window_end = settlement_rule.local_standard_window_end
    valid_times = snapshot.valid_for_times if snapshot.valid_for_times else ()

    cloud_path = _heating_window_values(snapshot.cloud_cover_path_pct, valid_times, window_start, window_end)
    wind_path = _heating_window_values(snapshot.wind_path, valid_times, window_start, window_end)
    precip_path = _heating_window_values(snapshot.precipitation_path, valid_times, window_start, window_end)

    # Peak cloud cover and max precipitation during heating window matter most for daily max.
    cloud = max(cloud_path) if cloud_path else Decimal("50")
    wind = max(wind_path) if wind_path else Decimal("0")
    precipitation = max(precip_path) if precip_path else Decimal("0")
    mean_cloud = sum(cloud_path) / Decimal(len(cloud_path)) if cloud_path else Decimal("50")

    adjustment = Decimal("0")
    # Cloud suppression: use peak cloud to catch afternoon build-up.
    # Scale continuously: mild penalty starts at 50%, full at 90%.
    if cloud >= Decimal("90"):
        adjustment -= Decimal("0.8")
    elif cloud >= Decimal("70"):
        adjustment -= Decimal("0.5")
    elif cloud >= Decimal("50"):
        adjustment -= Decimal("0.2")

    if city_profile.marine_sensitive_flag and wind >= Decimal("12"):
        adjustment -= Decimal("0.4")
    if precipitation >= Decimal("70"):
        adjustment -= Decimal("0.5")
    elif precipitation >= Decimal("50"):
        adjustment -= Decimal("0.3")
    elif precipitation >= Decimal("30"):
        adjustment -= Decimal("0.1")
    # Clear-sky bonus: require both low mean cloud AND low peak across entire heating window.
    # Extend to SON (Sep-Nov) since clear fall afternoons also drive above-average highs.
    if precipitation == 0 and mean_cloud <= Decimal("20") and cloud <= Decimal("25") and as_of_time.month in (5, 6, 7, 8, 9):
        adjustment += Decimal("0.2")
    threshold = settlement_rule.threshold_f
    if threshold is not None and abs(provider_max - threshold) <= Decimal("2"):
        adjustment *= Decimal("0.5")
    if empirical_bias_adjustment is not None:
        adjustment -= empirical_bias_adjustment
    return adjustment


def _provider_sigma(
    lead_hours: float,
    provider_id: str,
    boundary_distance: Decimal | None = None,
    reliability_weight: Decimal | None = None,
) -> Decimal:
    base = _provider_spec(provider_id).base_sigma_f
    if reliability_weight is not None:
        reliability_adjustment = (Decimal("0.55") - reliability_weight) * Decimal("1.2")
        base += reliability_adjustment
    if boundary_distance is not None and boundary_distance <= Decimal("2"):
        base += Decimal("0.35")
    if lead_hours < 6:
        return max(Decimal("1.2"), base - Decimal("0.5"))
    if lead_hours > 12:
        return base + Decimal("0.5")
    return base


def _threshold_skew_sigmas(
    sigma: float,
    threshold_distance_f: float | None,
) -> tuple[float, float]:
    if threshold_distance_f is None:
        return sigma, sigma
    proximity = max(0.0, 1.0 - min(abs(threshold_distance_f), 6.0) / 6.0)
    skew_strength = 0.20 * proximity
    warm_tail_wider = threshold_distance_f < 0
    if warm_tail_wider:
        sigma_down = sigma * max(0.65, 1.0 - (0.50 * skew_strength))
        sigma_up = sigma * (1.0 + skew_strength)
    else:
        sigma_down = sigma * (1.0 + skew_strength)
        sigma_up = sigma * max(0.65, 1.0 - (0.50 * skew_strength))
    return sigma_down, sigma_up


def _asymmetric_gaussian_pmf(
    mean: float,
    sigma_down: float,
    sigma_up: float,
    support: tuple[int, ...],
) -> tuple[Decimal, ...]:
    weights: list[float] = []
    for temp in support:
        sigma = sigma_up if temp >= mean else sigma_down
        z = (temp - mean) / max(sigma, 0.1)
        weights.append(exp(-0.5 * z * z))
    total = sum(weights) or 1.0
    return tuple(_decimal(weight / total) for weight in weights)


def build_forecast_distribution(
    snapshots: list[ForecastSnapshot],
    settlement_rule: SettlementRule,
    city_profile: CityProfile,
    as_of_time: datetime,
    provider_reliability: Mapping[str, Decimal] | None = None,
    provider_bias_adjustments: Mapping[str, Decimal] | None = None,
) -> ForecastEngineResult:
    if not snapshots:
        raise ValueError("at least one forecast snapshot is required")

    provider_pmfs: list[tuple[Decimal, tuple[Decimal, ...]]] = []
    provider_maxima_f: dict[str, Decimal] = {}
    provider_sigmas_f: dict[str, Decimal] = {}
    provider_weights: dict[str, Decimal] = {}
    provider_support_status: dict[str, str] = {}

    support_anchor = []
    lead_hours = max(
        0.0,
        (
            (settlement_rule.local_standard_window_end - as_of_time).total_seconds()
            / 3600.0
        ),
    )
    # 2026-05-17: select max vs min based on settlement_variable so LOW
    # markets use the day's minimum rather than maximum. The rule parser
    # already sets settlement_variable correctly; this was the only spot
    # downstream that ignored it.
    is_low_market = (
        getattr(settlement_rule, "settlement_variable", None)
        == "daily_low_temperature_f"
    )
    # 2026-05-24 bugfix: pre-filter snapshots to those whose forecast
    # actually covers the settlement window. Prior behavior fell back to
    # `list(snapshot.hourly_temp_path_f)` (the full 3-day path) when no
    # in-window hours existed, silently producing garbage values — for
    # Open-Meteo's rolling 3-day forecasts, as soon as the target day
    # rolled off the back, the fallback took max() over irrelevant future
    # days. The 7-day MAE analysis showed OM providers jumping from ~2°F
    # (in-window) to ~8°F (post-window), an artifact of this fallback.
    # GraphCast notably has a 48-72h horizon and routinely fails to
    # cover next-day markets — pre-this-fix, it contributed garbage; now
    # it correctly drops out. Three subsequent loops over `snapshots` all
    # index `provider_maxima_f[snapshot.provider_id]`, so filtering once
    # here is the only correct place — looping with `continue` would
    # KeyError downstream.
    filtered_snapshots: list = []
    for snapshot in snapshots:
        has_window = any(
            settlement_rule.local_standard_window_start <= valid_for <= settlement_rule.local_standard_window_end
            for valid_for in snapshot.valid_for_times
        )
        if has_window:
            filtered_snapshots.append(snapshot)
        else:
            print(f"[FORECAST] skipping {snapshot.provider_id}: "
                  f"forecast does not cover settlement window "
                  f"[{settlement_rule.local_standard_window_start} → "
                  f"{settlement_rule.local_standard_window_end}]")
    if not filtered_snapshots:
        raise ValueError(
            "no provider forecast covers the settlement window "
            f"[{settlement_rule.local_standard_window_start} → "
            f"{settlement_rule.local_standard_window_end}] — "
            "this market may have already settled or the forecast cache is stale"
        )
    snapshots = filtered_snapshots

    for snapshot in snapshots:
        window_values = [
            temp
            for temp, valid_for in zip(snapshot.hourly_temp_path_f, snapshot.valid_for_times, strict=False)
            if settlement_rule.local_standard_window_start <= valid_for <= settlement_rule.local_standard_window_end
        ]
        provider_max = min(window_values) if is_low_market else max(window_values)
        provider_max += _provider_bias_adjustment(
            snapshot,
            city_profile,
            settlement_rule,
            as_of_time,
            provider_max,
            empirical_bias_adjustment=(
                provider_bias_adjustments.get(snapshot.provider_id)
                if provider_bias_adjustments is not None
                else None
            ),
        )
        provider_maxima_f[snapshot.provider_id] = provider_max
        support_anchor.append(float(provider_max))

    support_sigmas = []
    for snapshot in snapshots:
        reliability_weight = (
            provider_reliability.get(snapshot.provider_id)
            if provider_reliability is not None
            else None
        )
        boundary_distance = (
            abs(provider_maxima_f[snapshot.provider_id] - settlement_rule.threshold_f)
            if settlement_rule.threshold_f is not None
            else None
        )
        support_sigmas.append(
            _provider_sigma(
                lead_hours,
                snapshot.provider_id,
                boundary_distance=boundary_distance,
                reliability_weight=reliability_weight,
            )
        )
    mean_support_sigma = (
        sum(support_sigmas) / Decimal(len(support_sigmas))
        if support_sigmas
        else Decimal("2.0")
    )
    support_buffer = max(6, int(ceil(float(mean_support_sigma * Decimal("3")))))
    min_support = int(min(support_anchor)) - support_buffer
    max_support = int(max(support_anchor)) + support_buffer
    support = tuple(range(min_support, max_support + 1))

    for snapshot in snapshots:
        reliability_weight = (
            provider_reliability.get(snapshot.provider_id)
            if provider_reliability is not None
            else None
        )
        boundary_distance = (
            abs(provider_maxima_f[snapshot.provider_id] - settlement_rule.threshold_f)
            if settlement_rule.threshold_f is not None
            else None
        )
        sigma = _provider_sigma(
            lead_hours,
            snapshot.provider_id,
            boundary_distance=boundary_distance,
            reliability_weight=reliability_weight,
        )
        provider_sigmas_f[snapshot.provider_id] = sigma
        provider_spec = _provider_spec(snapshot.provider_id)
        provider_support_status[snapshot.provider_id] = provider_spec.support_status
        provider_weight = _lead_bucket_weight(snapshot.provider_id, lead_hours)
        if reliability_weight is not None:
            provider_weight *= reliability_weight
        if provider_spec.support_status != "mvp_supported":
            provider_weight *= Decimal("0.50")
        provider_weights[snapshot.provider_id] = provider_weight
        threshold_distance = (
            float(provider_maxima_f[snapshot.provider_id] - settlement_rule.threshold_f)
            if settlement_rule.threshold_f is not None
            else None
        )
        sigma_down, sigma_up = _threshold_skew_sigmas(float(sigma), threshold_distance)
        # 2026-05-26 Fix A — overconfidence compression.
        # Audit of 44 settled less-yes bets showed p_yes bucket [0.85, 0.97]
        # had a 19% empirical win rate vs ~86% expected — severe under-spread
        # in the PMF tails. Realized highs averaged +2.0°F vs the threshold
        # (cool bias in either the mean or sigma). Multiply both sigmas by
        # _SIGMA_INFLATION (1.4) so extreme p_yes values from a single
        # provider compress toward 0.7 instead of 0.95+. The skew direction
        # from _threshold_skew_sigmas is preserved.
        # NB: this is a stopgap for the immediate bleed. The real fix is
        # to correct the underlying ~2°F cold bias in mean predictions
        # (climatology recency + per-direction provider bias). Tracking
        # in task #16 follow-up work.
        _SIGMA_INFLATION = 1.4
        sigma_down *= _SIGMA_INFLATION
        sigma_up *= _SIGMA_INFLATION
        pmf = _asymmetric_gaussian_pmf(
            mean=float(provider_maxima_f[snapshot.provider_id]),
            sigma_down=sigma_down,
            sigma_up=sigma_up,
            support=support,
        )
        provider_pmfs.append((provider_weight, pmf))

    total_weight = sum(weight for weight, _ in provider_pmfs) or Decimal("1")
    mixed = [Decimal("0") for _ in support]
    for weight, pmf in provider_pmfs:
        normalized_weight = weight / total_weight
        for index, probability in enumerate(pmf):
            mixed[index] += normalized_weight * probability

    total_probability = sum(mixed) or Decimal("1")
    normalized_mixed = tuple(prob / total_probability for prob in mixed)
    provider_spread_f = (
        max(provider_maxima_f.values()) - min(provider_maxima_f.values())
        if provider_maxima_f
        else Decimal("0")
    )
    if provider_maxima_f and settlement_rule.threshold_f is not None:
        mean_provider_max = sum(provider_maxima_f.values()) / Decimal(len(provider_maxima_f))
        provider_threshold_straddle_f = max(
            Decimal("0"),
            provider_spread_f - abs(mean_provider_max - settlement_rule.threshold_f),
        )
    else:
        provider_threshold_straddle_f = Decimal("0")
    mean_sigma = sum(provider_sigmas_f.values()) / Decimal(len(provider_sigmas_f))
    sigma_equivalent = _decimal(
        sqrt(float(mean_sigma * mean_sigma) + float((provider_spread_f / Decimal("2")) ** 2))
    )

    distribution = ForecastDistribution(
        as_of_time=as_of_time,
        station_id=settlement_rule.station_id,
        support_temps_f=support,
        pmf=normalized_mixed,
        provider_weights=provider_weights,
        base_entropy=Decimal("0"),
        sigma_equivalent_f=sigma_equivalent,
        calibration_version=f"mvp_supported_providers_{_season_key(as_of_time)}_v1",
        conditioned_flag=False,
    )
    return ForecastEngineResult(
        distribution=distribution,
        provider_maxima_f=provider_maxima_f,
        provider_sigmas_f=provider_sigmas_f,
        provider_weights=provider_weights,
        provider_support_status=provider_support_status,
        provider_spread_f=provider_spread_f,
        provider_threshold_straddle_f=provider_threshold_straddle_f,
        provider_bias_adjustments_f=dict(provider_bias_adjustments or {}),
    )
