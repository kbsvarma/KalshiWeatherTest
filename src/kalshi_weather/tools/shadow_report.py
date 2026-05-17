from __future__ import annotations

import json

from kalshi_weather.analytics import (
    build_drift_report,
    build_live_gate_report,
    build_nowcast_validation_report,
    build_opportunity_board,
    build_provider_reliability_report,
    build_qualification_scorecard,
    build_sensitivity_matrix,
    build_shadow_report,
    select_live_gate_profile,
)
from kalshi_weather.settlement.corpus import summarize_validation_entries
from kalshi_weather.storage import DerivedAnalyticsStore, FileReferenceRegistry, SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


def main() -> None:
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    decisions = store.list_decision_payloads()
    fills = store.list_shadow_fill_payloads()
    positions = store.list_shadow_position_payloads()
    validations = store.list_settlement_validation_payloads()
    derived_store = DerivedAnalyticsStore("data/derived")
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    shadow_report_payload = build_shadow_report(decisions, fills, position_payloads=positions)
    drift_report_payload = build_drift_report(decisions, fill_payloads=fills)
    qualification_states = {
        context.city_profile.city_id: (
            store.get_qualification_state(context.city_profile.city_id).state.value
            if store.get_qualification_state(context.city_profile.city_id) is not None
            else None
        )
        for context in registry.iter_city_contexts(seed)
    }
    city_reports = {}
    for context in registry.iter_city_contexts(seed):
        city_id = context.city_profile.city_id
        city_decisions = [payload for payload in decisions if payload.get("city_id") == city_id]
        city_fills = [payload for payload in fills if payload.get("city_id") == city_id]
        city_positions = [payload for payload in positions if payload.get("city_id") == city_id]
        city_validations = [payload for payload in validations if payload.get("city_id") == city_id]
        calibration_report = derived_store.read_provider_reliability(city_id)
        if calibration_report is None:
            calibration_report = build_provider_reliability_report(
                store.get_all_forecasts(context.station.station_id),
                store.get_all_observations(context.station.station_id),
                context.station,
            )
            derived_store.write_provider_reliability(city_id, calibration_report)
        nowcast_report = build_nowcast_validation_report(
            store.get_all_observations(context.station.station_id),
            store.get_latest_forecasts(context.station.station_id),
            context.city_profile,
            station_timezone=context.station.timezone,
        )
        settlement_summary = summarize_validation_entries(city_validations)
        city_reports[city_id] = {
            "shadow": build_shadow_report(city_decisions, city_fills, position_payloads=city_positions),
            "drift": build_drift_report(city_decisions, fill_payloads=city_fills),
            "sensitivity": build_sensitivity_matrix(city_decisions),
            "settlement_validation": settlement_summary,
            "forecast_calibration": calibration_report,
            "nowcast_validation": nowcast_report,
            "qualification_scorecard": build_qualification_scorecard(
                city_decisions,
                position_history_payloads=store.list_shadow_position_history_payloads(city_id),
                shadow_report=build_shadow_report(city_decisions, city_fills, position_payloads=city_positions),
                drift_report=build_drift_report(city_decisions, fill_payloads=city_fills),
                settlement_summary=settlement_summary,
                nowcast_report=nowcast_report,
                calibration_report=calibration_report,
            ),
            "live_gating": build_live_gate_report(
                city_decisions,
                city_fills,
                position_payloads=city_positions,
                settlement_summary=settlement_summary,
                nowcast_report=nowcast_report,
                calibration_report=calibration_report,
                profile=select_live_gate_profile(city_id),
            ),
            "opportunities": build_opportunity_board(
                city_decisions,
                qualification_states={city_id: qualification_states.get(city_id)},
                limit=5,
            ),
        }
    report = {
        "shadow": shadow_report_payload,
        "drift": drift_report_payload,
        "opportunity_board": build_opportunity_board(
            decisions,
            qualification_states=qualification_states,
            limit=20,
        ),
        "city_reports": city_reports,
    }
    print(json.dumps(to_jsonable(report), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
