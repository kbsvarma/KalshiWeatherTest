from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from kalshi_weather.analytics import QualificationScorecard, build_qualification_scorecard
from kalshi_weather.analytics.live_gating import NYC_MVP_LIVE_PROFILE, STRICT_PRODUCTION_PROFILE
from kalshi_weather.domain.enums import QualificationState
from kalshi_weather.domain.models import CityQualificationState

NOWCAST_SHADOW_NEAR_BASELINE_TOLERANCE = Decimal("1.03")
# 2026-05-17 fix: nowcast-baseline failure alone is NOT enough to demote.
# The qualification engine was over-gating on nowcast skill (predicting
# next-hour temperature) when our trades settle on the DAILY HIGH. NYC
# was being demoted despite having the most accurate NWS daily-high
# forecast in the fleet (1.89°F MAE vs 2.70°F average).
# Now: nowcast failure only triggers demotion if the best available
# daily-high forecast provider ALSO has weak MAE (>5°F). If even one
# provider is good, we have a usable signal and stay in shadow.
BEST_PROVIDER_MAE_DEMOTE_THRESHOLD_F = Decimal("5.0")


def _settlement_signal_is_weak(
    settlement_error_summary: dict[str, object] | None,
) -> bool:
    """Return True if the city's best-available daily-high forecast
    provider has MAE > the demotion threshold. Returns False (= signal
    is OK) when data is insufficient — we don't punish young data."""
    if not settlement_error_summary:
        return False
    n = int(settlement_error_summary.get("sample_count") or 0)
    if n < 10:  # too few samples — don't gate on this
        return False
    best_mae = settlement_error_summary.get("best_provider_mae_f")
    if best_mae is None:
        return False
    try:
        return Decimal(str(best_mae)) > BEST_PROVIDER_MAE_DEMOTE_THRESHOLD_F
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class QualificationUpdate:
    next_state: QualificationState
    reasons: tuple[str, ...]
    scorecard: QualificationScorecard


def _nowcast_material_failure(nowcast_report: dict[str, object] | None) -> bool:
    if not nowcast_report or not bool(nowcast_report.get("sample_sufficient")):
        return False
    if bool(nowcast_report.get("redesign_required")):
        return True
    if bool(nowcast_report.get("beats_baselines")):
        return False
    model_mae = nowcast_report.get("model_mae_f")
    best_baseline = nowcast_report.get("best_available_baseline_mae_f")
    if model_mae is None or best_baseline is None:
        return True
    try:
        model_mae_dec = Decimal(str(model_mae))
        baseline_dec = Decimal(str(best_baseline))
    except Exception:
        return True
    if baseline_dec <= 0:
        return True
    return model_mae_dec > (baseline_dec * NOWCAST_SHADOW_NEAR_BASELINE_TOLERANCE)


def _nowcast_near_baseline(nowcast_report: dict[str, object] | None) -> bool:
    if not nowcast_report or not bool(nowcast_report.get("sample_sufficient")):
        return False
    if bool(nowcast_report.get("beats_baselines")) or bool(nowcast_report.get("redesign_required")):
        return False
    return not _nowcast_material_failure(nowcast_report)


def recommend_qualification_update(
    current_state: CityQualificationState,
    shadow_report: dict[str, object],
    drift_report: dict[str, object],
    settled_validation_count: int,
    *,
    city_id: str | None = None,
    profile: str = STRICT_PRODUCTION_PROFILE,
    decision_payloads: list[dict[str, object]] | None = None,
    position_history_payloads: list[dict[str, object]] | None = None,
    settlement_error_summary: dict[str, object] | None = None,
    settlement_summary: dict[str, object] | None = None,
    nowcast_report: dict[str, object] | None = None,
    calibration_report: dict[str, object] | None = None,
) -> QualificationUpdate:
    scorecard = build_qualification_scorecard(
        list(decision_payloads or []),
        position_history_payloads=list(position_history_payloads or []),
        shadow_report=shadow_report,
        drift_report=drift_report,
        settlement_summary=settlement_summary,
        nowcast_report=nowcast_report,
        calibration_report=calibration_report,
    )
    reasons: list[str] = []
    next_state = current_state.state

    average_excess_lag = drift_report.get("average_observation_excess_lag_minutes")
    p95_excess_lag = drift_report.get("p95_observation_excess_lag_minutes")
    shadow_fill_count = int(shadow_report.get("shadow_fill_count") or 0)
    taker_allowed_count = int(shadow_report.get("taker_allowed_count") or 0)
    settled_position_count = int(shadow_report.get("settled_position_count") or 0)
    lower_80 = shadow_report.get("lower_80_confidence_executable_ev")
    settlement_ready = (
        bool(settlement_summary and settlement_summary.get("eligible_for_shadow_only"))
        if settlement_summary is not None
        else settled_validation_count >= 50
    )

    effective_city_id = str(city_id or current_state.city_id).lower()
    if profile == NYC_MVP_LIVE_PROFILE and effective_city_id == "nyc":
        resolved_count = int(settlement_summary.get("resolved_validation_count") or 0) if settlement_summary else 0
        matched_count = int(settlement_summary.get("matched_market_count") or 0) if settlement_summary else 0
        revision_resolved_count = int(settlement_summary.get("revision_resolved_count") or 0) if settlement_summary else 0
        unresolved_count = int(settlement_summary.get("unresolved_entry_count") or 0) if settlement_summary else 0
        critical_count = int(settlement_summary.get("critical_mismatch_count") or 0) if settlement_summary else 0
        global_sample_count = int(calibration_report.get("global_sample_count") or 0) if calibration_report else 0
        global_unique_run_count = int(calibration_report.get("global_unique_run_count") or 0) if calibration_report else 0
        global_mae = calibration_report.get("global_mean_abs_error_f") if calibration_report else None
        settled_position_count = int(shadow_report.get("settled_position_count") or 0)
        settled_win_rate = shadow_report.get("settled_win_rate")

        if critical_count > 0:
            return QualificationUpdate(
                QualificationState.OBSERVE_ONLY,
                ("settlement_critical_mismatch",),
                scorecard,
            )
        if (
            average_excess_lag is not None
            and p95_excess_lag is not None
            and (float(average_excess_lag) > 12 or float(p95_excess_lag) > 20)
        ):
            return QualificationUpdate(
                QualificationState.OBSERVE_ONLY,
                ("observation_lag_unstable",),
                scorecard,
            )
        # Only demote on nowcast failure when daily-high forecast is ALSO weak.
        # NYC's nowcast underperforms persistence but its daily-high MAE is
        # the best in the fleet; the old gate was over-aggressive.
        if _nowcast_material_failure(nowcast_report) and _settlement_signal_is_weak(
            settlement_error_summary
        ):
            return QualificationUpdate(
                QualificationState.OBSERVE_ONLY,
                ("nowcast_baseline_failure_and_settlement_weak",),
                scorecard,
            )
        if (
            resolved_count < 50
            or matched_count != resolved_count
            or revision_resolved_count != resolved_count
            or unresolved_count != 0
        ):
            return QualificationUpdate(
                QualificationState.OBSERVE_ONLY,
                ("nyc_mvp_settlement_validation_incomplete",),
                scorecard,
            )
        if (
            global_sample_count < 10
            or global_unique_run_count < 2
            or global_mae is None
            or float(global_mae) > 1.5
        ):
            return QualificationUpdate(
                QualificationState.SHADOW_ONLY,
                ("nyc_mvp_calibration_preliminary",),
                scorecard,
            )
        if (
            int(shadow_report.get("decision_count") or 0) >= 75
            and taker_allowed_count >= 15
            and lower_80 is not None
            and float(lower_80) > 0.02
            and settled_position_count >= 25
            and (shadow_report.get("total_settled_pnl_dollars") or 0) > 0
            and settled_win_rate is not None
            and float(settled_win_rate) > 0.55
            and scorecard.nowcast_score >= Decimal("0.80")
            and scorecard.calibration_score >= Decimal("0.30")
            and scorecard.market_depth_score >= Decimal("0.25")
        ):
            return QualificationUpdate(
                QualificationState.LIVE_PILOT,
                ("nyc_mvp_live_profile_ready", "nyc_single_city_cap_only"),
                scorecard,
            )
        return QualificationUpdate(
            QualificationState.SHADOW_ONLY,
            ("nyc_mvp_shadow_only_until_shadow_depth_improves",),
            scorecard,
        )

    # 2026-05-17 fix: don't demote on missing settlement_validations entries
    # when we have strong provider_errors evidence the city forecasts work.
    # The 10 cities added in May had 0 corpus entries (original 8 had 100)
    # which was conflating "no corpus built" with "validation failed".
    # We demote only if BOTH the legacy corpus is thin AND the actual
    # daily-high forecast MAE is weak.
    if settled_validation_count < 50 and _settlement_signal_is_weak(
        settlement_error_summary
    ):
        return QualificationUpdate(
            QualificationState.OBSERVE_ONLY,
            ("settlement_validation_below_50_and_forecast_weak",),
            scorecard,
        )
    if (
        settled_validation_count < 50
        and (not settlement_error_summary
             or int(settlement_error_summary.get("sample_count") or 0) < 10)
    ):
        # No corpus AND no real settlements yet — can't confirm reliability.
        # Stay in shadow rather than demoting to observe-only.
        return QualificationUpdate(
            QualificationState.SHADOW_ONLY,
            ("settlement_validation_pending",),
            scorecard,
        )

    # Same logic as the count gate above — if the legacy validation corpus
    # isn't built but real provider_errors look fine, stay in shadow.
    if not settlement_ready and _settlement_signal_is_weak(settlement_error_summary):
        return QualificationUpdate(
            QualificationState.OBSERVE_ONLY,
            ("settlement_validation_incomplete_and_forecast_weak",),
            scorecard,
        )
    if (
        not settlement_ready
        and (not settlement_error_summary
             or int(settlement_error_summary.get("sample_count") or 0) < 10)
    ):
        return QualificationUpdate(
            QualificationState.SHADOW_ONLY,
            ("settlement_validation_pending",),
            scorecard,
        )

    if settlement_summary and int(settlement_summary.get("critical_mismatch_count") or 0) > 0:
        return QualificationUpdate(
            QualificationState.OBSERVE_ONLY,
            ("settlement_critical_mismatch",),
            scorecard,
        )

    if (
        average_excess_lag is not None
        and p95_excess_lag is not None
        and (float(average_excess_lag) > 12 or float(p95_excess_lag) > 20)
    ):
        return QualificationUpdate(
            QualificationState.OBSERVE_ONLY,
            ("observation_lag_unstable",),
            scorecard,
        )

    if (
        calibration_report
        and bool(calibration_report.get("sample_sufficient"))
        and scorecard.calibration_score < Decimal("0.55")
    ):
        return QualificationUpdate(
            QualificationState.SHADOW_ONLY,
            ("forecast_calibration_shadow_probe",),
            scorecard,
        )

    # Same gate as above for non-NYC profiles: only treat nowcast failure
    # as a shadow probe trigger if daily-high signal is ALSO weak.
    if _nowcast_material_failure(nowcast_report) and _settlement_signal_is_weak(
        settlement_error_summary
    ):
        return QualificationUpdate(
            QualificationState.SHADOW_ONLY,
            ("nowcast_baseline_shadow_probe_and_settlement_weak",),
            scorecard,
        )

    if shadow_fill_count >= 20 and lower_80 is not None and float(lower_80) <= 0:
        return QualificationUpdate(
            QualificationState.OBSERVE_ONLY,
            ("shadow_ev_negative",),
            scorecard,
        )

    live_pilot_ready = (
        shadow_fill_count >= 100
        and settled_position_count >= 20
        and lower_80 is not None
        and float(lower_80) > 0
        and scorecard.calibration_score >= Decimal("0.70")
        and scorecard.nowcast_score >= Decimal("0.70")
        and scorecard.market_depth_score >= Decimal("0.60")
        and scorecard.slippage_score >= Decimal("0.60")
        and scorecard.shadow_ev_score >= Decimal("0.70")
        and scorecard.drawdown_score >= Decimal("0.55")
    )
    shadow_qualified_ready = (
        shadow_fill_count >= 20
        and taker_allowed_count >= 20
        and lower_80 is not None
        and float(lower_80) > 0
        and scorecard.calibration_score >= Decimal("0.45")
        and scorecard.market_depth_score >= Decimal("0.40")
        and scorecard.slippage_score >= Decimal("0.35")
        and scorecard.shadow_ev_score >= Decimal("0.45")
        and (
            nowcast_report is None
            or (not bool(nowcast_report.get("sample_sufficient")))
            or bool(nowcast_report.get("beats_baselines"))
        )
    )

    if live_pilot_ready:
        next_state = QualificationState.LIVE_PILOT
        reasons.extend(("live_pilot_metrics_positive", "drawdown_controlled"))
    elif shadow_qualified_ready:
        next_state = QualificationState.SHADOW_QUALIFIED
        reasons.append("shadow_metrics_positive")
    else:
        next_state = QualificationState.SHADOW_ONLY
        if _nowcast_near_baseline(nowcast_report):
            reasons.append("nowcast_near_baseline_shadow_probe")
        if shadow_fill_count < 20 or taker_allowed_count < 20:
            reasons.append("insufficient_shadow_sample")
        if scorecard.market_depth_score < Decimal("0.40"):
            reasons.append("market_depth_weak")
        if scorecard.slippage_score < Decimal("0.35"):
            reasons.append("slippage_profile_weak")
        if scorecard.calibration_score < Decimal("0.45"):
            reasons.append("forecast_calibration_preliminary")

    return QualificationUpdate(next_state, tuple(reasons), scorecard)


def apply_qualification_update(
    current_state: CityQualificationState,
    update: QualificationUpdate,
) -> CityQualificationState:
    now = datetime.now(timezone.utc)
    return CityQualificationState(
        city_id=current_state.city_id,
        state=update.next_state,
        effective_from=now,
        effective_to=None,
        settlement_validation_score=update.scorecard.settlement_validation_score,
        calibration_score=update.scorecard.calibration_score,
        nowcast_score=update.scorecard.nowcast_score,
        path_score=update.scorecard.path_score,
        market_depth_score=update.scorecard.market_depth_score,
        slippage_score=update.scorecard.slippage_score,
        shadow_ev_score=update.scorecard.shadow_ev_score,
        drawdown_score=update.scorecard.drawdown_score,
        promotion_reasons=update.reasons if update.next_state != current_state.state else current_state.promotion_reasons,
        demotion_reasons=update.reasons if update.next_state == QualificationState.OBSERVE_ONLY else current_state.demotion_reasons,
    )
