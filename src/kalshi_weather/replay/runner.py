from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from kalshi_weather.domain.enums import QualificationState, RunMode
from kalshi_weather.domain.models import (
    CityProfile,
    ForecastSnapshot,
    MarketSnapshot,
    ObservationSnapshot,
    OrderbookSnapshot,
    StationReference,
    TradeSnapshot,
)
from kalshi_weather.engines.decision import DecisionCycleResult, run_market_decision_cycle
from kalshi_weather.engines.fill_simulation import build_fill_simulations


def replay_recorded_market(
    market: MarketSnapshot,
    market_definition,
    orderbooks: list[OrderbookSnapshot],
    trades: list[TradeSnapshot],
    observations: list[ObservationSnapshot],
    forecasts: list[ForecastSnapshot],
    city_profile: CityProfile,
    station: StationReference,
    qualification_state: QualificationState,
    provider_reliability: dict[str, Decimal] | None = None,
    provider_calibration_report: Mapping[str, Any] | None = None,
) -> list[DecisionCycleResult]:
    ordered_orderbooks = sorted(orderbooks, key=lambda item: item.as_of_time)
    ordered_observations = sorted(observations, key=lambda item: item.event_time, reverse=True)
    results: list[DecisionCycleResult] = []
    for orderbook in ordered_orderbooks:
        as_of = orderbook.as_of_time
        recent_obs = [obs for obs in ordered_observations if obs.event_time <= as_of][:4]
        if not recent_obs:
            continue
        result = run_market_decision_cycle(
            market=market,
            market_definition=market_definition,
            orderbook=orderbook,
            recent_orderbooks=[snapshot for snapshot in ordered_orderbooks if snapshot.as_of_time <= as_of][-10:],
            recent_trades=[trade for trade in trades if trade.created_time <= as_of][-50:],
            observations=recent_obs,
            forecasts=forecasts,
            city_profile=city_profile,
            station=station,
            qualification_state=qualification_state,
            provider_reliability=provider_reliability,
            provider_calibration_report=provider_calibration_report,
            run_mode=RunMode.REPLAY,
            as_of_time=as_of,
        )
        results.append(result)
    return results


def summarize_replay_scenarios(results: list[DecisionCycleResult]) -> dict[str, object]:
    scenario_values: dict[str, list[float]] = {
        "optimistic": [],
        "base": [],
        "pessimistic": [],
    }
    scenario_fill_ratios: dict[str, list[float]] = {
        "optimistic": [],
        "base": [],
        "pessimistic": [],
    }
    scenario_filled_quantities: dict[str, list[float]] = {
        "optimistic": [],
        "base": [],
        "pessimistic": [],
    }
    for result in results:
        if result.selected_edge is None:
            continue
        fill_scenarios = build_fill_simulations(result.explanation, result.selected_edge)
        for scenario_name, simulation in fill_scenarios.items():
            scenario_values[scenario_name].append(float(simulation.executable_ev_total))
            scenario_fill_ratios[scenario_name].append(float(simulation.fill_ratio))
            scenario_filled_quantities[scenario_name].append(float(simulation.filled_quantity_fp))

    def _summary(name: str, values: list[float]) -> dict[str, object]:
        return {
            "sample_count": len(values),
            "positive_count": sum(1 for value in values if value > 0),
            "mean_executable_ev": (sum(values) / len(values)) if values else None,
            "mean_fill_ratio": (
                sum(scenario_fill_ratios[name]) / len(scenario_fill_ratios[name])
            )
            if scenario_fill_ratios[name]
            else None,
            "mean_filled_quantity": (
                sum(scenario_filled_quantities[name]) / len(scenario_filled_quantities[name])
            )
            if scenario_filled_quantities[name]
            else None,
        }

    return {name: _summary(name, values) for name, values in scenario_values.items()}
