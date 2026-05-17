from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from kalshi_weather.domain.models import MarketDefinition, MarketSnapshot, TradeSnapshot


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _to_datetime(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def market_definition_from_payload(payload: Mapping[str, Any]) -> MarketDefinition:
    return MarketDefinition(
        market_ticker=str(payload["ticker"]),
        event_ticker=str(payload.get("event_ticker") or ""),
        series_ticker=str(payload.get("series_ticker") or ""),
        market_type="binary_threshold",
        threshold_f=None,
        operator="",
        open_time=_to_datetime(payload.get("open_time")),
        close_time=_to_datetime(payload.get("close_time")),
        settlement_ts=_to_datetime(payload["expiration_time"])
        if payload.get("expiration_time")
        else None,
        rules_primary=str(payload.get("rules_primary") or ""),
        rules_secondary=str(payload.get("rules_secondary") or "")
        if payload.get("rules_secondary") is not None
        else None,
        price_level_structure=str(payload.get("price_level_structure") or ""),
        price_ranges=tuple(str(item) for item in payload.get("price_ranges", []) or []),
        can_close_early=bool(payload.get("can_close_early") or False),
        is_provisional=bool(payload.get("is_provisional") or False),
        status=str(payload.get("status") or ""),
    )


def market_snapshot_from_payload(payload: Mapping[str, Any], source_payload_id: str) -> MarketSnapshot:
    return MarketSnapshot(
        market_ticker=str(payload["ticker"]),
        status=str(payload.get("status") or ""),
        open_time=_to_datetime(payload.get("open_time")),
        close_time=_to_datetime(payload.get("close_time")),
        settlement_ts=_to_datetime(payload["expiration_time"])
        if payload.get("expiration_time")
        else None,
        yes_bid_dollars=_to_decimal(payload.get("yes_bid_dollars")),
        yes_ask_dollars=_to_decimal(payload.get("yes_ask_dollars")),
        no_bid_dollars=_to_decimal(payload.get("no_bid_dollars")),
        no_ask_dollars=_to_decimal(payload.get("no_ask_dollars")),
        yes_bid_size_fp=_to_decimal(payload.get("yes_bid_size_fp")),
        yes_ask_size_fp=_to_decimal(payload.get("yes_ask_size_fp")),
        no_bid_size_fp=_to_decimal(payload.get("no_bid_size_fp")),
        no_ask_size_fp=_to_decimal(payload.get("no_ask_size_fp")),
        last_price_dollars=_to_decimal(payload.get("last_price_dollars")),
        last_trade_size_fp=_to_decimal(payload.get("last_trade_count_fp")),
        volume_fp=_to_decimal(payload.get("volume_fp")),
        open_interest_fp=_to_decimal(payload.get("open_interest_fp")),
        updated_time=_to_datetime(
            payload.get("updated_time")
            or payload.get("last_update_time")
            or payload.get("close_time")
        ),
        source_payload_id=source_payload_id,
    )


def trade_snapshot_from_payload(payload: Mapping[str, Any], source_payload_id: str) -> TradeSnapshot:
    return TradeSnapshot(
        trade_id=str(payload["trade_id"]),
        market_ticker=str(payload["ticker"]),
        created_time=_to_datetime(payload["created_time"]),
        count_fp=_to_decimal(payload.get("count_fp")) or Decimal("0"),
        yes_price_dollars=_to_decimal(payload.get("yes_price_dollars")) or Decimal("0"),
        no_price_dollars=_to_decimal(payload.get("no_price_dollars")) or Decimal("0"),
        taker_side=str(payload.get("taker_side") or "").lower(),
        source_payload_id=source_payload_id,
    )
