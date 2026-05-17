from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_weather.domain.models import EdgeEstimate, StrategyDecisionExplanation


def _decimal(value: Any, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _scenario_fill_ratio(
    *,
    execution_style: str,
    tradability_score: Decimal,
    ghost_liquidity_ratio: Decimal,
    depth_consumed_levels: int,
    maker_fill_probability: Decimal,
    quote_stability_score: Decimal,
    scenario: str,
) -> Decimal:
    if execution_style == "maker":
        base = maker_fill_probability * (Decimal("0.70") + (Decimal("0.30") * quote_stability_score))
        if scenario == "optimistic":
            return min(Decimal("1"), base * Decimal("1.25"))
        if scenario == "pessimistic":
            return max(Decimal("0"), base * Decimal("0.60"))
        return min(Decimal("1"), base)

    base = tradability_score * (Decimal("1") - (ghost_liquidity_ratio * Decimal("0.45")))
    depth_penalty = Decimal("0.08") * Decimal(str(max(0, depth_consumed_levels - 1)))
    base = max(Decimal("0"), min(Decimal("1"), base - depth_penalty))
    if scenario == "optimistic":
        return min(Decimal("1"), base + Decimal("0.15"))
    if scenario == "pessimistic":
        return max(Decimal("0"), base - Decimal("0.25"))
    return base


@dataclass(frozen=True, slots=True)
class FillSimulation:
    scenario: str
    execution_style: str
    requested_quantity_fp: Decimal
    filled_quantity_fp: Decimal
    fill_ratio: Decimal
    modeled_fill_price_dollars: Decimal
    modeled_fee_dollars: Decimal
    modeled_slippage_dollars: Decimal
    modeled_adverse_selection_dollars: Decimal
    executable_ev_total: Decimal


def build_fill_simulations(
    explanation: StrategyDecisionExplanation,
    edge: EdgeEstimate,
) -> dict[str, FillSimulation]:
    micro = explanation.microstructure_summary
    execution_style = str(
        micro.get("selected_execution_style")
        or explanation.edge_summary.get("selected_execution_style")
        or "taker"
    )
    tradability_score = _decimal(micro.get("selected_taker_tradability_score"), Decimal("0.60"))
    ghost_liquidity_ratio = _decimal(micro.get("selected_ghost_liquidity_ratio"), Decimal("0.20"))
    depth_consumed_levels = int(micro.get("selected_depth_consumed_levels") or 1)
    maker_fill_probability = _decimal(micro.get("selected_maker_fill_probability"), Decimal("0"))
    quote_stability_score = _decimal(micro.get("selected_quote_stability_score"), Decimal("0.50"))
    slippage_cost = edge.slippage_cost
    adverse_cost = edge.adverse_selection_penalty

    simulations: dict[str, FillSimulation] = {}
    for scenario in ("optimistic", "base", "pessimistic"):
        fill_ratio = _scenario_fill_ratio(
            execution_style=execution_style,
            tradability_score=tradability_score,
            ghost_liquidity_ratio=ghost_liquidity_ratio,
            depth_consumed_levels=depth_consumed_levels,
            maker_fill_probability=maker_fill_probability,
            quote_stability_score=quote_stability_score,
            scenario=scenario,
        )
        filled_quantity = (edge.quantity_fp * fill_ratio).quantize(Decimal("0.000001"))
        price = edge.p_market_exec
        if execution_style == "taker":
            if scenario == "optimistic":
                price = max(Decimal("0"), edge.p_market_exec - (slippage_cost * Decimal("0.50")))
            elif scenario == "pessimistic":
                price = min(Decimal("1"), edge.p_market_exec + slippage_cost + (ghost_liquidity_ratio * Decimal("0.02")))
        else:
            if scenario == "optimistic":
                price = max(Decimal("0"), edge.p_market_exec - Decimal("0.01"))
            elif scenario == "pessimistic":
                price = min(Decimal("1"), edge.p_market_exec + Decimal("0.01"))

        fee = edge.fee_cost * fill_ratio
        slippage = slippage_cost * fill_ratio
        adverse = adverse_cost * fill_ratio
        if scenario == "optimistic":
            slippage *= Decimal("0.50")
            adverse *= Decimal("0.75")
        elif scenario == "pessimistic":
            slippage *= Decimal("1.50")
            adverse *= Decimal("1.25")
        price_penalty = (price - edge.p_market_exec) * filled_quantity
        ev_total = (edge.executable_ev_per_contract * filled_quantity) - price_penalty
        simulations[scenario] = FillSimulation(
            scenario=scenario,
            execution_style=execution_style,
            requested_quantity_fp=edge.quantity_fp,
            filled_quantity_fp=filled_quantity,
            fill_ratio=fill_ratio,
            modeled_fill_price_dollars=price,
            modeled_fee_dollars=fee,
            modeled_slippage_dollars=slippage,
            modeled_adverse_selection_dollars=adverse,
            executable_ev_total=ev_total,
        )
    return simulations
