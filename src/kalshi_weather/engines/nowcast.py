from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from kalshi_weather.domain.models import CurrentStateEstimate, ForecastSnapshot, ObservationSnapshot, CityProfile


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values) / Decimal(len(values))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _season_key(as_of_time: datetime) -> str:
    month = as_of_time.month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _sky_cover_pct(code: str | None) -> Decimal:
    mapping = {
        "CLR": Decimal("0"),
        "SKC": Decimal("0"),
        "FEW": Decimal("15"),
        "SCT": Decimal("40"),
        "BKN": Decimal("75"),
        "OVC": Decimal("95"),
    }
    return mapping.get(str(code or "").upper(), Decimal("50"))


def _expected_cadence_minutes(observations: list[ObservationSnapshot]) -> int:
    if len(observations) < 2:
        return 60
    deltas = []
    for newer, older in zip(observations, observations[1:], strict=False):
        delta = (newer.event_time - older.event_time).total_seconds() / 60.0
        if delta > 0:
            deltas.append(delta)
    if not deltas:
        return 60
    return max(15, min(90, int(round(median(deltas)))))


def _contains_precip(observation: ObservationSnapshot | None) -> bool:
    if observation is None:
        return False
    weather = " ".join(observation.weather_codes).upper()
    return any(token in weather for token in ("RAIN", "SHOWER", "THUNDER", "DRIZZLE"))


def _contains_thunder(observation: ObservationSnapshot | None) -> bool:
    if observation is None:
        return False
    weather = " ".join(observation.weather_codes).upper()
    return "THUNDER" in weather


def _is_onshore(city_profile: CityProfile, wind_dir_deg: int | None) -> bool:
    if wind_dir_deg is None:
        return False
    return wind_dir_deg in city_profile.onshore_wind_sectors


def _short_horizon_delta(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    return values[1] - values[0]


def _hours_in_heating_window(
    as_of_time: datetime,
    *,
    window_start: int,
    window_end: int,
) -> Decimal:
    current_hour = Decimal(str(as_of_time.hour)) + (Decimal(str(as_of_time.minute)) / Decimal("60"))
    if current_hour <= Decimal(window_start):
        return Decimal("0")
    return _clamp(
        current_hour - Decimal(window_start),
        Decimal("0"),
        Decimal(str(max(window_end - window_start, 0))),
    )


def _observation_outlier_flag(
    latest: ObservationSnapshot,
    previous: ObservationSnapshot | None,
    *,
    expected_cadence_minutes: int,
) -> bool:
    if previous is None or latest.temperature_f is None or previous.temperature_f is None:
        return False
    elapsed_minutes = abs((latest.event_time - previous.event_time).total_seconds()) / 60.0
    if elapsed_minutes > max(expected_cadence_minutes, 1) * 1.25:
        return False
    return abs(latest.temperature_f - previous.temperature_f) > Decimal("8")


def _projection_weight(
    *,
    expected_cadence_minutes: int,
    lag_minutes: int,
    observation_trend: Decimal,
    forecast_trend: Decimal,
    in_heating_window: bool,
    shock_risk: Decimal,
) -> Decimal:
    cadence_factor = _clamp(
        Decimal(str(max(expected_cadence_minutes, 1) / 60.0)),
        Decimal("0.10"),
        Decimal("1.0"),
    )
    lag_factor = _clamp(
        Decimal(str(max(lag_minutes, 0) / max(expected_cadence_minutes, 1))),
        Decimal("0.15"),
        Decimal("1.0"),
    )
    alignment_factor = Decimal("1.0")
    if observation_trend != 0 and forecast_trend != 0 and (observation_trend * forecast_trend) < 0:
        alignment_factor = Decimal("0.55")
    elif observation_trend == 0 or forecast_trend == 0:
        alignment_factor = Decimal("0.80")
    window_factor = Decimal("1.0") if in_heating_window else Decimal("0.65")
    shock_factor = Decimal("1.0") - (shock_risk * Decimal("0.35"))
    short_cadence_factor = Decimal("1.0")
    if expected_cadence_minutes <= 20:
        short_cadence_factor *= Decimal("0.55")
        if lag_minutes <= expected_cadence_minutes and shock_risk < Decimal("0.35"):
            short_cadence_factor *= Decimal("0.65")
    return _clamp(
        cadence_factor * lag_factor * alignment_factor * window_factor * shock_factor * short_cadence_factor,
        Decimal("0"),
        Decimal("1.0"),
    )


def _cloud_transition_score(
    latest: ObservationSnapshot,
    previous: ObservationSnapshot | None,
    forecast: ForecastSnapshot | None,
) -> Decimal:
    observed_jump = Decimal("0")
    if previous is not None:
        observed_jump = max(Decimal("0"), _sky_cover_pct(latest.sky_cover_code) - _sky_cover_pct(previous.sky_cover_code))
    forecast_jump = Decimal("0")
    if forecast and forecast.cloud_cover_path_pct:
        forecast_jump = max(Decimal("0"), _short_horizon_delta(forecast.cloud_cover_path_pct))
    return _clamp((observed_jump / Decimal("100")) + (forecast_jump / Decimal("150")), Decimal("0"), Decimal("1"))


def _storm_onset_score(
    latest: ObservationSnapshot,
    previous: ObservationSnapshot | None,
    forecast: ForecastSnapshot | None,
) -> Decimal:
    observed = Decimal("0")
    if _contains_precip(latest):
        observed = Decimal("0.20")
        if previous is None or not _contains_precip(previous):
            observed = Decimal("0.45")
        if _contains_thunder(latest):
            observed += Decimal("0.20")
    forecast_component = Decimal("0")
    if forecast and forecast.precipitation_path:
        forecast_component = max(
            Decimal("0"),
            (forecast.precipitation_path[0] - Decimal("35")) / Decimal("100"),
        )
        forecast_component += max(Decimal("0"), _short_horizon_delta(forecast.precipitation_path) / Decimal("150"))
    return _clamp(observed + forecast_component, Decimal("0"), Decimal("1"))


def _wind_shift_score(latest: ObservationSnapshot, previous: ObservationSnapshot | None) -> Decimal:
    if previous is None or previous.wind_dir_deg is None or latest.wind_dir_deg is None:
        return Decimal("0")
    raw_delta = abs(latest.wind_dir_deg - previous.wind_dir_deg)
    delta = min(raw_delta, 360 - raw_delta)
    speed = latest.wind_speed_kt or Decimal("0")
    speed_factor = _clamp(speed / Decimal("20"), Decimal("0"), Decimal("1"))
    return _clamp((Decimal(str(delta)) / Decimal("180")) * speed_factor, Decimal("0"), Decimal("0.35"))


def _marine_transition_score(
    latest: ObservationSnapshot,
    previous: ObservationSnapshot | None,
    city_profile: CityProfile,
    in_heating_window: bool,
) -> Decimal:
    if not city_profile.marine_sensitive_flag or not in_heating_window:
        return Decimal("0")
    if not _is_onshore(city_profile, latest.wind_dir_deg):
        return Decimal("0")
    if previous is not None and _is_onshore(city_profile, previous.wind_dir_deg):
        return Decimal("0")
    speed = latest.wind_speed_kt or Decimal("0")
    return _clamp(speed / Decimal("25"), Decimal("0"), Decimal("0.30"))


def build_current_state_estimate(
    observations: list[ObservationSnapshot],
    forecast: ForecastSnapshot | None,
    city_profile: CityProfile,
    as_of_time: datetime,
    station_timezone: str | None = None,
    yesterday_high_f: Decimal | None = None,
) -> CurrentStateEstimate:
    if not observations:
        raise ValueError("observations are required")
    latest = observations[0]
    previous = observations[1] if len(observations) > 1 else None
    if latest.temperature_f is None:
        raise ValueError("latest observation must include temperature")
    local_as_of_time = as_of_time.astimezone(ZoneInfo(station_timezone)) if station_timezone else as_of_time
    season = _season_key(local_as_of_time)
    window_start, window_end = city_profile.heating_window_by_season.get(season, (9, 17))
    in_heating_window = window_start <= local_as_of_time.hour <= window_end

    recent_trend_values: list[Decimal] = []
    for newer, older in zip(observations, observations[1:], strict=False):
        if newer.temperature_f is None or older.temperature_f is None:
            continue
        hours = Decimal(
            str((newer.event_time - older.event_time).total_seconds() / 3600.0)
        )
        if hours == 0:
            continue
        recent_trend_values.append((newer.temperature_f - older.temperature_f) / hours)
    observation_trend = _average(recent_trend_values)

    forecast_trend = Decimal("0")
    if forecast and len(forecast.hourly_temp_path_f) >= 2:
        forecast_trend = forecast.hourly_temp_path_f[1] - forecast.hourly_temp_path_f[0]

    lag_minutes = int((as_of_time - latest.event_time).total_seconds() / 60)
    lag_hours = Decimal(str(max(lag_minutes, 0) / 60.0))
    expected_cadence_minutes = _expected_cadence_minutes(observations)
    excess_lag_minutes = max(0, lag_minutes - expected_cadence_minutes)
    observation_outlier_flag = _observation_outlier_flag(
        latest,
        previous,
        expected_cadence_minutes=expected_cadence_minutes,
    )
    hours_in_window = _hours_in_heating_window(
        local_as_of_time,
        window_start=window_start,
        window_end=window_end,
    )
    observation_weight = _clamp(
        Decimal("0.50") + (Decimal("0.05") * hours_in_window),
        Decimal("0.50"),
        Decimal("0.90"),
    )
    if observation_outlier_flag:
        observation_weight = min(observation_weight, Decimal("0.35"))
    forecast_weight = Decimal("1") - observation_weight
    trend = (observation_trend * observation_weight) + (forecast_trend * forecast_weight)

    cloud_score = _cloud_transition_score(latest, previous, forecast)
    storm_score = _storm_onset_score(latest, previous, forecast)
    wind_shift_score = _wind_shift_score(latest, previous)
    marine_score = _marine_transition_score(latest, previous, city_profile, in_heating_window)
    shock_risk = _clamp(
        (cloud_score * Decimal("0.45"))
        + (storm_score * Decimal("0.40"))
        + (wind_shift_score * Decimal("0.15"))
        + marine_score,
        Decimal("0"),
        Decimal("1"),
    )
    if (
        cloud_score > Decimal("0.40")
        and storm_score > Decimal("0.30")
        and wind_shift_score > Decimal("0.15")
    ):
        shock_risk = _clamp(shock_risk * Decimal("1.25"), Decimal("0"), Decimal("1"))
    elif (
        cloud_score > Decimal("0.40")
        and storm_score < Decimal("0.20")
        and wind_shift_score < Decimal("0.12")
    ):
        shock_risk = min(shock_risk, Decimal("0.45") + marine_score)
    cadence_hours = Decimal(str(expected_cadence_minutes / 60.0))
    effective_projection_hours = min(lag_hours, cadence_hours)
    if lag_hours > cadence_hours:
        effective_projection_hours += (lag_hours - cadence_hours) * Decimal("0.35")
    projection_weight = _projection_weight(
        expected_cadence_minutes=expected_cadence_minutes,
        lag_minutes=lag_minutes,
        observation_trend=observation_trend,
        forecast_trend=forecast_trend,
        in_heating_window=in_heating_window,
        shock_risk=shock_risk,
    )
    carry_forward_shock_threshold = Decimal("0.35")
    if city_profile.marine_sensitive_flag:
        carry_forward_shock_threshold = Decimal("0.45")
    carry_forward_bias_mode = (
        expected_cadence_minutes <= 20
        and lag_minutes <= expected_cadence_minutes
        and shock_risk < carry_forward_shock_threshold
        and not observation_outlier_flag
    )
    if carry_forward_bias_mode:
        projection_weight = Decimal("0")
    projected_delta = trend * effective_projection_hours

    cloud_adjustment = Decimal("0")
    if in_heating_window and cloud_score > 0:
        cloud_adjustment = -min(
            city_profile.cloud_shock_cap_f * Decimal("0.18"),
            city_profile.cloud_shock_cap_f
            * Decimal(str(float(cloud_score) ** 1.8))
            * Decimal("0.18"),
        )
    storm_adjustment = Decimal("0")
    if storm_score > 0:
        storm_adjustment = -min(
            city_profile.storm_shock_cap_f * Decimal("0.16"),
            city_profile.storm_shock_cap_f
            * Decimal(str(float(storm_score) ** 1.5))
            * Decimal("0.16"),
        )

    marine_adjustment = Decimal("0")
    if marine_score > 0:
        marine_adjustment = -min(
            city_profile.marine_intrusion_cap_f * Decimal("0.12"),
            city_profile.marine_intrusion_cap_f * marine_score * Decimal("0.12"),
        )

    wind_shift_adjustment = Decimal("0")
    if previous is not None and wind_shift_score > 0 and observation_trend < 0:
        wind_shift_adjustment = (
            -min(
                city_profile.wind_shift_cap_f * Decimal("0.08"),
                city_profile.wind_shift_cap_f * wind_shift_score * Decimal("0.08"),
            )
        )

    lag_penalty = Decimal("0")
    if excess_lag_minutes > 0:
        lag_penalty = min(Decimal("1"), Decimal(str(excess_lag_minutes / 25.0)))

    sigma = (
        Decimal("0.8")
        + (Decimal("0.04") * Decimal(excess_lag_minutes))
        + (Decimal("0.9") * shock_risk)
    )
    if observation_outlier_flag:
        sigma += Decimal("0.75")
    confidence_downgrade = min(
        Decimal("0.6"),
        (Decimal("0.45") * lag_penalty) + (Decimal("0.35") * shock_risk),
    )
    if observation_outlier_flag and previous is not None and previous.temperature_f is not None:
        anchor_temperature = (latest.temperature_f + previous.temperature_f) / Decimal("2")
    else:
        anchor_temperature = latest.temperature_f
    if observation_outlier_flag:
        confidence_downgrade = min(Decimal("0.6"), confidence_downgrade + Decimal("0.20"))
    current_temp_estimate = anchor_temperature + (
        projected_delta
        + cloud_adjustment
        + storm_adjustment
        + marine_adjustment
        + wind_shift_adjustment
    ) * projection_weight

    return CurrentStateEstimate(
        as_of_time=as_of_time,
        station_id=latest.station_id,
        current_temp_est_f=current_temp_estimate,
        current_temp_sigma_f=sigma,
        near_term_slope_f_per_hr=trend,
        observation_lag_minutes=lag_minutes,
        expected_observation_cadence_minutes=expected_cadence_minutes,
        observation_excess_lag_minutes=excess_lag_minutes,
        observation_lag_penalty=lag_penalty,
        shock_risk_score=shock_risk,
        cloud_cover_shock_adjustment_f=cloud_adjustment,
        storm_shock_adjustment_f=storm_adjustment,
        marine_intrusion_adjustment_f=marine_adjustment,
        wind_shift_adjustment_f=wind_shift_adjustment,
        discontinuity_suspected=(
            shock_risk >= Decimal("0.55")
            or storm_score >= Decimal("0.55")
            or excess_lag_minutes >= 20
            or observation_outlier_flag
        ),
        confidence_downgrade=confidence_downgrade,
        provenance_refs=tuple(obs.source_payload_id for obs in observations[:2]),
        yesterday_high_f=yesterday_high_f,
    )
