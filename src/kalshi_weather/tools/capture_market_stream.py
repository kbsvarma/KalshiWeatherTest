from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from kalshi_weather.clients import (
    KalshiAuthError,
    KalshiPublicClient,
    KalshiWebSocketClient,
    SubscriptionSpec,
)
from kalshi_weather.ingestion.contracts import RawPayloadRecord
from kalshi_weather.market import (
    OrderbookSequenceGapError,
    apply_orderbook_delta,
    orderbook_snapshot_from_stream_state,
    orderbook_state_from_snapshot,
    trade_snapshot_from_ws_message,
)
from kalshi_weather.storage import FileRawStore, FileReferenceRegistry, SQLiteStateStore


async def main_async(duration_seconds: int = 15) -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    public_client = KalshiPublicClient()
    series_tickers: list[str] = []
    market_tickers: list[str] = []
    for context in registry.iter_city_contexts(seed):
        series_tickers.append(context.series_definition.series_ticker)
        open_payload = public_client.list_markets(
            context.series_definition.series_ticker,
            status="open",
            limit=25,
        )
        market_tickers.extend(
            [
                str(market.get("ticker") or "")
                for market in open_payload.get("markets", [])
                if market.get("ticker")
            ][:8]
        )
    market_tickers = list(dict.fromkeys(market_tickers))
    if not market_tickers:
        raise SystemExit("no open markets found for configured city series")

    try:
        ws_client = KalshiWebSocketClient.from_env()
    except KalshiAuthError as exc:
        raise SystemExit(str(exc)) from exc

    raw_store = FileRawStore("data/raw")
    state_store = SQLiteStateStore("data/state/runtime.sqlite3")
    orderbook_states = {}
    end_time = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
    message_count = 0
    trade_count = 0
    orderbook_count = 0

    specs = (
        SubscriptionSpec(channels=("orderbook_delta",), market_tickers=tuple(market_tickers)),
        SubscriptionSpec(channels=("trade",), market_tickers=tuple(market_tickers)),
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
        message_count += 1

        if message_type == "orderbook_snapshot":
            state = orderbook_state_from_snapshot(payload)
            orderbook_states[state.market_ticker] = state
            state_store.save_orderbook_snapshot(
                orderbook_snapshot_from_stream_state(
                    state,
                    source_ref=payload_hash,
                    as_of_time=received_at,
                )
            )
            orderbook_count += 1
        elif message_type == "orderbook_delta":
            if market_ticker not in orderbook_states:
                continue
            try:
                state = apply_orderbook_delta(orderbook_states[market_ticker], payload)
            except OrderbookSequenceGapError:
                orderbook_states.pop(market_ticker, None)
                continue
            orderbook_states[market_ticker] = state
            state_store.save_orderbook_snapshot(
                orderbook_snapshot_from_stream_state(
                    state,
                    source_ref=payload_hash,
                    as_of_time=received_at,
                )
            )
            orderbook_count += 1
        elif message_type == "trade":
            state_store.save_trade_snapshots(
                [trade_snapshot_from_ws_message(payload, source_payload_id=payload_hash)]
            )
            trade_count += 1

        if datetime.now(timezone.utc) >= end_time:
            break

    print(
        json.dumps(
            {
                "captured_messages": message_count,
                "captured_orderbook_updates": orderbook_count,
                "captured_trades": trade_count,
                "market_tickers": market_tickers,
                "series_tickers": series_tickers,
                "duration_seconds": duration_seconds,
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
