from __future__ import annotations

from decimal import Decimal

from kalshi_weather.domain.enums import QualificationState, RiskState
from kalshi_weather.domain.models import CurrentStateEstimate, RiskDecision, TradabilityAssessment
from kalshi_weather.engines.ev import DecisionThresholds


# Raised 2026-05-18 from 5.0 → 50.0 after the user (correctly) called out
# that the daily $/cap (15.0 in run_weather_cycle.sh) is the operative
# spending guardrail. With 27 open positions overnight the risk-unit
# computation hit 12.38 — well over 5.0 — and was hard-vetoing every
# new evaluation, even though only $1.06 had been spent today out of $15.
# Net effect: a "secondary" gate was functionally overriding the primary
# spending policy.
#
# 50.0 is high enough that it's effectively non-binding under the current
# 3-markets-per-city × 20-cities × $0.60 contracts model (worst case ~60
# positions → sqrt(60) ≈ 7.7 units). It still trips on a true pathological
# fully-correlated blow-up (e.g. 200+ same-side positions on the same
# regime), which is the actual scenario this gate was designed for.
#
# Cap history:
#   2026-05-16: 1.80   (single-position-per-city era)
#   2026-05-17: 5.0    (sized for ~25 contracts on $15/day)
#   2026-05-18: 50.0   (daily $/cap is the real governor; this just
#                       catches catastrophic correlation cases)
PORTFOLIO_RISK_LIMIT = Decimal("50.0")


def build_risk_decision(
    qualification_state: QualificationState,
    current_state: CurrentStateEstimate,
    tradability: TradabilityAssessment,
    thresholds: DecisionThresholds,
    raw_edge: Decimal,
    executable_ev: Decimal,
    friction_to_edge_ratio: Decimal,
    portfolio_risk_units_value: Decimal = Decimal("0"),
    active_kill_switch: bool = False,
) -> RiskDecision:
    veto_reasons: list[str] = []
    hard_halt = False
    portfolio_ok = portfolio_risk_units_value <= PORTFOLIO_RISK_LIMIT
    qualification_ok = qualification_state in {
        QualificationState.SHADOW_ONLY,
        QualificationState.SHADOW_QUALIFIED,
        QualificationState.LIVE_PILOT,
    }
    if active_kill_switch:
        hard_halt = True
        veto_reasons.append("manual_kill_switch")
    if qualification_state == QualificationState.DISABLED:
        hard_halt = True
        veto_reasons.append("city_disabled")
    if not qualification_ok:
        veto_reasons.append("qualification_block")
    if current_state.observation_excess_lag_minutes > 20:
        hard_halt = True
        veto_reasons.append("observation_lag_halt")
    if tradability.block_reasons:
        veto_reasons.extend(tradability.block_reasons)
    if raw_edge < thresholds.min_raw_edge:
        veto_reasons.append("raw_edge_below_floor")
    if executable_ev < thresholds.min_executable_ev:
        veto_reasons.append("insufficient_executable_ev")
    if friction_to_edge_ratio > thresholds.max_friction_to_edge_ratio:
        veto_reasons.append("friction_overload")
    if not portfolio_ok:
        veto_reasons.append("portfolio_risk_limit")

    risk_state = RiskState.ALLOW
    if hard_halt:
        risk_state = RiskState.HARD_HALT
    elif veto_reasons:
        risk_state = RiskState.SOFT_BLOCK

    return RiskDecision(
        risk_state=risk_state,
        hard_halt_flag=hard_halt,
        city_halt_flag=hard_halt and not active_kill_switch,
        global_halt_flag=active_kill_switch,
        exposure_ok=portfolio_ok,
        correlation_ok=portfolio_ok,
        source_health_ok=current_state.observation_excess_lag_minutes <= 20,
        qualification_ok=qualification_ok,
        veto_reasons=tuple(veto_reasons),
        manual_override_state=None,
    )
