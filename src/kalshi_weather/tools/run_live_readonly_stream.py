from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from kalshi_weather.analytics import build_provider_reliability_report, extract_provider_reliability_weights
from kalshi_weather.clients import (
    KalshiAuthError,
    KalshiPublicClient,
    KalshiWebSocketClient,
    NwsWeatherClient,
    SubscriptionSpec,
)
from kalshi_weather.domain.enums import RunMode
from kalshi_weather.engines import run_market_decision_cycle
from kalshi_weather.ingestion.adapters import (
    NwsGridForecastAdapter,
    NwsHourlyForecastAdapter,
    NwsObservationAdapter,
)
from kalshi_weather.ingestion.contracts import RawPayloadRecord
from kalshi_weather.market import (
    OrderbookSequenceGapError,
    apply_orderbook_delta,
    market_definition_from_payload,
    market_snapshot_from_payload,
    orderbook_snapshot_from_stream_state,
    orderbook_state_from_snapshot,
    trade_snapshot_from_ws_message,
)
from kalshi_weather.settlement.rule_parser import SettlementRuleParseError
from kalshi_weather.storage import DerivedAnalyticsStore, FileRawStore, FileReferenceRegistry, SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


async def main_async(duration_seconds: int = 20) -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    contexts = registry.iter_city_contexts(seed)
    context_by_city = {context.city_profile.city_id: context for context in contexts}

    raw_store = FileRawStore("data/raw")
    derived_store = DerivedAnalyticsStore("data/derived")
    state_store = SQLiteStateStore("data/state/runtime.sqlite3")
    nws_client = NwsWeatherClient()
    qualification_by_city = {}
    provider_reliability_by_city = {}
    calibration_report_by_city = {}
    for context in contexts:
        state_store.ensure_default_qualification(context.city_profile.city_id)
        qualification = state_store.get_qualification_state(context.city_profile.city_id)
        if qualification is None:
            raise SystemExit(f"missing qualification state for {context.city_profile.city_id}")
        qualification_by_city[context.city_profile.city_id] = qualification

        obs_adapter = NwsObservationAdapter(nws_client, context.station, limit=4)
        obs_raw = obs_adapter.fetch_raw()
        raw_store.write(obs_raw)
        observations = [env.record for env in obs_adapter.normalize(obs_raw)]
        state_store.save_observations(observations)

        forecasts = []
        for adapter in (
            NwsHourlyForecastAdapter(nws_client, context.station),
            NwsGridForecastAdapter(nws_client, context.station),
        ):
            forecast_raw = adapter.fetch_raw()
            raw_store.write(forecast_raw)
            forecasts.extend(env.record for env in adapter.normalize(forecast_raw))
        state_store.save_forecasts(forecasts)

        calibration_report = build_provider_reliability_report(
            forecasts=state_store.get_all_forecasts(context.station.station_id),
            observations=state_store.get_all_observations(context.station.station_id),
            station=context.station,
        )
        derived_store.write_provider_reliability(context.city_profile.city_id, calibration_report)
        calibration_report_by_city[context.city_profile.city_id] = calibration_report
        provider_reliability_by_city[context.city_profile.city_id] = extract_provider_reliability_weights(
            calibration_report
        )

    kill_switch = state_store.get_kill_switch("GLOBAL")
    active_kill_switch = bool(kill_switch and kill_switch.get("state") == "ACTIVE")

    kalshi_client = KalshiPublicClient()
    market_ticker_to_payload = {}
    market_ticker_to_context = {}
    fee_multiplier_by_series = {}
    for context in contexts:
        market_payload = kalshi_client.list_markets(
            context.series_definition.series_ticker,
            status="open",
            limit=200,
        )
        market_ticker_to_payload.update(
            {
                str(item.get("ticker") or ""): item
                for item in market_payload.get("markets", [])
                if item.get("ticker")
            }
        )
        for ticker in market_ticker_to_payload:
            if ticker.split("-", 1)[0] == context.series_definition.series_ticker:
                market_ticker_to_context[ticker] = context
        series_payload = kalshi_client.get_series(context.series_definition.series_ticker)
        fee_multiplier_by_series[context.series_definition.series_ticker] = int(
            (series_payload.get("series", {}) or {}).get("fee_multiplier") or 1
        )
    market_ticker_to_snapshot = {
        ticker: market_snapshot_from_payload(payload, source_payload_id="live_readonly_bootstrap")
        for ticker, payload in market_ticker_to_payload.items()
    }
    market_tickers = tuple(market_ticker_to_payload.keys())
    if not market_tickers:
        raise SystemExit("no open markets returned for configured city series")

    try:
        ws_client = KalshiWebSocketClient.from_env()
    except KalshiAuthError as exc:
        raise SystemExit(str(exc)) from exc

    orderbook_states = {}
    city_decisions: dict[str, list[dict[str, object]]] = {
        context.city_profile.city_id: [] for context in contexts
    }
    end_time = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)

    specs = (
        SubscriptionSpec(channels=("orderbook_delta",), market_tickers=market_tickers),
        SubscriptionSpec(channels=("trade",), market_tickers=market_tickers),
    )
    async for payload in ws_client.stream(specs, idle_timeout_seconds=2.0):
        received_at = datetime.now(timezone.utc)
        payload_text = json.dumps(payload, sort_keys=True)
        payload_hash = sha256(payload_text.encode("utf-8")).hexdigest()
        message_type = str(payload.get("type") or "unknown")
        msg = payload.get("msg", {})
        market_ticker = str(msg.get("market_ticker") or "")
        raw_store.write(
            RawPayloadRecord(
                source_name=f"kalshi_ws_{message_type}",
                source_endpoint=ws_client.ws_url,
                request_params={"channels": [message_type]},
                transport_status=101,
                payload_hash=payload_hash,
                parser_version="kalshi_ws_v1",
                ingest_time=received_at,
                event_time=None,
                payload=payload_text,
                metadata={
                    "market_ticker": market_ticker,
                    "sid": payload.get("sid"),
                    "seq": msg.get("seq") or payload.get("seq"),
                },
            )
        )

        if message_type == "trade":
            state_store.save_trade_snapshots(
                [trade_snapshot_from_ws_message(payload, source_payload_id=payload_hash)]
            )
        elif message_type in {"orderbook_snapshot", "orderbook_delta"}:
            if message_type == "orderbook_snapshot":
                state = orderbook_state_from_snapshot(payload)
            else:
                existing_state = orderbook_states.get(market_ticker)
                if existing_state is None:
                    continue
                try:
                    state = apply_orderbook_delta(existing_state, payload)
                except OrderbookSequenceGapError:
                    orderbook_states.pop(market_ticker, None)
                    continue
            orderbook_states[state.market_ticker] = state
            orderbook = orderbook_snapshot_from_stream_state(
                state,
                source_ref=payload_hash,
                as_of_time=received_at,
            )
            state_store.save_orderbook_snapshot(orderbook)

            payload_market = market_ticker_to_payload.get(state.market_ticker)
            market_snapshot = market_ticker_to_snapshot.get(state.market_ticker)
            context = market_ticker_to_context.get(state.market_ticker)
            if not payload_market or market_snapshot is None or context is None:
                continue
            try:
                market_definition = market_definition_from_payload(payload_market)
                _ = market_definition.rules_primary
                open_positions = [
                    (position.city_id, position)
                    for position in state_store.list_shadow_positions()
                    if position.lifecycle_status == "OPEN"
                ]
                open_position_signals = []
                for open_city_id, position in open_positions:
                    latest_signal = next(
                        (
                            payload
                            for payload in state_store.list_decision_payloads(open_city_id)
                            if str(payload.get("market_ticker") or "") == position.market_ticker
                        ),
                        None,
                    )
                    if latest_signal is not None:
                        open_position_signals.append(latest_signal)
                result = run_market_decision_cycle(
                    market=market_snapshot,
                    market_definition=market_definition,
                    orderbook=orderbook,
                    recent_orderbooks=state_store.get_recent_orderbook_snapshots(state.market_ticker, limit=10),
                    recent_trades=state_store.get_recent_trade_snapshots(state.market_ticker, limit=50),
                    observations=state_store.get_recent_observations(context.station.station_id, limit=4),
                    forecasts=state_store.get_latest_forecasts(context.station.station_id),
                    city_profile=context.city_profile,
                    station=context.station,
                    qualification_state=qualification_by_city[context.city_profile.city_id].state,
                    provider_reliability=provider_reliability_by_city[context.city_profile.city_id],
                    provider_calibration_report=calibration_report_by_city[context.city_profile.city_id],
                    run_mode=RunMode.LIVE_READONLY,
                    fee_multiplier=fee_multiplier_by_series[context.series_definition.series_ticker],
                    open_positions=open_positions,
                    open_position_signals=open_position_signals,
                    active_kill_switch=active_kill_switch,
                )
            except SettlementRuleParseError:
                continue
            state_store.save_decision(result.explanation)
            city_decisions[context.city_profile.city_id].append(
                {
                    "market_ticker": result.explanation.market_ticker,
                    "decision": result.explanation.final_decision.value,
                    "selected_side": result.selected_edge.side if result.selected_edge else None,
                    "selected_ev": str(result.selected_edge.executable_ev_per_contract)
                    if result.selected_edge
                    else None,
                    "as_of_time": result.explanation.as_of_time.isoformat(),
                }
            )

        if datetime.now(timezone.utc) >= end_time:
            break

    print(
        json.dumps(
            to_jsonable(
                {
                    "run_mode": RunMode.LIVE_READONLY,
                    "city_reports": {
                        city_id: {
                            "qualification_state": qualification_by_city[city_id].state.value,
                            "provider_reliability": {
                                key: str(value)
                                for key, value in provider_reliability_by_city[city_id].items()
                            },
                            "decision_count": len(decisions),
                            "recent_decisions": decisions[-10:],
                        }
                        for city_id, decisions in city_decisions.items()
                    },
                }
            ),
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
