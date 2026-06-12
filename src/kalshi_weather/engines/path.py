from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from math import exp
from zoneinfo import ZoneInfo

from kalshi_weather.domain.models import (
    CityProfile,
    CurrentStateEstimate,
    ForecastDistribution,
    ObservationSnapshot,
    PathProgressState,
    SettlementRule,
)
from kalshi_weather.engines.climatology import apply_shrinkage, compute_p_climatology
from kalshi_weather.engines.regime import synoptic_peak_hour_adjustment


@dataclass(frozen=True, slots=True)
class PathEngineResult:
    path_state: PathProgressState
    conditioned_distribution: ForecastDistribution
    p_yes: Decimal


def _season_key(as_of_time: datetime) -> str:
    month = as_of_time.month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _sigmoid(x: float) -> Decimal:
    return Decimal(str(1 / (1 + exp(-x))))


def _conditional_tail_stats(
    distribution: ForecastDistribution,
    *,
    current_high: Decimal,
) -> tuple[Decimal, Decimal]:
    tail = [
        (Decimal(temp), probability)
        for temp, probability in zip(distribution.support_temps_f, distribution.pmf, strict=False)
        if Decimal(temp) > current_high
    ]
    tail_probability = sum(probability for _temp, probability in tail)
    if tail_probability <= 0:
        return Decimal("0"), Decimal("0")
    conditional_mean = sum(temp * probability for temp, probability in tail) / tail_probability
    conditional_p80_temp = current_high
    cumulative = Decimal("0")
    for temp, probability in tail:
        cumulative += probability / tail_probability
        conditional_p80_temp = temp
        if cumulative >= Decimal("0.80"):
            break
    return (
        max(Decimal("0"), conditional_mean - current_high),
        max(Decimal("0"), conditional_p80_temp - current_high),
    )


def _passthrough_for_low_market(
    *,
    distribution: ForecastDistribution,
    current_state: CurrentStateEstimate,
    settlement_rule: SettlementRule,
    city_profile: CityProfile,
    as_of_time: datetime,
    local_as_of_time: datetime,
    threshold: Decimal,
) -> PathEngineResult:
    """LOW-market handler: skip path conditioning, apply climatology
    shrinkage, return p_yes from the raw distribution.

    The full path engine assumes a heating window with rising temperature
    toward an afternoon max — that math is wrong for LOW markets which
    settle on the overnight minimum. Until we build symmetric cooling-path
    logic, LOW markets use forecast + climatology only.
    """
    threshold_high = settlement_rule.threshold_high_f
    is_range = settlement_rule.operator == "between" and threshold_high is not None
    p_yes = Decimal("0")
    for temp, probability in zip(
        distribution.support_temps_f, distribution.pmf, strict=False
    ):
        if is_range:
            if threshold <= Decimal(temp) <= threshold_high:
                p_yes += probability
        elif settlement_rule.operator in {">", ">="}:
            if temp > threshold or (temp == threshold and settlement_rule.inclusive_flag):
                p_yes += probability
        else:
            if temp < threshold or (temp == threshold and settlement_rule.inclusive_flag):
                p_yes += probability

    # T1.2 climatology shrinkage (correctly looks up tmin_f_by_month here)
    p_pre_shrinkage = p_yes
    p_climatology = compute_p_climatology(
        city_id=city_profile.city_id,
        settlement_month=settlement_rule.local_standard_window_start.month,
        operator=settlement_rule.operator,
        threshold_f=threshold,
        threshold_high_f=threshold_high,
        inclusive_flag=settlement_rule.inclusive_flag,
        settlement_variable=getattr(
            settlement_rule, "settlement_variable", "daily_low_temperature_f"
        ),
    )
    p_yes, shrinkage_weight = apply_shrinkage(
        p_model=p_yes, p_climatology=p_climatology
    )

    path_state = PathProgressState(
        as_of_time=as_of_time,
        current_high_so_far_f=Decimal("0"),
        current_temp_f=current_state.current_temp_est_f,
        threshold_gap_f=Decimal("0"),
        remaining_effective_window_minutes=0,
        estimated_intraday_slope_f_per_hr=Decimal("0"),
        solar_insolation_vector={
            "daylight_weight": Decimal("0"),
            "minutes_to_peak": Decimal("0"),
        },
        thermal_ceiling_estimate_f=Decimal("0"),
        residual_gain_mean_f=Decimal("0"),
        residual_gain_p80_f=Decimal("0"),
        reachability_score=Decimal("1"),
        late_day_decay_factor=Decimal("1"),
        path_uncertainty_addon=Decimal("0.05"),  # mild extra unc for LOW until proper logic
        threshold_already_crossed_flag=False,
        persistence_gap_f=None,
        persistence_uncertainty_addon=Decimal("0"),
        p_climatology=p_climatology,
        p_pre_shrinkage=p_pre_shrinkage,
        shrinkage_weight=shrinkage_weight,
    )
    return PathEngineResult(
        path_state=path_state,
        conditioned_distribution=distribution,
        p_yes=p_yes,
    )


def apply_path_adjustment(
    distribution: ForecastDistribution,
    current_state: CurrentStateEstimate,
    observations: list[ObservationSnapshot],
    settlement_rule: SettlementRule,
    city_profile: CityProfile,
    as_of_time: datetime,
) -> PathEngineResult:
    local_tz = settlement_rule.local_standard_window_start.tzinfo or ZoneInfo("UTC")
    local_as_of_time = as_of_time.astimezone(local_tz)
    threshold = settlement_rule.threshold_f or Decimal("0")

    # 2026-05-17: LOW markets — settlement is overnight, the path engine's
    # daytime-warming heuristics don't apply. Use raw forecast distribution
    # (already correctly built from min-of-hourly in forecast.py) plus
    # climatology shrinkage. Persistence + path conditioning skipped.
    is_low_market = (
        getattr(settlement_rule, "settlement_variable", None)
        == "daily_low_temperature_f"
    )
    if is_low_market:
        return _passthrough_for_low_market(
            distribution=distribution,
            current_state=current_state,
            settlement_rule=settlement_rule,
            city_profile=city_profile,
            as_of_time=as_of_time,
            local_as_of_time=local_as_of_time,
            threshold=threshold,
        )
    settlement_window_started = local_as_of_time >= settlement_rule.local_standard_window_start
    settlement_window_open = (
        settlement_rule.local_standard_window_start
        <= local_as_of_time
        <= settlement_rule.local_standard_window_end
    )
    high_so_far_inputs = [
        obs.temperature_f
        for obs in observations
        if (
            obs.temperature_f is not None
            and settlement_rule.local_standard_window_start
            <= obs.event_time.astimezone(local_tz)
            <= min(local_as_of_time, settlement_rule.local_standard_window_end)
        )
    ]
    if settlement_window_open:
        high_so_far_inputs.append(current_state.current_temp_est_f)
    path_floor_active = bool(high_so_far_inputs)
    current_high = (
        max(high_so_far_inputs)
        if path_floor_active
        else Decimal(min(distribution.support_temps_f)) - Decimal("1")
    )
    threshold_gap = threshold - current_high
    settlement_day = settlement_rule.local_standard_window_start.date()
    season = _season_key(settlement_rule.local_standard_window_start)
    base_peak_hour = city_profile.typical_peak_hour_local_by_season.get(season, 15)
    # Adjust peak hour earlier when shock/cloud signals indicate the temperature
    # will peak before the climatological afternoon maximum (e.g. convective days).
    peak_hour = synoptic_peak_hour_adjustment(current_state, base_peak_hour)
    start_hour, end_hour = city_profile.heating_window_by_season.get(season, (9, 17))
    heating_start_dt = datetime.combine(settlement_day, time(start_hour, 0), tzinfo=local_tz)
    peak_dt = datetime.combine(settlement_day, time(peak_hour, 0), tzinfo=local_tz)
    window_end_dt = datetime.combine(settlement_day, time(end_hour, 0), tzinfo=local_tz)
    remaining_minutes = max(0, int((window_end_dt - local_as_of_time).total_seconds() / 60))
    daylight_weight = Decimal("1") if heating_start_dt <= local_as_of_time <= window_end_dt else Decimal("0")
    solar_insolation_vector = {
        "daylight_weight": daylight_weight,
        "minutes_to_peak": Decimal(str(max(0, int((peak_dt - local_as_of_time).total_seconds() / 60)))),
    }
    residual_gain_mean, residual_gain_p80 = _conditional_tail_stats(
        distribution,
        current_high=current_high,
    )
    thermal_ceiling = current_high + residual_gain_p80
    threshold_crossed = (
        path_floor_active
        and current_high >= threshold
        and settlement_rule.operator in {">", ">="}
    )
    reachable_gain = min(residual_gain_mean, max(Decimal("0"), thermal_ceiling - current_high))
    reachability_score = (
        Decimal("1")
        if threshold_crossed or not settlement_window_started
        else _sigmoid(
            float(
                (reachable_gain - threshold_gap)
                / max(current_state.current_temp_sigma_f, Decimal("1"))
            )
        )
    )

    if threshold_crossed:
        decay_factor = Decimal("1")
    elif local_as_of_time < peak_dt:
        decay_factor = Decimal("1")
    elif local_as_of_time <= peak_dt + timedelta(hours=1):
        decay_factor = Decimal("0.85")
    elif local_as_of_time <= peak_dt + timedelta(hours=2):
        decay_factor = Decimal("0.60")
    else:
        decay_factor = Decimal("0.35")

    adjusted = []
    # For range/bracket ("between") markets the decay should apply to mass
    # strictly above the CAP (overshoot is what makes YES lose), not above
    # the floor. Mass inside the range [floor, cap] is winning territory.
    is_range_market = (
        settlement_rule.operator == "between"
        and settlement_rule.threshold_high_f is not None
    )
    decay_pivot = (
        settlement_rule.threshold_high_f if is_range_market else threshold
    )
    for temp, probability in zip(distribution.support_temps_f, distribution.pmf, strict=False):
        if path_floor_active and Decimal(temp) < current_high:
            adjusted.append(Decimal("0"))
            continue
        if threshold_crossed:
            adjusted.append(probability)
            continue
        # For range markets: decay only the OVERSHOOT mass (temp > cap).
        # For threshold markets: decay mass at-or-above the threshold as before.
        should_decay = (
            (is_range_market and Decimal(temp) > decay_pivot)
            or (not is_range_market and Decimal(temp) >= decay_pivot)
        )
        if should_decay:
            adjusted.append(probability * reachability_score * decay_factor)
        else:
            adjusted.append(probability)
    total = sum(adjusted) or Decimal("1")
    conditioned_pmf = tuple(prob / total for prob in adjusted)
    conditioned_distribution = ForecastDistribution(
        as_of_time=distribution.as_of_time,
        station_id=distribution.station_id,
        support_temps_f=distribution.support_temps_f,
        pmf=conditioned_pmf,
        provider_weights=distribution.provider_weights,
        base_entropy=distribution.base_entropy,
        sigma_equivalent_f=distribution.sigma_equivalent_f,
        calibration_version=distribution.calibration_version,
        conditioned_flag=True,
    )
    p_yes = Decimal("0")
    # Range markets: YES if floor <= temp <= cap (both inclusive). For these
    # `threshold_high_f` is set and operator == "between".
    threshold_high = settlement_rule.threshold_high_f
    is_range = settlement_rule.operator == "between" and threshold_high is not None

    # 2026-05-23: Intraday Bayesian update from ASOS observations.
    # If today's already-observed hours are running warmer/cooler than the
    # NWS hourly forecast for those same hours, that bias is a posterior
    # signal — the remaining-day high is likely to inherit it. Apply as a
    # horizontal shift of the distribution (equivalently: shift the
    # threshold(s) by the inverse of the bias). Requires >=3 sample hours
    # for stability; bias is bounded to ±2°F so a single bad obs cannot
    # dominate. The existing reachability conditioning already neutralizes
    # the signal when the realized high is locked in.
    effective_threshold = threshold
    effective_threshold_high = threshold_high
    if (current_state.intraday_obs_forecast_bias_f is not None
            and current_state.intraday_obs_sample_hours is not None
            and current_state.intraday_obs_sample_hours >= 3):
        raw_bias = Decimal(str(current_state.intraday_obs_forecast_bias_f))
        bounded_bias = max(Decimal("-2"), min(Decimal("2"), raw_bias))
        effective_threshold = threshold - bounded_bias
        if threshold_high is not None:
            effective_threshold_high = threshold_high - bounded_bias

    for temp, probability in zip(conditioned_distribution.support_temps_f, conditioned_distribution.pmf, strict=False):
        if is_range:
            # Inclusive both ends per Kalshi "between X-Y" rule wording.
            if effective_threshold <= Decimal(temp) <= effective_threshold_high:
                p_yes += probability
        elif settlement_rule.operator in {">", ">="}:
            if temp > effective_threshold or (temp == effective_threshold and settlement_rule.inclusive_flag):
                p_yes += probability
        else:
            if temp < effective_threshold or (temp == effective_threshold and settlement_rule.inclusive_flag):
                p_yes += probability

    # T1.2 climatology shrinkage — anchor p_model against 30-year base rate
    # for this (city × month × threshold). When the model says 80% but
    # climatology says 20%, we shrink toward the prior to catch model busts.
    # No-ops gracefully when the climate normals cache is missing for this
    # city or settlement month sample is too small.
    p_pre_shrinkage = p_yes
    p_climatology = compute_p_climatology(
        city_id=city_profile.city_id,
        settlement_month=settlement_day.month,
        operator=settlement_rule.operator,
        threshold_f=threshold,
        threshold_high_f=threshold_high,
        inclusive_flag=settlement_rule.inclusive_flag,
        settlement_variable=settlement_rule.settlement_variable
            if hasattr(settlement_rule, "settlement_variable")
            else "daily_high_temperature_f",
    )
    p_yes, shrinkage_weight = apply_shrinkage(
        p_model=p_yes, p_climatology=p_climatology
    )

    # T2.5 isotonic recalibration — maps raw p_yes to empirical hit rate
    # learned from settled bets. Identity until 50+ settled samples exist.
    # Wrapped in try so cache parse errors never block decisions.
    try:
        from kalshi_weather.analytics.probability_calibration import apply_calibration
        p_yes, _calibrated = apply_calibration(p_yes)
    except Exception:
        pass

    base_path_uncertainty_addon = (
        (Decimal("0.15") * (Decimal("1") - reachability_score))
        + (Decimal("0.10") * (Decimal("1") - decay_factor))
    )

    # T1.3 persistence baseline — if model strongly disagrees with what
    # actually happened yesterday, the regime is in transition and our
    # forecast confidence should be lowered. Adds up to +0.05 to the addon
    # (small, deliberate: we don't want to over-suppress signals on shoulder
    # days like a cold front passing through). Gracefully no-ops when we
    # haven't ingested yesterday's settlement.
    persistence_gap_f: Decimal | None = None
    persistence_uncertainty_addon = Decimal("0")
    if current_state.yesterday_high_f is not None:
        ensemble_mean_f = sum(
            Decimal(temp) * prob
            for temp, prob in zip(
                distribution.support_temps_f, distribution.pmf, strict=False
            )
        )
        persistence_gap_f = ensemble_mean_f - current_state.yesterday_high_f
        abs_gap = abs(persistence_gap_f)
        # Linear ramp: 0 below 5°F, full +0.05 at 10°F or more.
        if abs_gap > Decimal("5"):
            ramp = (abs_gap - Decimal("5")) / Decimal("5")
            ramp = min(Decimal("1"), max(Decimal("0"), ramp))
            persistence_uncertainty_addon = ramp * Decimal("0.05")

    # SPC convective outlook → uncertainty. Convection is non-linear and
    # our daily-max prediction is unreliable when SPC flags risk.
    # Conservative additions: small bump per rank tier.
    spc_uncertainty_addon = Decimal("0")
    if current_state.spc_outlook_rank == 2:    # MRGL
        spc_uncertainty_addon = Decimal("0.01")
    elif current_state.spc_outlook_rank == 3:  # SLGT
        spc_uncertainty_addon = Decimal("0.03")
    elif current_state.spc_outlook_rank == 4:  # ENH
        spc_uncertainty_addon = Decimal("0.06")
    elif current_state.spc_outlook_rank >= 5:  # MDT / HIGH
        spc_uncertainty_addon = Decimal("0.10")

    # AFD forecaster narrative → uncertainty + bias.
    afd_uncertainty_addon = Decimal("0")
    if current_state.afd_confidence == "low":
        afd_uncertainty_addon += Decimal("0.02")
    elif current_state.afd_confidence == "high":
        afd_uncertainty_addon -= Decimal("0.01")  # small confidence boost
    if current_state.afd_model_spread_flag is True:
        afd_uncertainty_addon += Decimal("0.03")
    # 2026-05-19: also use the LLM-extracted ``regime`` signal — previously
    # discarded. Regimes that mean "expect big day-over-day change" inflate
    # uncertainty; stable regimes shrink it slightly.
    regime = current_state.afd_regime
    if regime in ("frontal_passage", "convective"):
        # High-change regimes: ensemble forecasts of daily max are
        # systematically less reliable when a front or convection is
        # passing. Add real uncertainty.
        afd_uncertainty_addon += Decimal("0.03")
    elif regime in ("stable", "ridge", "marine_layer"):
        # Stable regimes: forecasters agree, ensemble has tight spread,
        # we can lean in slightly.
        afd_uncertainty_addon -= Decimal("0.01")
    elif regime in ("anomalous_warm", "anomalous_cool"):
        # Forecaster explicitly flagging anomaly: still useful but the
        # MAGNITUDE of the anomaly is what carries risk, not the label
        # itself. Tiny bump.
        afd_uncertainty_addon += Decimal("0.01")
    # Cap the AFD contribution at [-0.02, 0.07] so a bad LLM extraction
    # cannot dominate. Widened the floor (was 0) so high-confidence stable
    # regimes can give a real boost.
    afd_uncertainty_addon = max(Decimal("-0.02"), min(Decimal("0.07"), afd_uncertainty_addon))

    # NOTE: ``current_state.afd_mentioned_today_high_f`` is extracted by
    # the LLM but not yet applied as a prior pull on the distribution.
    # That requires shifting the discrete PMF support which is a bigger
    # math change — deferred. The signal is captured in the decision
    # payload (via path_state) so we can backtest the value of using
    # it before wiring it into the live decision.

    # 2026-05-23: NWS forecast revision-pace as small uncertainty signal.
    # Empirically (May 22-23) the per-city revision count over 24h ranged
    # from 3 (Denver, stable regime) to 11 (Chicago/Austin, active regimes).
    # When NWS has been actively rewriting the forecast all day, the
    # underlying skill is lower — slight uncertainty bump. Capped to keep
    # any single signal from dominating.
    revisions_addon = Decimal("0")
    if current_state.nws_forecast_revisions_24h is not None:
        excess = max(0, current_state.nws_forecast_revisions_24h - 5)
        revisions_addon = min(Decimal("0.03"), Decimal(str(excess)) * Decimal("0.005"))

    # 2026-05-23: GEFS ensemble inter-member std as probabilistic uncertainty
    # signal. Independent of our own across-model fusion math (GEFS spread
    # comes from initial-condition perturbations of a single model). Typical
    # member spread for daily high is 1-3°F in stable regimes, 4-6°F in
    # transitions. We treat >1.5°F as meaningfully unsettled. Capped at 0.03
    # to match the other small addons; cannot dominate.
    gefs_ensemble_addon = Decimal("0")
    if current_state.gefs_ensemble_std_f is not None:
        excess_f = max(Decimal("0"), Decimal(str(current_state.gefs_ensemble_std_f)) - Decimal("1.5"))
        gefs_ensemble_addon = min(Decimal("0.03"), excess_f * Decimal("0.01"))

    path_uncertainty_addon = min(
        Decimal("0.25"),
        base_path_uncertainty_addon
        + persistence_uncertainty_addon
        + spc_uncertainty_addon
        + afd_uncertainty_addon
        + revisions_addon
        + gefs_ensemble_addon,
    )

    path_state = PathProgressState(
        as_of_time=as_of_time,
        current_high_so_far_f=current_high,
        current_temp_f=current_state.current_temp_est_f,
        threshold_gap_f=threshold_gap,
        remaining_effective_window_minutes=remaining_minutes,
        estimated_intraday_slope_f_per_hr=current_state.near_term_slope_f_per_hr,
        solar_insolation_vector=solar_insolation_vector,
        thermal_ceiling_estimate_f=thermal_ceiling,
        residual_gain_mean_f=residual_gain_mean,
        residual_gain_p80_f=residual_gain_p80,
        reachability_score=reachability_score,
        late_day_decay_factor=decay_factor,
        path_uncertainty_addon=path_uncertainty_addon,
        threshold_already_crossed_flag=threshold_crossed,
        persistence_gap_f=persistence_gap_f,
        persistence_uncertainty_addon=persistence_uncertainty_addon,
        p_climatology=p_climatology,
        p_pre_shrinkage=p_pre_shrinkage,
        shrinkage_weight=shrinkage_weight,
    )
    return PathEngineResult(
        path_state=path_state,
        conditioned_distribution=conditioned_distribution,
        p_yes=p_yes,
    )
