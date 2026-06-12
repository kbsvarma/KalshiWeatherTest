from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from kalshi_weather.domain.models import CurrentStateEstimate, PathProgressState, RegimeAssessment


def synoptic_peak_hour_adjustment(current_state: CurrentStateEstimate, base_peak_hour: int) -> int:
    """Return an adjusted peak hour based on the current synoptic state.

    On convective or heavily overcast days the temperature typically peaks
    earlier (before storm onset or before cloud build-up suppresses heating),
    so we shift the effective peak hour earlier to avoid over-estimating the
    reachability of afternoon temperatures.
    """
    shock = current_state.shock_risk_score
    cloud_adj = current_state.cloud_cover_shock_adjustment_f
    storm_adj = current_state.storm_shock_adjustment_f

    if shock >= Decimal("0.65") or storm_adj <= Decimal("-0.8"):
        # Strong convective signal: peak probably happens mid-morning.
        return max(10, base_peak_hour - 3)
    if shock >= Decimal("0.45") or cloud_adj <= Decimal("-1.2"):
        # Moderate cloud/storm signal: peak is 1-2 h earlier.
        return max(11, base_peak_hour - 2)
    if shock >= Decimal("0.30") or cloud_adj <= Decimal("-0.8"):
        return max(12, base_peak_hour - 1)
    return base_peak_hour


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value


def build_regime_assessment(
    city_id: str,
    current_state: CurrentStateEstimate,
    path_state: PathProgressState,
    as_of_time: datetime,
) -> RegimeAssessment:
    active_regime = "CLEAR_STABLE_HEATING"
    haircut = Decimal("0")
    sizing = Decimal("1")
    block_flag = False

    remaining_minutes = path_state.remaining_effective_window_minutes
    if current_state.shock_risk_score >= Decimal("0.50"):
        active_regime = "CONVECTIVE_SHOCK"
        shock_excess = _clamp(
            (current_state.shock_risk_score - Decimal("0.50")) / Decimal("0.50"),
            Decimal("0"),
            Decimal("1"),
        )
        haircut = Decimal("0.20") + (shock_excess * Decimal("0.35"))
        block_flag = path_state.threshold_gap_f <= Decimal("3") and remaining_minutes < 180
    elif current_state.cloud_cover_shock_adjustment_f <= Decimal("-1.5"):
        active_regime = "CLOUD_SUPPRESSION_RISK"
        haircut = Decimal("0.20")
    elif current_state.marine_intrusion_adjustment_f <= Decimal("-2"):
        active_regime = "MARINE_INTRUSION"
        haircut = Decimal("0.30")
        sizing = Decimal("0.5")
    elif (
        not path_state.threshold_already_crossed_flag
        and remaining_minutes < 90
    ):
        active_regime = "LATE_DAY_DECAY"
        if remaining_minutes < 60:
            haircut = Decimal("0.40")
            # 2026-05-28 fix: previously hard-blocked unconditionally when
            # <60 min remained. That killed 71% of decisions and removed the
            # entire greater-yes longshot lane (the +86% ROI strategy).
            # The 40% haircut already discounts EV; p_model itself already
            # encodes reachability. Only hard-block when BOTH reachability
            # is genuinely dead AND threshold is meaningfully far — same
            # conditional logic as the 60-90 min branch, but stricter.
            block_flag = (
                path_state.reachability_score <= Decimal("0.30")
                and path_state.threshold_gap_f >= Decimal("4")
            )
            sizing = Decimal("0.70")
        else:
            haircut = Decimal("0.20")
            block_flag = (
                path_state.reachability_score <= Decimal("0.50")
                and path_state.threshold_gap_f >= Decimal("3")
            )
            sizing = Decimal("0.85")

    return RegimeAssessment(
        city_id=city_id,
        as_of_time=as_of_time,
        active_regime=active_regime,
        regime_scores={active_regime: Decimal("1")},
        feature_values={
            "shock_risk_score": current_state.shock_risk_score,
            "threshold_gap_f": path_state.threshold_gap_f,
            "remaining_minutes": Decimal(path_state.remaining_effective_window_minutes),
        },
        block_flag=block_flag,
        haircut_value=haircut,
        sizing_multiplier=sizing,
        explanation_codes=(active_regime.lower(),),
    )
