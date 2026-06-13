from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from statistics import fmean, median, pstdev
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from kalshi_weather.domain.models import ForecastSnapshot, ObservationSnapshot, StationReference


def _to_local_date(value: datetime, timezone_name: str) -> str:
    return value.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _season_key(value: datetime, timezone_name: str) -> str:
    month = value.astimezone(ZoneInfo(timezone_name)).month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _lead_bucket_name(lead_hours: float) -> str:
    if lead_hours > 12:
        return "gt_12h"
    if lead_hours >= 6:
        return "six_to_12h"
    return "lt_6h"


def _forecast_day_max(
    snapshot: ForecastSnapshot,
    timezone_name: str,
) -> tuple[str, Decimal, datetime] | None:
    run_date = _to_local_date(snapshot.provider_run_time, timezone_name)
    samples = [
        (temp, valid_time)
        for temp, valid_time in zip(snapshot.hourly_temp_path_f, snapshot.valid_for_times, strict=False)
        if _to_local_date(valid_time, timezone_name) == run_date
    ]
    # 2026-05-25 bugfix: removed the silent fallback that re-used all hours
    # of the path (or the bare max) when no run-date-aligned hours existed.
    # Returning None when no in-day samples exist makes the calibration
    # honestly skip that snapshot.
    if not samples:
        return None
    # 2026-06-12 bugfix (the REAL root cause of the phantom cool bias):
    # partial-coverage poisoning. A forecast fetched at 9 PM local has
    # run-date hours 21:00-23:00 only — its "day max" is the evening temp,
    # which sits 5-10°F below the actual afternoon peak. Comparing that to
    # the observed full-day max produced phantom "cool bias" of −5 to −7°F
    # per provider, which (clamped) over-warmed live forecasts. Measured
    # against 468 settled city-days with peak-coverage enforced, true
    # biases are only −0.5 to −1.3°F for most providers.
    # Require the run-date path to bracket the typical heating peak:
    # earliest sample ≤ 14:00 local AND latest ≥ 17:00 local. GraphCast's
    # short horizon (~48h with trailing Nones) fails this most often —
    # correctly so: its late-day day-max estimates were off by −9.6°F.
    tz = ZoneInfo(timezone_name)
    local_hours = [s[1].astimezone(tz).hour for s in samples]
    if min(local_hours) > 14 or max(local_hours) < 17:
        return None
    predicted_max, max_valid_time = max(samples, key=lambda sample: sample[0])
    return run_date, predicted_max, max_valid_time


def _interpolated_forecast_value(
    snapshot: ForecastSnapshot,
    event_time: datetime,
) -> Decimal | None:
    if not snapshot.valid_for_times or not snapshot.hourly_temp_path_f:
        return None
    if event_time < snapshot.provider_run_time:
        return None
    if event_time < snapshot.valid_for_times[0] or event_time > snapshot.valid_for_times[-1]:
        return None
    if event_time == snapshot.valid_for_times[-1]:
        return snapshot.hourly_temp_path_f[-1]
    for index in range(len(snapshot.valid_for_times) - 1):
        left_time = snapshot.valid_for_times[index]
        right_time = snapshot.valid_for_times[index + 1]
        if left_time <= event_time <= right_time:
            if event_time == left_time:
                return snapshot.hourly_temp_path_f[index]
            span_seconds = (right_time - left_time).total_seconds()
            if span_seconds <= 0:
                return snapshot.hourly_temp_path_f[index]
            elapsed_seconds = (event_time - left_time).total_seconds()
            weight = Decimal(str(elapsed_seconds / span_seconds))
            left_value = snapshot.hourly_temp_path_f[index]
            right_value = snapshot.hourly_temp_path_f[index + 1]
            return left_value + ((right_value - left_value) * weight)
    return None


def _observed_day_maxima(
    observations: list[ObservationSnapshot],
    timezone_name: str,
) -> dict[str, Decimal]:
    by_day: dict[str, list[Decimal]] = defaultdict(list)
    for observation in observations:
        if observation.temperature_f is None:
            continue
        by_day[_to_local_date(observation.event_time, timezone_name)].append(observation.temperature_f)
    return {
        day: max(values)
        for day, values in by_day.items()
        if values
    }


def _provider_report(
    *,
    abs_errors: list[float],
    biases: list[float],
    point_sample_count: int,
    run_ids: set[str],
    day_max_biases: list[float] | None = None,
) -> dict[str, Any]:
    mae = fmean(abs_errors) if abs_errors else None
    bias = fmean(biases) if biases else None
    report: dict[str, Any] = {
        "sample_count": len(abs_errors),
        "point_sample_count": point_sample_count,
        "unique_run_count": len(run_ids),
        "mean_abs_error_f": mae,
        "mean_bias_f": bias,
    }
    # 2026-06-12: day-max bias tracked SEPARATELY from hourly point bias.
    # `mean_bias_f` mixes ~24 hourly point errors per snapshot (bias ≈ 0;
    # hourly temps verify well) with 1 day-max error (the −1 to −2°F
    # peak-smoothing bias that drives our markets) — diluting the day-max
    # signal ~24:1. The provider-max correction in the forecast engine
    # needs the day-max-only number; validation showed the diluted value
    # left mid-range p_yes underconfident by ~0.3 (predicted 0.55 →
    # realized 0.87 on 468 settled days).
    if day_max_biases:
        # MEDIAN, not mean (2026-06-12): day-max errors are right-skewed —
        # occasionally a provider runs several °F hot, but the typical day
        # it runs ~1°F cool. The mean washes the typical-day signal out
        # (HOU: GFS mean +1.8 vs median −0.5). The bias correction shifts
        # the whole PMF, so it must track the TYPICAL (median) error.
        # Direct measurement (415 city-days, 10 AM context): blend with
        # mean-based corrections still sat 1.1-1.2°F cool of the realized
        # high, actual landing above the blend 76% of the time.
        report["day_max_bias_f"] = median(day_max_biases)
        report["day_max_sample_count"] = len(day_max_biases)
        # Empirical day-max error spread — the honest per-provider sigma.
        # The engine's heuristic sigma (base + reliability + boundary
        # add-ons) stacked to ~2.9-3.3°F while measured day-max error
        # spread is ~2°F; the over-width compressed mid-range p_yes
        # toward 0.5 (validation v4: predicted 0.55 → realized 0.81).
        report["day_max_sigma_f"] = pstdev(day_max_biases) if len(day_max_biases) >= 2 else None
    return report


def _normalized_weight_map(provider_reports: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    # 2026-06-12 fix: skill-based weighting was effectively UNIFORM.
    # Two compounding causes:
    #   1. score = 1/(1+MAE) — MAE 1.6 vs 4.1 gave only a 2:1 score ratio.
    #   2. max(0.10, ...) floor — with 11 providers the normalized mean is
    #      ~0.09, so nearly every provider fell below the floor and was
    #      raised to exactly 0.10 → all weights identical → ECMWF_AIFS
    #      (MAE 1.57) counted the same as GraphCast (MAE 4.08).
    # New scheme: inverse-MAE-squared (variance weighting — the optimal
    # combination weight for independent unbiased estimators), floor
    # lowered to 0.02 (keeps a provider alive for ensemble diversity
    # without letting noise dominate). build_forecast_distribution
    # re-normalizes by total weight, so the floor needs no renormalize.
    raw_scores: dict[str, float] = {}
    for provider_id, report in provider_reports.items():
        mae = float(report.get("mean_abs_error_f") or 4.0)
        score = 1.0 / max(0.25, mae * mae)
        raw_scores[provider_id] = score
    total_score = sum(raw_scores.values())
    weights: dict[str, str] = {}
    for provider_id in provider_reports:
        normalized = (raw_scores[provider_id] / total_score) if total_score > 0 else 0.0
        sample_count = float(provider_reports[provider_id].get("sample_count") or 0.0)
        # Shrink toward uniform when the sample is thin (<20 samples).
        shrinkage_factor = max(0.0, 1.0 - min(sample_count, 20.0) / 20.0)
        uniform = 1.0 / max(1, len(provider_reports))
        shrunk = (uniform * shrinkage_factor) + (normalized * (1.0 - shrinkage_factor))
        clamped = min(0.80, max(0.02, shrunk))
        weights[provider_id] = f"{clamped:.6f}"
    return weights


def _extract_weight_mapping(
    report: Mapping[str, Any],
    *,
    season_key: str | None,
    lead_bucket: str | None,
) -> Mapping[str, Any] | None:
    if season_key is not None and lead_bucket is not None:
        seasonal = report.get("provider_weights_by_season_lead_bucket")
        if isinstance(seasonal, Mapping):
            season_payload = seasonal.get(season_key)
            if isinstance(season_payload, Mapping):
                bucket_payload = season_payload.get(lead_bucket)
                if isinstance(bucket_payload, Mapping) and bucket_payload:
                    return bucket_payload
    if season_key is not None:
        seasonal = report.get("provider_weights_by_season")
        if isinstance(seasonal, Mapping):
            season_payload = seasonal.get(season_key)
            if isinstance(season_payload, Mapping) and season_payload:
                return season_payload
    weights = report.get("provider_weights")
    return weights if isinstance(weights, Mapping) else None


def _extract_report_mapping(
    report: Mapping[str, Any],
    *,
    season_key: str | None,
    lead_bucket: str | None,
) -> Mapping[str, Any] | None:
    if season_key is not None and lead_bucket is not None:
        seasonal = report.get("provider_reports_by_season_lead_bucket")
        if isinstance(seasonal, Mapping):
            season_payload = seasonal.get(season_key)
            if isinstance(season_payload, Mapping):
                bucket_payload = season_payload.get(lead_bucket)
                if isinstance(bucket_payload, Mapping) and bucket_payload:
                    return bucket_payload
    if season_key is not None:
        seasonal = report.get("provider_reports_by_season")
        if isinstance(seasonal, Mapping):
            season_payload = seasonal.get(season_key)
            if isinstance(season_payload, Mapping) and season_payload:
                return season_payload
    reports = report.get("provider_reports")
    return reports if isinstance(reports, Mapping) else None


def build_provider_reliability_report(
    forecasts: list[ForecastSnapshot],
    observations: list[ObservationSnapshot],
    station: StationReference,
) -> dict[str, Any]:
    observed_maxima = _observed_day_maxima(observations, station.timezone)
    provider_errors: dict[str, list[float]] = defaultdict(list)
    provider_biases: dict[str, list[float]] = defaultdict(list)
    provider_point_samples: dict[str, int] = defaultdict(int)
    provider_run_ids: dict[str, set[str]] = defaultdict(set)
    seasonal_errors: dict[tuple[str, str], list[float]] = defaultdict(list)
    seasonal_biases: dict[tuple[str, str], list[float]] = defaultdict(list)
    seasonal_point_samples: dict[tuple[str, str], int] = defaultdict(int)
    seasonal_run_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    seasonal_lead_errors: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    seasonal_lead_biases: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    seasonal_lead_point_samples: dict[tuple[str, str, str], int] = defaultdict(int)
    seasonal_lead_run_ids: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    # Day-max-only bias tracking (see _provider_report 2026-06-12 note)
    provider_day_max_biases: dict[str, list[float]] = defaultdict(list)
    seasonal_day_max_biases: dict[tuple[str, str], list[float]] = defaultdict(list)
    seasonal_lead_day_max_biases: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    observations_after_run = [
        observation
        for observation in observations
        if observation.temperature_f is not None
    ]

    for snapshot in forecasts:
        run_id = f"{snapshot.provider_id}:{snapshot.provider_run_time.isoformat()}"
        provider_run_ids[snapshot.provider_id].add(run_id)
        for observation in observations_after_run:
            predicted = _interpolated_forecast_value(snapshot, observation.event_time)
            if predicted is None or observation.temperature_f is None:
                continue
            error = float(predicted - observation.temperature_f)
            provider_errors[snapshot.provider_id].append(abs(error))
            provider_biases[snapshot.provider_id].append(error)
            provider_point_samples[snapshot.provider_id] += 1
            season = _season_key(observation.event_time, station.timezone)
            lead_bucket = _lead_bucket_name(
                max(0.0, (observation.event_time - snapshot.provider_run_time).total_seconds() / 3600.0)
            )
            seasonal_errors[(snapshot.provider_id, season)].append(abs(error))
            seasonal_biases[(snapshot.provider_id, season)].append(error)
            seasonal_point_samples[(snapshot.provider_id, season)] += 1
            seasonal_run_ids[(snapshot.provider_id, season)].add(run_id)
            seasonal_lead_errors[(snapshot.provider_id, season, lead_bucket)].append(abs(error))
            seasonal_lead_biases[(snapshot.provider_id, season, lead_bucket)].append(error)
            seasonal_lead_point_samples[(snapshot.provider_id, season, lead_bucket)] += 1
            seasonal_lead_run_ids[(snapshot.provider_id, season, lead_bucket)].add(run_id)
        day_max = _forecast_day_max(snapshot, station.timezone)
        if day_max is None:
            continue
        local_date, predicted_max, max_valid_time = day_max
        realized_max = observed_maxima.get(local_date)
        if realized_max is None:
            continue
        max_error = float(predicted_max - realized_max)
        provider_errors[snapshot.provider_id].append(abs(max_error))
        provider_biases[snapshot.provider_id].append(max_error)
        day_max_season = _season_key(max_valid_time, station.timezone)
        day_max_lead_bucket = _lead_bucket_name(
            max(0.0, (max_valid_time - snapshot.provider_run_time).total_seconds() / 3600.0)
        )
        seasonal_errors[(snapshot.provider_id, day_max_season)].append(abs(max_error))
        seasonal_biases[(snapshot.provider_id, day_max_season)].append(max_error)
        seasonal_lead_errors[(snapshot.provider_id, day_max_season, day_max_lead_bucket)].append(abs(max_error))
        seasonal_lead_biases[(snapshot.provider_id, day_max_season, day_max_lead_bucket)].append(max_error)
        provider_day_max_biases[snapshot.provider_id].append(max_error)
        seasonal_day_max_biases[(snapshot.provider_id, day_max_season)].append(max_error)
        seasonal_lead_day_max_biases[(snapshot.provider_id, day_max_season, day_max_lead_bucket)].append(max_error)

    provider_reports: dict[str, dict[str, Any]] = {}
    for provider_id in sorted(provider_errors):
        provider_reports[provider_id] = _provider_report(
            abs_errors=provider_errors[provider_id],
            biases=provider_biases[provider_id],
            point_sample_count=provider_point_samples.get(provider_id, 0),
            run_ids=provider_run_ids.get(provider_id, set()),
            day_max_biases=provider_day_max_biases.get(provider_id),
        )

    provider_weights = _normalized_weight_map(provider_reports)
    for provider_id, report in provider_reports.items():
        report["reliability_weight"] = float(provider_weights[provider_id])

    provider_reports_by_season: dict[str, dict[str, dict[str, Any]]] = {}
    for provider_id, season in sorted(seasonal_errors):
        season_mapping = provider_reports_by_season.setdefault(season, {})
        season_mapping[provider_id] = _provider_report(
            abs_errors=seasonal_errors[(provider_id, season)],
            biases=seasonal_biases[(provider_id, season)],
            point_sample_count=seasonal_point_samples.get((provider_id, season), 0),
            run_ids=seasonal_run_ids.get((provider_id, season), set()),
            day_max_biases=seasonal_day_max_biases.get((provider_id, season)),
        )
    provider_weights_by_season = {
        season: _normalized_weight_map(reports)
        for season, reports in provider_reports_by_season.items()
    }
    for season, reports in provider_reports_by_season.items():
        for provider_id, provider_report in reports.items():
            provider_report["reliability_weight"] = float(provider_weights_by_season[season][provider_id])

    provider_reports_by_season_lead_bucket: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for provider_id, season, lead_bucket in sorted(seasonal_lead_errors):
        season_mapping = provider_reports_by_season_lead_bucket.setdefault(season, {})
        bucket_mapping = season_mapping.setdefault(lead_bucket, {})
        bucket_mapping[provider_id] = _provider_report(
            abs_errors=seasonal_lead_errors[(provider_id, season, lead_bucket)],
            biases=seasonal_lead_biases[(provider_id, season, lead_bucket)],
            point_sample_count=seasonal_lead_point_samples.get((provider_id, season, lead_bucket), 0),
            run_ids=seasonal_lead_run_ids.get((provider_id, season, lead_bucket), set()),
            day_max_biases=seasonal_lead_day_max_biases.get((provider_id, season, lead_bucket)),
        )
    provider_weights_by_season_lead_bucket = {
        season: {
            lead_bucket: _normalized_weight_map(reports)
            for lead_bucket, reports in season_mapping.items()
        }
        for season, season_mapping in provider_reports_by_season_lead_bucket.items()
    }
    for season, season_mapping in provider_reports_by_season_lead_bucket.items():
        for lead_bucket, reports in season_mapping.items():
            for provider_id, provider_report in reports.items():
                provider_report["reliability_weight"] = float(
                    provider_weights_by_season_lead_bucket[season][lead_bucket][provider_id]
                )

    global_errors = [value for values in provider_errors.values() for value in values]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "station_id": station.station_id,
        "global_sample_count": len(global_errors),
        "global_unique_run_count": sum(len(run_ids) for run_ids in provider_run_ids.values()),
        "global_mean_abs_error_f": (fmean(global_errors) if global_errors else None),
        "provider_reports": provider_reports,
        "provider_weights": provider_weights,
        "provider_reports_by_season": provider_reports_by_season,
        "provider_weights_by_season": provider_weights_by_season,
        "provider_reports_by_season_lead_bucket": provider_reports_by_season_lead_bucket,
        "provider_weights_by_season_lead_bucket": provider_weights_by_season_lead_bucket,
        "sample_sufficient": len(global_errors) >= 24 and sum(len(run_ids) for run_ids in provider_run_ids.values()) >= 4,
    }


def extract_provider_reliability_weights(
    report: Mapping[str, Any] | None,
    *,
    season_key: str | None = None,
    lead_hours: float | None = None,
) -> dict[str, Decimal]:
    if not report:
        return {}
    weights = _extract_weight_mapping(
        report,
        season_key=season_key,
        lead_bucket=_lead_bucket_name(lead_hours) if lead_hours is not None else None,
    )
    if not isinstance(weights, Mapping):
        return {}
    results: dict[str, Decimal] = {}
    for provider_id, value in weights.items():
        try:
            results[str(provider_id)] = Decimal(str(value))
        except Exception:
            continue
    return results


def extract_provider_bias_adjustments(
    report: Mapping[str, Any] | None,
    *,
    season_key: str | None = None,
    lead_hours: float | None = None,
) -> dict[str, Decimal]:
    if not report:
        return {}
    lead_bucket = _lead_bucket_name(lead_hours) if lead_hours is not None else None
    selected_reports = _extract_report_mapping(
        report,
        season_key=season_key,
        lead_bucket=lead_bucket,
    )
    global_reports = _extract_report_mapping(report, season_key=None, lead_bucket=None)
    results: dict[str, Decimal] = {}
    provider_ids = set()
    if isinstance(global_reports, Mapping):
        provider_ids.update(str(key) for key in global_reports.keys())
    if isinstance(selected_reports, Mapping):
        provider_ids.update(str(key) for key in selected_reports.keys())
    for provider_id in sorted(provider_ids):
        provider_report = (
            selected_reports.get(provider_id)
            if isinstance(selected_reports, Mapping)
            else None
        )
        fallback_report = (
            global_reports.get(provider_id)
            if isinstance(global_reports, Mapping)
            else None
        )
        source_report = None
        if isinstance(provider_report, Mapping):
            sample_count = int(provider_report.get("sample_count") or 0)
            point_count = int(provider_report.get("point_sample_count") or 0)
            if sample_count >= 4 or point_count >= 4:
                source_report = provider_report
        if source_report is None and isinstance(fallback_report, Mapping):
            sample_count = int(fallback_report.get("sample_count") or 0)
            point_count = int(fallback_report.get("point_sample_count") or 0)
            if sample_count >= 6 or point_count >= 6:
                source_report = fallback_report
        if source_report is None:
            continue
        # 2026-06-12: prefer day-max-only bias over the diluted mean_bias_f.
        # This adjustment corrects the provider's DAY MAX, so it must be
        # estimated from day-max errors. mean_bias_f mixes ~24 hourly point
        # errors (bias ≈ 0) per 1 day-max error, washing out the −1 to −2°F
        # peak-smoothing bias and leaving mid-range p_yes underconfident by
        # ~0.3 on the 468-day validation replay. Fall back to mean_bias_f
        # only when day-max samples are too thin (< 6).
        raw_bias = None
        day_max_n = int(source_report.get("day_max_sample_count") or 0)
        if day_max_n >= 6 and source_report.get("day_max_bias_f") is not None:
            raw_bias = source_report.get("day_max_bias_f")
        elif source_report.get("mean_bias_f") is not None:
            raw_bias = source_report.get("mean_bias_f")
        if raw_bias is None:
            continue
        try:
            bias = Decimal(str(raw_bias))
        except Exception:
            continue
        # ±3°F clamp: with honest per-bucket day-max biases (−0.5 to −3°F
        # measured across 468 settled city-days) this is a noise bound,
        # not a correction driver.
        results[provider_id] = max(Decimal("-3"), min(Decimal("3"), bias))
    return results


def extract_provider_day_max_sigmas(
    report: Mapping[str, Any] | None,
    *,
    season_key: str | None = None,
    lead_hours: float | None = None,
) -> dict[str, Decimal]:
    """Empirical per-provider day-max error sigma (2026-06-12).

    Replaces the engine's heuristic sigma stack (base + reliability +
    boundary add-ons, which inflated to ~3°F vs ~2°F measured) with the
    observed spread of (predicted day-max − realized high). Selection
    mirrors extract_provider_bias_adjustments: per-(season, lead-bucket)
    first, global fallback, ≥6 day-max samples required. Clamped to
    [1.2, 6.0] °F as a noise bound.
    """
    if not report:
        return {}
    lead_bucket = _lead_bucket_name(lead_hours) if lead_hours is not None else None
    selected_reports = _extract_report_mapping(report, season_key=season_key, lead_bucket=lead_bucket)
    global_reports = _extract_report_mapping(report, season_key=None, lead_bucket=None)
    results: dict[str, Decimal] = {}
    provider_ids = set()
    for mapping in (global_reports, selected_reports):
        if isinstance(mapping, Mapping):
            provider_ids.update(str(k) for k in mapping.keys())
    for provider_id in sorted(provider_ids):
        source = None
        for mapping in (selected_reports, global_reports):
            if not isinstance(mapping, Mapping):
                continue
            candidate = mapping.get(provider_id)
            if (isinstance(candidate, Mapping)
                    and int(candidate.get("day_max_sample_count") or 0) >= 6
                    and candidate.get("day_max_sigma_f") is not None):
                source = candidate
                break
        if source is None:
            continue
        try:
            sigma = Decimal(str(source.get("day_max_sigma_f")))
        except Exception:
            continue
        results[provider_id] = max(Decimal("1.2"), min(Decimal("6"), sigma))
    return results
