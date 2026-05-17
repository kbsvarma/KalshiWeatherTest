from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from kalshi_weather.domain.models import EdgeEstimate, MicrostructureAssessment, PathProgressState, RegimeAssessment


@dataclass(frozen=True, slots=True)
class DecisionThresholds:
    min_raw_edge: Decimal = Decimal("0.03")
    min_executable_ev: Decimal = Decimal("0.015")
    max_friction_to_edge_ratio: Decimal = Decimal("0.60")


def kelly_contract_size(
    executable_ev: Decimal,
    *,
    min_contracts: int = 1,
    max_contracts: int = 3,
    ev_step: Decimal = Decimal("0.020"),
) -> int:
    """Fractional Kelly sizing: 1 contract at min_ev, +1 for each ev_step above that.

    Examples with ev_step=0.020:
        ev=0.015 → 1 contract (below first step)
        ev=0.025 → 1 contract (between floor and first step)
        ev=0.040 → 2 contracts
        ev=0.060 → 3 contracts (capped at max_contracts)
    """
    if executable_ev <= Decimal("0"):
        return 0
    steps_above_floor = int(executable_ev / ev_step)
    return max(min_contracts, min(max_contracts, 1 + steps_above_floor))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _fee_cost(price: Decimal, quantity: Decimal, fee_multiplier: int | None) -> Decimal:
    multiplier = Decimal(str(fee_multiplier or 1))
    return Decimal("0.07") * multiplier * quantity * price * (Decimal("1") - price)


def _portfolio_haircut(portfolio_risk_units_value: Decimal) -> Decimal:
    return _clamp(
        (portfolio_risk_units_value - Decimal("0.5")) * Decimal("0.10"),
        Decimal("0"),
        Decimal("0.15"),
    )


def _provider_disagreement_haircut(
    provider_spread_f: Decimal,
    provider_threshold_straddle_f: Decimal,
) -> Decimal:
    effective_spread = (
        provider_threshold_straddle_f
        if provider_threshold_straddle_f > Decimal("0")
        else provider_spread_f * Decimal("0.35")
    )
    if effective_spread < Decimal("1"):
        return Decimal("0")
    if effective_spread < Decimal("2"):
        return Decimal("0.05")
    if effective_spread <= Decimal("4"):
        return Decimal("0.15")
    return Decimal("0.25")


def compute_edge_estimate(
    market_ticker: str,
    side: str,
    quantity: Decimal,
    p_yes: Decimal,
    executable_price: Decimal,
    micro: MicrostructureAssessment,
    path_state: PathProgressState,
    regime: RegimeAssessment,
    fee_multiplier: int | None = 1,
    execution_style: str = "taker",
    provider_spread_f: Decimal = Decimal("0"),
    provider_threshold_straddle_f: Decimal = Decimal("0"),
    portfolio_risk_units_value: Decimal = Decimal("0"),
) -> EdgeEstimate:
    if side == "yes":
        p_model = p_yes
        p_market_exec = executable_price
    else:
        p_model = Decimal("1") - p_yes
        p_market_exec = executable_price

    raw_edge = p_model - p_market_exec
    # Kalshi charges the creation fee only when the contract is created (at entry).
    # At settlement the winning side receives $1 and the losing side $0 — the fee
    # formula p*(1-p) evaluates to 0 at both boundaries, so settlement incurs no fee.
    # For positions held to settlement, total fee = entry fee only.
    # We add a small early-exit allowance (50% of entry fee) to cover the rare case
    # where hold_ev turns negative and the bot exits the secondary market before close.
    entry_fee = _fee_cost(executable_price, quantity, fee_multiplier)
    early_exit_allowance = entry_fee * Decimal("0.50")
    fee_cost = entry_fee + early_exit_allowance
    slippage_cost = micro.slippage_cost if execution_style == "taker" else Decimal("0")
    adverse_selection_penalty = (
        micro.taker_adverse_selection_penalty
        if execution_style == "taker"
        else micro.maker_adverse_selection_penalty
    )
    total_friction = fee_cost + slippage_cost + adverse_selection_penalty
    friction_to_edge_ratio = (
        total_friction / raw_edge if raw_edge > Decimal("0.0001") else Decimal("999")
    )
    disagreement_haircut = _provider_disagreement_haircut(
        provider_spread_f,
        provider_threshold_straddle_f,
    )
    uncertainty_haircut = min(
        Decimal("0.65"),
        Decimal("0.20") + path_state.path_uncertainty_addon + disagreement_haircut,
    )
    regime_haircut = regime.haircut_value
    portfolio_haircut = _portfolio_haircut(portfolio_risk_units_value)
    edge_conf_adj = raw_edge * (Decimal("1") - uncertainty_haircut) * (
        Decimal("1") - regime_haircut
    ) * (
        Decimal("1") - portfolio_haircut
    )
    executable_ev_per_contract = edge_conf_adj - total_friction
    executable_ev_total = executable_ev_per_contract * quantity

    return EdgeEstimate(
        market_ticker=market_ticker,
        side=side,
        quantity_fp=quantity,
        p_model=p_model,
        p_market_exec=p_market_exec,
        raw_edge=raw_edge,
        fee_cost=fee_cost,
        slippage_cost=slippage_cost,
        adverse_selection_penalty=adverse_selection_penalty,
        total_friction=total_friction,
        friction_to_edge_ratio=friction_to_edge_ratio,
        uncertainty_haircut=uncertainty_haircut,
        regime_haircut=regime_haircut,
        portfolio_haircut=portfolio_haircut,
        edge_conf_adj=edge_conf_adj,
        executable_ev_per_contract=executable_ev_per_contract,
        executable_ev_total=executable_ev_total,
    )
