from __future__ import annotations

import json

from kalshi_weather.analytics import (
    build_drift_report,
    build_nowcast_validation_report,
    build_provider_reliability_report,
    build_shadow_report,
    select_live_gate_profile,
)
from kalshi_weather.governance.qualification import apply_qualification_update, recommend_qualification_update
from kalshi_weather.settlement.corpus import summarize_validation_entries
from kalshi_weather.storage import DerivedAnalyticsStore, FileReferenceRegistry, SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


def _compute_settlement_error_summary(store, city_id: str) -> dict[str, object]:
    """Return per-city daily-high forecast performance from provider_errors.

    Used by the qualification engine to verify nowcast failures aren't
    over-gating cities whose daily-high forecasts are actually accurate.
    """
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT provider_id, AVG(abs_error_f), COUNT(*) FROM provider_errors "
            "WHERE city_id = ? GROUP BY provider_id",
            (city_id,),
        ).fetchall()
    if not rows:
        return {"sample_count": 0, "best_provider_mae_f": None}
    best_mae = min(float(mae) for _, mae, _ in rows if mae is not None)
    total_n = sum(int(n) for _, _, n in rows)
    return {
        "sample_count": total_n,
        "best_provider_mae_f": best_mae,
        "per_provider_mae_f": {
            pid: float(mae) for pid, mae, _ in rows if mae is not None
        },
    }


def main() -> None:
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    derived_store = DerivedAnalyticsStore("data/derived")
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    city_reports = []
    for context in registry.iter_city_contexts(seed):
        city_id = context.city_profile.city_id
        store.ensure_default_qualification(city_id)
        current_state = store.get_qualification_state(city_id)
        if current_state is None:
            continue
        decisions = store.list_decision_payloads(city_id)
        fills = store.list_shadow_fill_payloads(city_id)
        positions = [payload for payload in store.list_shadow_position_payloads() if payload.get("city_id") == city_id]
        position_history = store.list_shadow_position_history_payloads(city_id)
        shadow_report = build_shadow_report(decisions, fills, position_payloads=positions)
        drift_report = build_drift_report(decisions, fill_payloads=fills)
        settlement_summary = summarize_validation_entries(store.list_settlement_validation_payloads(city_id))
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
        # T2.3-derived per-city settlement-error summary. Gives the
        # qualification engine real evidence about whether our daily-high
        # forecasts have been accurate, vs the older nowcast-only check.
        settlement_error_summary = _compute_settlement_error_summary(store, city_id)

        update = recommend_qualification_update(
            current_state=current_state,
            shadow_report=shadow_report,
            drift_report=drift_report,
            settled_validation_count=int(settlement_summary.get("validated_market_count") or 0),
            city_id=city_id,
            profile=select_live_gate_profile(city_id),
            decision_payloads=decisions,
            position_history_payloads=position_history,
            settlement_summary=settlement_summary,
            nowcast_report=nowcast_report,
            calibration_report=calibration_report,
            settlement_error_summary=settlement_error_summary,
        )
        next_state = apply_qualification_update(current_state, update)
        store.save_qualification_state(next_state)
        city_reports.append(
            {
                "city_id": city_id,
                "previous_state": current_state.state.value,
                "next_state": next_state.state.value,
                "profile": select_live_gate_profile(city_id),
                "reasons": update.reasons,
                "scorecard": to_jsonable(update.scorecard),
                "settlement_summary": settlement_summary,
                "calibration_report": calibration_report,
                "nowcast_beats_baselines": nowcast_report.get("beats_baselines"),
            }
        )
    print(
        json.dumps(
            {
                "city_reports": city_reports,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
