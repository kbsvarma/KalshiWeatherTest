from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from kalshi_weather.domain.models import OrderbookSnapshot, TradeSnapshot
from kalshi_weather.market.orderbook import derive_implied_ask_ladders


class OrderbookSequenceGapError(RuntimeError):
    """Raised when an orderbook delta sequence is discontinuous."""


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _parse_ts(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _normalize_ladder(levels: Any) -> tuple[tuple[Decimal, Decimal], ...]:
    if not levels:
        return ()
    normalized = [(_decimal(price), _decimal(size)) for price, size in levels]
    return tuple(sorted((level for level in normalized if level[1] > 0), key=lambda item: item[0]))


def _apply_delta_to_ladder(
    ladder: tuple[tuple[Decimal, Decimal], ...],
    *,
    price: Decimal,
    delta_fp: Decimal,
) -> tuple[tuple[Decimal, Decimal], ...]:
    updated: dict[Decimal, Decimal] = {level_price: size for level_price, size in ladder}
    next_size = updated.get(price, Decimal("0")) + delta_fp
    if next_size <= 0:
        updated.pop(price, None)
    else:
        updated[price] = next_size
    return tuple(sorted(updated.items(), key=lambda item: item[0]))


@dataclass(frozen=True, slots=True)
class OrderbookStreamState:
    market_ticker: str
    market_id: str | None
    sid: int
    seq: int
    yes_bids_ladder: tuple[tuple[Decimal, Decimal], ...]
    no_bids_ladder: tuple[tuple[Decimal, Decimal], ...]
    last_event_time: datetime


def orderbook_state_from_snapshot(payload: Mapping[str, Any]) -> OrderbookStreamState:
    if str(payload.get("type") or "") != "orderbook_snapshot":
        raise ValueError("payload is not an orderbook_snapshot message")
    msg = payload.get("msg", {})
    return OrderbookStreamState(
        market_ticker=str(msg.get("market_ticker") or ""),
        market_id=str(msg.get("market_id")) if msg.get("market_id") is not None else None,
        sid=int(payload.get("sid") or 0),
        seq=int(msg.get("seq") or payload.get("seq") or 0),
        yes_bids_ladder=_normalize_ladder(msg.get("yes_dollars") or msg.get("yes_dollars_fp") or []),
        no_bids_ladder=_normalize_ladder(msg.get("no_dollars") or msg.get("no_dollars_fp") or []),
        last_event_time=_parse_ts(msg.get("ts")),
    )


def apply_orderbook_delta(
    state: OrderbookStreamState,
    payload: Mapping[str, Any],
) -> OrderbookStreamState:
    if str(payload.get("type") or "") != "orderbook_delta":
        raise ValueError("payload is not an orderbook_delta message")
    msg = payload.get("msg", {})
    next_seq = int(msg.get("seq") or payload.get("seq") or 0)
    if next_seq != state.seq + 1:
        raise OrderbookSequenceGapError(
            f"expected seq {state.seq + 1}, received {next_seq} for {state.market_ticker}"
        )
    price = _decimal(msg.get("price_dollars"))
    delta_fp = _decimal(msg.get("delta_fp"))
    side = str(msg.get("side") or "").lower()
    if side == "yes":
        yes_ladder = _apply_delta_to_ladder(state.yes_bids_ladder, price=price, delta_fp=delta_fp)
        no_ladder = state.no_bids_ladder
    elif side == "no":
        yes_ladder = state.yes_bids_ladder
        no_ladder = _apply_delta_to_ladder(state.no_bids_ladder, price=price, delta_fp=delta_fp)
    else:
        raise ValueError(f"unsupported orderbook side: {side}")

    return OrderbookStreamState(
        market_ticker=state.market_ticker,
        market_id=state.market_id,
        sid=state.sid,
        seq=next_seq,
        yes_bids_ladder=yes_ladder,
        no_bids_ladder=no_ladder,
        last_event_time=_parse_ts(msg.get("ts")),
    )


def orderbook_snapshot_from_stream_state(
    state: OrderbookStreamState,
    *,
    source_ref: str,
    as_of_time: datetime | None = None,
) -> OrderbookSnapshot:
    implied_yes_asks, implied_no_asks = derive_implied_ask_ladders(
        state.yes_bids_ladder,
        state.no_bids_ladder,
    )
    return OrderbookSnapshot(
        market_ticker=state.market_ticker,
        as_of_time=as_of_time or state.last_event_time,
        seq=state.seq,
        yes_bids_ladder=state.yes_bids_ladder,
        no_bids_ladder=state.no_bids_ladder,
        implied_yes_asks_ladder=implied_yes_asks,
        implied_no_asks_ladder=implied_no_asks,
        checksum_status="ws_stream",
        source_refs=(source_ref,),
    )


def trade_snapshot_from_ws_message(
    payload: Mapping[str, Any],
    *,
    source_payload_id: str,
) -> TradeSnapshot:
    if str(payload.get("type") or "") != "trade":
        raise ValueError("payload is not a trade message")
    msg = payload.get("msg", {})
    return TradeSnapshot(
        trade_id=str(msg.get("trade_id") or ""),
        market_ticker=str(msg.get("market_ticker") or msg.get("ticker") or ""),
        created_time=_parse_ts(msg.get("ts") or msg.get("created_time")),
        count_fp=_decimal(msg.get("count_fp") or "0"),
        yes_price_dollars=_decimal(msg.get("yes_price_dollars") or "0"),
        no_price_dollars=_decimal(msg.get("no_price_dollars") or "0"),
        taker_side=str(msg.get("taker_side") or "").lower(),
        source_payload_id=source_payload_id,
    )
