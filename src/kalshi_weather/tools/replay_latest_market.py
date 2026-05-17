from __future__ import annotations

import json

from kalshi_weather.analytics import extract_provider_reliability_weights, build_sensitivity_matrix
from kalshi_weather.clients import KalshiPublicClient
from kalshi_weather.market.payloads import market_definition_from_payload, market_snapshot_from_payload
from kalshi_weather.replay import replay_recorded_market, summarize_replay_scenarios
from kalshi_weather.storage import DerivedAnalyticsStore, FileReferenceRegistry, SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


def main() -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    derived_store = DerivedAnalyticsStore("data/derived")
    kalshi = KalshiPublicClient()
    city_reports: list[dict[str, object]] = []
    for context in registry.iter_city_contexts(seed):
        qualification = store.get_qualification_state(context.city_profile.city_id)
        if qualification is None:
            continue
        calibration_report = (
            derived_store.read_provider_reliability(context.city_profile.city_id) or {}
        )
        latest_decision = next(
            iter(store.list_decision_payloads(context.city_profile.city_id)),
            None,
        )
        market_ticker = str(latest_decision.get("market_ticker")) if latest_decision else ""
        if not market_ticker:
            open_markets = kalshi.iter_open_markets(
                context.series_definition.series_ticker,
                max_records=1,
            )
            if not open_markets:
                city_reports.append(
                    {
                        "city_id": context.city_profile.city_id,
                        "series_ticker": context.series_definition.series_ticker,
                        "error": "no open markets returned",
                    }
                )
                continue
            market_ticker = str(open_markets[0].get("ticker") or "")
        payload = kalshi.get_market(market_ticker)
        market_payload = payload["market"]
        market_definition = market_definition_from_payload(market_payload)
        market_snapshot = market_snapshot_from_payload(
            market_payload,
            source_payload_id="live_market_fetch",
        )
        results = replay_recorded_market(
            market=market_snapshot,
            market_definition=market_definition,
            orderbooks=store.get_all_orderbook_snapshots(market_snapshot.market_ticker),
            trades=store.get_all_trade_snapshots(market_snapshot.market_ticker),
            observations=store.get_all_observations(context.station.station_id),
            forecasts=store.get_all_forecasts(context.station.station_id),
            city_profile=context.city_profile,
            station=context.station,
            qualification_state=qualification.state,
            provider_reliability=extract_provider_reliability_weights(calibration_report),
            provider_calibration_report=calibration_report,
        )
        replay_decision_payloads = []
        for item in results:
            replay_payload = item.explanation.to_dict()
            if item.selected_edge is not None:
                replay_payload["edge_summary"] = dict(replay_payload.get("edge_summary", {}))
                replay_payload["edge_summary"]["selected_executable_ev"] = str(
                    item.selected_edge.executable_ev_per_contract
                )
                replay_payload["edge_summary"]["selected_friction_to_edge_ratio"] = str(
                    item.selected_edge.friction_to_edge_ratio
                )
            replay_decision_payloads.append(replay_payload)
        city_reports.append(
            {
                "city_id": context.city_profile.city_id,
                "market_ticker": market_snapshot.market_ticker,
                "replay_count": len(results),
                "scenario_summary": summarize_replay_scenarios(results),
                "sensitivity": build_sensitivity_matrix(replay_decision_payloads),
                "forecast_calibration": calibration_report,
                "decisions": [
                    {
                        "as_of_time": item.explanation.as_of_time.isoformat(),
                        "decision": item.explanation.final_decision.value,
                        "selected_side": item.selected_edge.side if item.selected_edge else None,
                    }
                    for item in results[-10:]
                ],
            }
        )
    print(
        json.dumps(
            {"city_reports": city_reports},
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
