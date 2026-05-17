from __future__ import annotations

from decimal import Decimal
from statistics import fmean, pstdev
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def build_shadow_report(
    decision_payloads: list[dict[str, Any]],
    fill_payloads: list[dict[str, Any]],
    position_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    executable_evs = [
        _decimal(payload.get("edge_summary", {}).get("selected_executable_ev"))
        for payload in decision_payloads
        if payload.get("final_decision") == "TAKER_ALLOWED"
    ]
    executable_evs = [value for value in executable_evs if value is not None]
    ev_floats = [float(value) for value in executable_evs]
    mean_ev = fmean(ev_floats) if ev_floats else 0.0
    std_ev = pstdev(ev_floats) if len(ev_floats) > 1 else 0.0
    lower_80 = mean_ev - (0.841621 * std_ev / max(len(ev_floats), 1) ** 0.5) if ev_floats else None
    settled_pnls = [
        float(_decimal(payload.get("settled_pnl_dollars")) or Decimal("0"))
        for payload in (position_payloads or [])
        if payload.get("lifecycle_status") == "CLOSED"
    ]
    settled_predicted_evs = [
        float(_decimal(payload.get("predicted_executable_ev_total")) or Decimal("0"))
        for payload in fill_payloads
        if payload.get("reconciliation_status") == "SETTLED"
    ]
    settled_win_rate = (
        sum(1 for pnl in settled_pnls if pnl > 0) / len(settled_pnls)
        if settled_pnls
        else None
    )
    total_settled_pnl = sum(settled_pnls)
    total_predicted_settled_ev = sum(settled_predicted_evs)
    total_calibration_residual = total_settled_pnl - total_predicted_settled_ev
    open_position_count = sum(
        1 for payload in (position_payloads or []) if payload.get("lifecycle_status") == "OPEN"
    )
    return {
        "decision_count": len(decision_payloads),
        "taker_allowed_count": sum(1 for payload in decision_payloads if payload.get("final_decision") == "TAKER_ALLOWED"),
        "shadow_fill_count": len(fill_payloads),
        "mean_selected_executable_ev": mean_ev,
        "lower_80_confidence_executable_ev": lower_80,
        "open_position_count": open_position_count,
        "settled_position_count": len(settled_pnls),
        "settled_win_rate": settled_win_rate,
        "total_settled_pnl_dollars": total_settled_pnl,
        "mean_settled_pnl_dollars": (sum(settled_pnls) / len(settled_pnls)) if settled_pnls else None,
        "total_predicted_settled_ev_dollars": total_predicted_settled_ev if settled_predicted_evs else None,
        "mean_predicted_settled_ev_dollars": (
            total_predicted_settled_ev / len(settled_predicted_evs)
        )
        if settled_predicted_evs
        else None,
        "total_settled_calibration_residual_dollars": (
            total_calibration_residual if settled_predicted_evs else None
        ),
        "mean_settled_calibration_residual_dollars": (
            total_calibration_residual / len(settled_predicted_evs)
        )
        if settled_predicted_evs
        else None,
    }
