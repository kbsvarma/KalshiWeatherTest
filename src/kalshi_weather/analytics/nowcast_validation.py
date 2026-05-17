from __future__ import annotations

from decimal import Decimal
from math import sqrt

from kalshi_weather.domain.models import CityProfile, ForecastSnapshot, ObservationSnapshot
from kalshi_weather.engines.nowcast import build_current_state_estimate


def _latest_forecast_before(
    forecasts: list[ForecastSnapshot],
    as_of_time,
) -> ForecastSnapshot | None:
    candidates = [forecast for forecast in forecasts if forecast.provider_run_time <= as_of_time]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.provider_run_time)


def _bucket(shock_risk: Decimal) -> str:
    if shock_risk < Decimal("0.33"):
        return "low"
    if shock_risk < Decimal("0.66"):
        return "medium"
    return "high"


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _rmse(values: list[float]) -> float | None:
    if not values:
        return None
    return sqrt(sum(value * value for value in values) / len(values))


def build_nowcast_validation_report(
    observations: list[ObservationSnapshot],
    forecasts: list[ForecastSnapshot],
    city_profile: CityProfile,
    station_timezone: str | None = None,
) -> dict[str, object]:
    ordered = sorted(
        [obs for obs in observations if obs.temperature_f is not None],
        key=lambda item: item.event_time,
    )
    model_errors: list[float] = []
    carry_errors: list[float] = []
    linear_errors: list[float] = []
    bucket_errors: dict[str, list[float]] = {"low": [], "medium": [], "high": []}

    for index in range(2, len(ordered)):
        target = ordered[index]
        history = list(reversed(ordered[max(0, index - 4):index]))
        if len(history) < 2 or history[0].temperature_f is None or history[1].temperature_f is None:
            continue
        forecast = _latest_forecast_before(forecasts, target.event_time)
        estimate = build_current_state_estimate(
            observations=history,
            forecast=forecast,
            city_profile=city_profile,
            as_of_time=target.event_time,
            station_timezone=station_timezone,
        )
        actual = float(target.temperature_f)
        latest = float(history[0].temperature_f)
        model_error = abs(float(estimate.current_temp_est_f) - actual)
        carry_error = abs(latest - actual)
        elapsed_hours = (target.event_time - history[0].event_time).total_seconds() / 3600.0
        linear_projection = latest + (float(estimate.near_term_slope_f_per_hr) * max(elapsed_hours, 0.0))
        linear_error = abs(linear_projection - actual)
        model_errors.append(model_error)
        carry_errors.append(carry_error)
        linear_errors.append(linear_error)
        bucket_errors[_bucket(estimate.shock_risk_score)].append(model_error)

    required_sample = 24
    model_mae = _mean(model_errors)
    carry_mae = _mean(carry_errors)
    linear_mae = _mean(linear_errors)
    sample_count = len(model_errors)
    sample_sufficient = sample_count >= required_sample
    beats_baselines = bool(
        sample_sufficient
        and model_mae is not None
        and carry_mae is not None
        and linear_mae is not None
        and model_mae < carry_mae
        and model_mae < linear_mae
    )
    best_baseline = min(
        [value for value in (carry_mae, linear_mae) if value is not None],
        default=None,
    )
    report = {
        "sample_count": len(model_errors),
        "required_sample": required_sample,
        "sample_sufficient": sample_sufficient,
        "model_mae_f": model_mae,
        "carry_forward_mae_f": carry_mae,
        "linear_only_mae_f": linear_mae,
        "model_rmse_f": _rmse(model_errors),
        "shock_buckets": {
            bucket: {
                "count": len(values),
                "mae_f": _mean(values),
            }
            for bucket, values in bucket_errors.items()
        },
        "best_available_baseline_mae_f": best_baseline,
    }
    report["beats_baselines"] = beats_baselines
    report["redesign_required"] = bool(
        sample_sufficient
        and best_baseline is not None
        and model_mae is not None
        and model_mae > (best_baseline * 1.10)
    )
    report["validation_blocked"] = not beats_baselines
    return report
