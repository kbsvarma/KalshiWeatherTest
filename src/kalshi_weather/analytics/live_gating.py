from __future__ import annotations

from typing import Any

from .drift import build_drift_report
from .sensitivity import build_sensitivity_matrix
from .shadow import build_shadow_report


STRICT_PRODUCTION_PROFILE = "STRICT_PRODUCTION"
NYC_MVP_LIVE_PROFILE = "NYC_MVP_LIVE"


def select_live_gate_profile(city_id: str | None) -> str:
    if str(city_id or "").lower() == "nyc":
        return NYC_MVP_LIVE_PROFILE
    return STRICT_PRODUCTION_PROFILE


def _strict_gates(
    *,
    shadow_report: dict[str, Any],
    drift_report: dict[str, Any],
    sensitivity_report: dict[str, Any],
    settlement_summary: dict[str, Any],
    nowcast_report: dict[str, Any],
    calibration_report: dict[str, Any],
) -> dict[str, bool]:
    average_excess_lag = drift_report.get("average_observation_excess_lag_minutes")
    p95_excess_lag = drift_report.get("p95_observation_excess_lag_minutes")
    return {
        "minimum_total_shadow_sample": shadow_report["shadow_fill_count"] >= 100,
        "positive_lower_80_shadow_ev": (
            shadow_report["lower_80_confidence_executable_ev"] is not None
            and shadow_report["lower_80_confidence_executable_ev"] > 0
        ),
        "positive_realized_shadow_pnl": (
            shadow_report["settled_position_count"] >= 20
            and shadow_report["total_settled_pnl_dollars"] > 0
        ),
        "stable_observation_lag": (
            average_excess_lag is not None
            and p95_excess_lag is not None
            and average_excess_lag <= 12
            and p95_excess_lag <= 20
        ),
        "single_schema_version": (
            len(drift_report["schema_versions"]) == 1 and "1.0.0" in drift_report["schema_versions"]
        ),
        "provisional_parameters_bounded": not sensitivity_report["blocking_parameters"],
        "settlement_validation_ready": bool(settlement_summary.get("eligible_for_shadow_only")),
        "nowcast_beats_baselines": bool(nowcast_report.get("beats_baselines")),
        "forecast_calibration_sample_sufficient": bool(calibration_report.get("sample_sufficient")),
    }


def _nyc_mvp_live_gates(
    *,
    shadow_report: dict[str, Any],
    drift_report: dict[str, Any],
    sensitivity_report: dict[str, Any],
    settlement_summary: dict[str, Any],
    nowcast_report: dict[str, Any],
    calibration_report: dict[str, Any],
) -> dict[str, bool]:
    average_excess_lag = drift_report.get("average_observation_excess_lag_minutes")
    p95_excess_lag = drift_report.get("p95_observation_excess_lag_minutes")
    blocking_parameters = set(sensitivity_report.get("blocking_parameters") or [])
    resolved = int(settlement_summary.get("resolved_validation_count") or 0)
    matched = int(settlement_summary.get("matched_market_count") or 0)
    unresolved = int(settlement_summary.get("unresolved_entry_count") or 0)
    critical = int(settlement_summary.get("critical_mismatch_count") or 0)
    revision_resolved = int(settlement_summary.get("revision_resolved_count") or 0)
    calibration_samples = int(calibration_report.get("global_sample_count") or 0)
    calibration_runs = int(calibration_report.get("global_unique_run_count") or 0)
    calibration_mae = calibration_report.get("global_mean_abs_error_f")
    lower_80 = shadow_report.get("lower_80_confidence_executable_ev")
    positive_shadow_lower80 = lower_80 is not None and lower_80 > 0.02
    settled_position_count = int(shadow_report.get("settled_position_count") or 0)
    settled_win_rate = shadow_report.get("settled_win_rate")
    stable_lag = (
        average_excess_lag is not None
        and p95_excess_lag is not None
        and average_excess_lag <= 12
        and p95_excess_lag <= 20
    )
    return {
        "minimum_total_shadow_sample": (
            int(shadow_report.get("decision_count") or 0) >= 75
            and int(shadow_report.get("taker_allowed_count") or 0) >= 20
        ),
        "positive_lower_80_shadow_ev": positive_shadow_lower80,
        "positive_realized_shadow_pnl": (
            settled_position_count >= 75
            and (shadow_report.get("total_settled_pnl_dollars") or 0) > 0
            and settled_win_rate is not None
            and settled_win_rate > 0.58
        ),
        "stable_observation_lag": stable_lag,
        "single_schema_version": (
            len(drift_report["schema_versions"]) == 1 and "1.0.0" in drift_report["schema_versions"]
        ),
        "provisional_parameters_bounded": (
            not blocking_parameters
            or (blocking_parameters <= {"observation_excess_lag_hard_halt_minutes"} and stable_lag)
        ),
        "settlement_validation_ready": (
            resolved >= 50
            and matched == resolved
            and critical == 0
            and revision_resolved == resolved
            and unresolved == 0
        ),
        "nowcast_beats_baselines": bool(nowcast_report.get("beats_baselines")),
        "forecast_calibration_sample_sufficient": (
            calibration_samples >= 10
            and calibration_runs >= 2
            and calibration_mae is not None
            and float(calibration_mae) <= 1.5
        ),
    }


def build_live_gate_report(
    decision_payloads: list[dict[str, Any]],
    fill_payloads: list[dict[str, Any]],
    position_payloads: list[dict[str, Any]] | None = None,
    settlement_summary: dict[str, Any] | None = None,
    nowcast_report: dict[str, Any] | None = None,
    calibration_report: dict[str, Any] | None = None,
    *,
    profile: str = STRICT_PRODUCTION_PROFILE,
) -> dict[str, Any]:
    shadow_report = build_shadow_report(decision_payloads, fill_payloads, position_payloads=position_payloads)
    drift_report = build_drift_report(decision_payloads, fill_payloads=fill_payloads)
    sensitivity_report = build_sensitivity_matrix(decision_payloads)
    settlement_summary = settlement_summary or {}
    nowcast_report = nowcast_report or {}
    calibration_report = calibration_report or {}
    if profile == NYC_MVP_LIVE_PROFILE:
        gates = _nyc_mvp_live_gates(
            shadow_report=shadow_report,
            drift_report=drift_report,
            sensitivity_report=sensitivity_report,
            settlement_summary=settlement_summary,
            nowcast_report=nowcast_report,
            calibration_report=calibration_report,
        )
    else:
        gates = _strict_gates(
            shadow_report=shadow_report,
            drift_report=drift_report,
            sensitivity_report=sensitivity_report,
            settlement_summary=settlement_summary,
            nowcast_report=nowcast_report,
            calibration_report=calibration_report,
        )
    gates_passed = sum(1 for v in gates.values() if v)
    gates_total = len(gates)
    return {
        "profile": profile,
        "gates": gates,
        "all_passed": all(gates.values()),
        "gates_passed": gates_passed,
        "gates_total": gates_total,
        "gates_failed": [k for k, v in gates.items() if not v],
        "shadow_report": shadow_report,
        "drift_report": drift_report,
        "sensitivity_report": sensitivity_report,
        "settlement_summary": settlement_summary,
        "nowcast_report": nowcast_report,
        "calibration_report": calibration_report,
    }
