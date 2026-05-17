from __future__ import annotations

from decimal import Decimal
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _relative_impact(baseline: float | None, comparison: float | None) -> float | None:
    if baseline is None or comparison is None or baseline == 0:
        return None
    return abs(comparison - baseline) / abs(baseline) * 100.0


def build_sensitivity_matrix(
    decision_payloads: list[dict[str, Any]],
    minimum_sample: int = 120,
) -> dict[str, Any]:
    scenarios = []
    min_ev_values = (Decimal("0.005"), Decimal("0.015"), Decimal("0.03"))
    max_friction_values = (Decimal("0.40"), Decimal("0.60"), Decimal("0.80"))
    for min_ev in min_ev_values:
        for max_friction in max_friction_values:
            passing = []
            for payload in decision_payloads:
                edge = payload.get("edge_summary", {})
                selected_ev = _decimal(edge.get("selected_executable_ev"))
                friction_ratio = _decimal(edge.get("selected_friction_to_edge_ratio"))
                if selected_ev is None or friction_ratio is None:
                    continue
                if selected_ev >= min_ev and friction_ratio <= max_friction:
                    passing.append(float(selected_ev))
            scenarios.append(
                {
                    "min_executable_ev": str(min_ev),
                    "max_friction_to_edge_ratio": str(max_friction),
                    "passing_count": len(passing),
                    "mean_executable_ev": sum(passing) / len(passing) if passing else 0.0,
                }
            )
    baseline_ev = _mean(
        [
            float(_decimal(payload.get("edge_summary", {}).get("selected_executable_ev")) or Decimal("0"))
            for payload in decision_payloads
            if _decimal(payload.get("edge_summary", {}).get("selected_executable_ev")) is not None
        ]
    )

    parameter_reports: list[dict[str, Any]] = []
    blocking_parameters: list[str] = []
    parameter_specs = (
        {
            "name": "min_executable_ev",
            "baseline": Decimal("0.015"),
            "values": min_ev_values,
            "selector": lambda payload, value: (
                (_decimal(payload.get("edge_summary", {}).get("selected_executable_ev")) or Decimal("-999"))
                >= value
            ),
        },
        {
            "name": "max_friction_to_edge_ratio",
            "baseline": Decimal("0.60"),
            "values": max_friction_values,
            "selector": lambda payload, value: (
                (_decimal(payload.get("edge_summary", {}).get("selected_friction_to_edge_ratio")) or Decimal("999"))
                <= value
            ),
        },
        {
            "name": "observation_excess_lag_hard_halt_minutes",
            "baseline": Decimal("20"),
            "values": (Decimal("10"), Decimal("20"), Decimal("30")),
            "selector": lambda payload, value: Decimal(
                str(payload.get("data_freshness", {}).get("observation_excess_lag_minutes") or 999)
            )
            <= value,
        },
    )
    for spec in parameter_specs:
        baseline_value = spec["baseline"]
        scenario_means: dict[str, float | None] = {}
        for scenario in spec["values"]:
            selected = []
            for payload in decision_payloads:
                ev = _decimal(payload.get("edge_summary", {}).get("selected_executable_ev"))
                if ev is None:
                    continue
                if spec["selector"](payload, scenario):
                    selected.append(float(ev))
            scenario_means[str(scenario)] = _mean(selected)
        pessimistic_value = min(spec["values"])
        pessimistic_impact_pct = _relative_impact(
            scenario_means.get(str(baseline_value)),
            scenario_means.get(str(pessimistic_value)),
        )
        low_sensitivity = pessimistic_impact_pct is not None and pessimistic_impact_pct < 5.0
        enough_sample = len(decision_payloads) >= minimum_sample
        if not low_sensitivity and not enough_sample:
            blocking_parameters.append(str(spec["name"]))
        parameter_reports.append(
            {
                "name": spec["name"],
                "baseline": str(baseline_value),
                "scenario_mean_executable_ev": scenario_means,
                "decision_sample": len(decision_payloads),
                "required_sample": minimum_sample,
                "pessimistic_impact_pct": pessimistic_impact_pct,
                "low_sensitivity_under_pessimistic": low_sensitivity,
                "replaced_by_data": False,
                "needs_replacement_or_justification": (not low_sensitivity) or (not enough_sample),
            }
        )
    return {
        "baseline_mean_executable_ev": baseline_ev,
        "scenarios": scenarios,
        "parameters": parameter_reports,
        "blocking_parameters": blocking_parameters,
    }
