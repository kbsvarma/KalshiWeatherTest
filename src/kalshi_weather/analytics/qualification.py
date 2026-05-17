from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _clamp01(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values) / Decimal(len(values))


@dataclass(frozen=True, slots=True)
class QualificationScorecard:
    settlement_validation_score: Decimal
    calibration_score: Decimal
    nowcast_score: Decimal
    path_score: Decimal
    market_depth_score: Decimal
    slippage_score: Decimal
    shadow_ev_score: Decimal
    drawdown_score: Decimal


def _settlement_validation_score(settlement_summary: Mapping[str, Any] | None) -> Decimal:
    if not settlement_summary:
        return Decimal("0")
    resolved = int(settlement_summary.get("resolved_validation_count") or 0)
    matched = int(settlement_summary.get("matched_market_count") or 0)
    supportable = int(settlement_summary.get("supportable_market_count") or 0)
    unresolved = int(settlement_summary.get("unresolved_entry_count") or 0)
    critical = int(settlement_summary.get("critical_mismatch_count") or 0)
    if critical > 0:
        return Decimal("0")
    sample_factor = _clamp01(Decimal(resolved) / Decimal("50")) if resolved > 0 else Decimal("0")
    match_ratio = _clamp01(Decimal(matched) / Decimal(resolved)) if resolved > 0 else Decimal("0")
    unresolved_penalty = (
        _clamp01(Decimal("1") - (Decimal(unresolved) / Decimal(supportable)))
        if supportable > 0
        else (Decimal("1") if resolved > 0 else Decimal("0"))
    )
    return _clamp01(sample_factor * match_ratio * unresolved_penalty)


def _calibration_score(calibration_report: Mapping[str, Any] | None) -> Decimal:
    if not calibration_report:
        return Decimal("0")
    sample_count = int(calibration_report.get("global_sample_count") or 0)
    mae = _decimal(calibration_report.get("global_mean_abs_error_f"))
    provider_reports = calibration_report.get("provider_reports")
    provider_mapping = provider_reports if isinstance(provider_reports, Mapping) else {}
    bias_values = [
        abs(_decimal(report.get("mean_bias_f")) or Decimal("0"))
        for report in provider_mapping.values()
        if isinstance(report, Mapping) and report.get("mean_bias_f") is not None
    ]
    sample_factor = _clamp01(Decimal(sample_count) / Decimal("24")) if sample_count > 0 else Decimal("0")
    mae_score = _clamp01(Decimal("1") - ((mae or Decimal("8")) / Decimal("6")))
    bias_score = _clamp01(Decimal("1") - ((_mean(bias_values) or Decimal("5")) / Decimal("4")))
    provider_diversity = _clamp01(Decimal(len(provider_mapping)) / Decimal("2")) if provider_mapping else Decimal("0")
    blended = (
        (Decimal("0.60") * mae_score)
        + (Decimal("0.25") * bias_score)
        + (Decimal("0.15") * provider_diversity)
    )
    return _clamp01(sample_factor * blended)


def _nowcast_score(nowcast_report: Mapping[str, Any] | None) -> Decimal:
    if not nowcast_report:
        return Decimal("0")
    if bool(nowcast_report.get("redesign_required")):
        return Decimal("0")
    sample_count = int(nowcast_report.get("sample_count") or 0)
    required_sample = int(nowcast_report.get("required_sample") or 24)
    model_mae = _decimal(nowcast_report.get("model_mae_f"))
    best_baseline = _decimal(nowcast_report.get("best_available_baseline_mae_f"))
    sample_factor = (
        _clamp01(Decimal(sample_count) / Decimal(required_sample))
        if required_sample > 0 and sample_count > 0
        else Decimal("0")
    )
    if model_mae is not None and model_mae > 0 and best_baseline is not None:
        performance_score = _clamp01(best_baseline / model_mae)
    else:
        performance_score = Decimal("0")
    shock_buckets = nowcast_report.get("shock_buckets")
    bucket_mapping = shock_buckets if isinstance(shock_buckets, Mapping) else {}
    populated_buckets = sum(
        1
        for bucket in bucket_mapping.values()
        if isinstance(bucket, Mapping) and int(bucket.get("count") or 0) > 0
    )
    shock_coverage = _clamp01(Decimal(populated_buckets) / Decimal("3")) if bucket_mapping else Decimal("0")
    blended = (Decimal("0.80") * performance_score) + (Decimal("0.20") * shock_coverage)
    return _clamp01(sample_factor * blended)


def _path_score(decision_payloads: list[dict[str, Any]]) -> Decimal:
    valid = 0
    coherent = 0
    for payload in decision_payloads:
        path_state = payload.get("path_state", {})
        p_yes = _decimal(path_state.get("p_yes"))
        threshold_gap = _decimal(path_state.get("threshold_gap_f"))
        reachability = _decimal(path_state.get("reachability_score"))
        if p_yes is None or threshold_gap is None or reachability is None:
            continue
        valid += 1
        is_coherent = Decimal("0") <= p_yes <= Decimal("1") and Decimal("0") <= reachability <= Decimal("1")
        if threshold_gap <= Decimal("0") and p_yes < Decimal("0.45"):
            is_coherent = False
        if threshold_gap >= Decimal("8") and reachability <= Decimal("0.25") and p_yes > Decimal("0.55"):
            is_coherent = False
        if reachability >= Decimal("0.75") and p_yes < Decimal("0.30"):
            is_coherent = False
        if reachability <= Decimal("0.15") and p_yes > Decimal("0.70"):
            is_coherent = False
        if is_coherent:
            coherent += 1
    if valid == 0:
        return Decimal("0")
    sample_factor = _clamp01(Decimal(valid) / Decimal("24"))
    coherence_ratio = Decimal(coherent) / Decimal(valid)
    return _clamp01(sample_factor * coherence_ratio)


def _market_depth_score(decision_payloads: list[dict[str, Any]]) -> Decimal:
    composites: list[Decimal] = []
    for payload in decision_payloads:
        micro = payload.get("microstructure_summary", {})
        risk = payload.get("risk_summary", {})
        tradability = _decimal(micro.get("max_taker_tradability_score")) or Decimal("0")
        quote = _decimal(micro.get("max_quote_stability_score"))
        if quote is None:
            quote = _decimal(micro.get("selected_quote_stability_score")) or Decimal("0")
        ghost = _decimal(micro.get("mean_ghost_liquidity_ratio"))
        if ghost is None:
            ghost = Decimal("1")
        candidate_count = int(risk.get("taker_candidate_count") or 0)
        candidate_score = _clamp01(Decimal(candidate_count) / Decimal("2"))
        composite = (
            (Decimal("0.45") * tradability)
            + (Decimal("0.25") * quote)
            + (Decimal("0.20") * _clamp01(Decimal("1") - ghost))
            + (Decimal("0.10") * candidate_score)
        )
        composites.append(_clamp01(composite))
    average = _mean(composites)
    if average is None:
        return Decimal("0")
    sample_factor = _clamp01(Decimal(len(composites)) / Decimal("24"))
    return _clamp01(sample_factor * average)


def _slippage_score(decision_payloads: list[dict[str, Any]]) -> Decimal:
    slippages: list[Decimal] = []
    adverse_penalties: list[Decimal] = []
    for payload in decision_payloads:
        edge = payload.get("edge_summary", {})
        slippage = _decimal(edge.get("selected_slippage_cost"))
        adverse = _decimal(edge.get("selected_adverse_selection_penalty"))
        if slippage is not None:
            slippages.append(slippage)
        if adverse is not None:
            adverse_penalties.append(adverse)
    if not slippages and not adverse_penalties:
        return Decimal("0")
    mean_slippage = _mean(slippages) or Decimal("0.05")
    mean_adverse = _mean(adverse_penalties) or Decimal("0.06")
    sample_factor = _clamp01(Decimal(max(len(slippages), len(adverse_penalties))) / Decimal("20"))
    slippage_component = _clamp01(Decimal("1") - (mean_slippage / Decimal("0.03")))
    adverse_component = _clamp01(Decimal("1") - (mean_adverse / Decimal("0.05")))
    blended = (Decimal("0.65") * slippage_component) + (Decimal("0.35") * adverse_component)
    return _clamp01(sample_factor * blended)


def _shadow_ev_score(shadow_report: Mapping[str, Any] | None) -> Decimal:
    if not shadow_report:
        return Decimal("0")
    shadow_fill_count = int(shadow_report.get("shadow_fill_count") or 0)
    lower_80 = _decimal(shadow_report.get("lower_80_confidence_executable_ev")) or Decimal("0")
    mean_settled = _decimal(shadow_report.get("mean_settled_pnl_dollars"))
    mean_residual = _decimal(shadow_report.get("mean_settled_calibration_residual_dollars"))
    sample_factor = _clamp01(Decimal(shadow_fill_count) / Decimal("20")) if shadow_fill_count > 0 else Decimal("0")
    lower_80_score = _clamp01(lower_80 / Decimal("0.02")) if lower_80 > 0 else Decimal("0")
    realized_score = _clamp01((mean_settled or lower_80) / Decimal("0.02")) if (mean_settled or lower_80) > 0 else Decimal("0")
    residual_score = Decimal("0.5")
    if mean_residual is not None:
        residual_score = _clamp01((mean_residual / Decimal("0.02")) + Decimal("0.5"))
    blended = (
        (Decimal("0.60") * lower_80_score)
        + (Decimal("0.25") * realized_score)
        + (Decimal("0.15") * residual_score)
    )
    return _clamp01(sample_factor * blended)


def _drawdown_score(position_history_payloads: list[dict[str, Any]]) -> Decimal:
    closed_positions = [
        payload
        for payload in sorted(position_history_payloads, key=lambda item: str(item.get("recorded_at") or ""))
        if payload.get("lifecycle_status") == "CLOSED"
    ]
    if not closed_positions:
        return Decimal("0")
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for payload in closed_positions:
        cumulative += _decimal(payload.get("settled_pnl_dollars")) or Decimal("0")
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    sample_factor = _clamp01(Decimal(len(closed_positions)) / Decimal("10"))
    drawdown_component = _clamp01(Decimal("1") - (max_drawdown / Decimal("1.00")))
    return _clamp01(sample_factor * drawdown_component)


def build_qualification_scorecard(
    decision_payloads: list[dict[str, Any]],
    *,
    position_history_payloads: list[dict[str, Any]] | None = None,
    shadow_report: Mapping[str, Any] | None = None,
    drift_report: Mapping[str, Any] | None = None,
    settlement_summary: Mapping[str, Any] | None = None,
    nowcast_report: Mapping[str, Any] | None = None,
    calibration_report: Mapping[str, Any] | None = None,
) -> QualificationScorecard:
    del drift_report
    return QualificationScorecard(
        settlement_validation_score=_settlement_validation_score(settlement_summary),
        calibration_score=_calibration_score(calibration_report),
        nowcast_score=_nowcast_score(nowcast_report),
        path_score=_path_score(decision_payloads),
        market_depth_score=_market_depth_score(decision_payloads),
        slippage_score=_slippage_score(decision_payloads),
        shadow_ev_score=_shadow_ev_score(shadow_report),
        drawdown_score=_drawdown_score(position_history_payloads or []),
    )
