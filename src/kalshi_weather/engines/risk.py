from __future__ import annotations

from decimal import Decimal

from kalshi_weather.domain.enums import QualificationState, RiskState
from kalshi_weather.domain.models import CurrentStateEstimate, RiskDecision, TradabilityAssessment
from kalshi_weather.engines.ev import DecisionThresholds


# Raised 2026-05-17 from 1.80 → 5.0 after auditing 1086 morning decisions
# showed `portfolio_risk_limit` blocking 600 of them. The 1.80 cap was
# calibrated for the old single-position-per-city era; after enabling
# multi-bracket-per-city (up to 3 markets/city × 18 cities = 54 possible
# positions), the risk-unit sqrt() math meant 7 open positions ALREADY
# exceeded 1.80 and no new bets could pass. The hard $15/day USD cap at
# live_execution.py plus the 3-markets-per-city dedup are the real
# concentration controls; this limit's job is to catch pathological
# correlation cases, not to block every cycle. 5.0 = sqrt(25) which lines
# up with the rough max bet count ($15 / $0.60 avg ≈ 25 contracts).
PORTFOLIO_RISK_LIMIT = Decimal("5.0")


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
