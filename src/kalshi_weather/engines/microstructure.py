from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from kalshi_weather.domain.models import (
    MicrostructureAssessment,
    OrderbookSnapshot,
    TradeSnapshot,
    TradabilityAssessment,
)
from kalshi_weather.market.orderbook import best_implied_ask_price, executable_wap_for_buy


WEATHER_WIDE_SPREAD_THRESHOLD = Decimal("0.12")
WEATHER_LOW_TRADABILITY_THRESHOLD = Decimal("0.60")


def _best_yes_bid(snapshot: OrderbookSnapshot) -> Decimal | None:
    if not snapshot.yes_bids_ladder:
        return None
    return max(price for price, _size in snapshot.yes_bids_ladder)


def _best_no_bid(snapshot: OrderbookSnapshot) -> Decimal | None:
    if not snapshot.no_bids_ladder:
        return None
    return max(price for price, _size in snapshot.no_bids_ladder)


def _durability_score(history: list[OrderbookSnapshot], side: str) -> Decimal:
    if len(history) < 2:
        return Decimal("0.5")
    bests = []
    for snapshot in history:
        best = _best_yes_bid(snapshot) if side == "no" else best_implied_ask_price(snapshot.implied_yes_asks_ladder)
        bests.append(best)
    unchanged = 0
    comparisons = 0
    for newer, older in zip(bests, bests[1:], strict=False):
        if newer is None or older is None:
            continue
        comparisons += 1
        if newer == older:
            unchanged += 1
    if comparisons == 0:
        return Decimal("0.5")
    return Decimal(str(unchanged / comparisons))


def _quote_stability_score(history: list[OrderbookSnapshot], side: str) -> Decimal:
    if len(history) < 2:
        return Decimal("0.5")
    changes = 0
    comparisons = 0
    prev = None
    for snapshot in history:
        current = _best_yes_bid(snapshot) if side == "yes" else _best_no_bid(snapshot)
        if prev is not None and current is not None:
            comparisons += 1
            if current != prev:
                changes += 1
        prev = current
    if comparisons == 0:
        return Decimal("0.5")
    return Decimal("1") - Decimal(str(changes / comparisons))


def _ghost_liquidity_ratio(history: list[OrderbookSnapshot], side: str) -> Decimal:
    if len(history) < 2:
        return Decimal("0.2")
    drops = 0
    comparisons = 0
    for newer, older in zip(history, history[1:], strict=False):
        older_ladder = older.implied_yes_asks_ladder if side == "yes" else older.implied_no_asks_ladder
        newer_ladder = newer.implied_yes_asks_ladder if side == "yes" else newer.implied_no_asks_ladder
        if not older_ladder or not newer_ladder:
            continue
        comparisons += 1
        older_size = older_ladder[0][1]
        newer_size = newer_ladder[0][1]
        if older_size > 0 and newer_size < (older_size * Decimal("0.5")):
            drops += 1
    if comparisons == 0:
        return Decimal("0.2")
    return Decimal(str(drops / comparisons))


def _average_trade_count(trades: list[TradeSnapshot]) -> Decimal:
    if not trades:
        return Decimal("0")
    return sum(trade.count_fp for trade in trades) / Decimal(len(trades))


def _maker_fill_probability(trades: list[TradeSnapshot], side: str) -> Decimal:
    if not trades:
        return Decimal("0")
    desired_taker_side = "no" if side == "yes" else "yes"
    matched = sum(1 for trade in trades if trade.taker_side == desired_taker_side)
    flow_ratio = Decimal(str(matched / len(trades)))
    participation_multiplier = min(Decimal("1"), Decimal(str(len(trades) / 8.0)))
    return min(Decimal("0.95"), flow_ratio * participation_multiplier)


def _maker_adverse_selection_penalty(trades: list[TradeSnapshot], side: str) -> Decimal:
    if not trades:
        return Decimal("0.04")
    aggressive_count = sum(1 for trade in trades if trade.taker_side == side)
    flow_ratio = Decimal(str(aggressive_count / len(trades)))
    size_penalty = min(Decimal("0.02"), _average_trade_count(trades) / Decimal("250"))
    return Decimal("0.01") + (flow_ratio * Decimal("0.03")) + size_penalty


def _recent_trade_imbalance(trades: list[TradeSnapshot], side: str) -> Decimal:
    if not trades:
        return Decimal("0.5")
    side_count = sum(1 for trade in trades if trade.taker_side == side)
    return Decimal(str(side_count / len(trades)))


def _time_to_close_bucket(as_of_time: datetime, close_time: datetime) -> str:
    seconds = max(0, int((close_time - as_of_time).total_seconds()))
    if seconds <= 900:
        return "lt_15m"
    if seconds <= 3600:
        return "15m_to_1h"
    if seconds <= 14400:
        return "1h_to_4h"
    return "gt_4h"


def _spread(snapshot: OrderbookSnapshot, side: str) -> Decimal:
    top_price = best_implied_ask_price(
        snapshot.implied_yes_asks_ladder if side == "yes" else snapshot.implied_no_asks_ladder
    )
    best_bid = _best_yes_bid(snapshot) if side == "yes" else _best_no_bid(snapshot)
    if top_price is None or best_bid is None:
        return Decimal("1")
    return max(Decimal("0"), top_price - best_bid)


def _spread_blowout_flag(
    history: list[OrderbookSnapshot],
    snapshot: OrderbookSnapshot,
    side: str,
) -> bool:
    historical_spreads = [_spread(item, side) for item in history[1:]]
    historical_spreads = [value for value in historical_spreads if value < Decimal("1")]
    if not historical_spreads:
        return False
    average = sum(historical_spreads) / Decimal(len(historical_spreads))
    if average <= 0:
        return False
    return _spread(snapshot, side) >= average * Decimal("3")


def _taker_adverse_selection_multiplier(time_to_close_bucket: str) -> Decimal:
    if time_to_close_bucket == "gt_4h":
        return Decimal("1.20")
    if time_to_close_bucket == "1h_to_4h":
        return Decimal("1.00")
    if time_to_close_bucket == "15m_to_1h":
        return Decimal("0.90")
    return Decimal("0.75")


def _tradability_time_multiplier(time_to_close_bucket: str) -> Decimal:
    if time_to_close_bucket == "lt_15m":
        return Decimal("0.92")
    if time_to_close_bucket == "15m_to_1h":
        return Decimal("0.97")
    return Decimal("1.00")


def assess_taker_side(
    snapshot: OrderbookSnapshot,
    recent_history: list[OrderbookSnapshot],
    recent_trades: list[TradeSnapshot],
    market_close_time: datetime,
    side: str,
    quantity: Decimal,
) -> tuple[MicrostructureAssessment, TradabilityAssessment]:
    ladder = snapshot.implied_yes_asks_ladder if side == "yes" else snapshot.implied_no_asks_ladder
    top_price = best_implied_ask_price(ladder)
    wap, levels = executable_wap_for_buy(ladder, quantity)
    if top_price is None or wap is None:
        micro = MicrostructureAssessment(
            market_ticker=snapshot.market_ticker,
            side=side,
            quantity_fp=quantity,
            top_of_book_price=top_price,
            executable_wap_price=wap,
            depth_consumed_levels=levels,
            slippage_cost=Decimal("1"),
            top_of_book_durability_score=Decimal("0"),
            quote_stability_score=Decimal("0"),
            ghost_liquidity_ratio=Decimal("1"),
            maker_fill_probability=_maker_fill_probability(recent_trades, side),
            maker_adverse_selection_penalty=_maker_adverse_selection_penalty(recent_trades, side),
            taker_adverse_selection_penalty=Decimal("0.03"),
            time_to_close_bucket=_time_to_close_bucket(snapshot.as_of_time, market_close_time),
        )
        tradability = TradabilityAssessment(
            market_ticker=snapshot.market_ticker,
            side=side,
            tradability_score=Decimal("0"),
            thin_book_flag=True,
            stale_book_flag=False,
            friction_overload_flag=True,
            allowed_taker_flag=False,
            allowed_maker_flag=False,
            block_reasons=("insufficient_depth",),
        )
        return micro, tradability

    slippage = wap - top_price
    durability = _durability_score(recent_history, side)
    stability = _quote_stability_score(recent_history, side)
    ghost = _ghost_liquidity_ratio(recent_history, side)
    spread = _spread(snapshot, side)
    time_to_close_bucket = _time_to_close_bucket(snapshot.as_of_time, market_close_time)
    depth_score = Decimal("1") if levels <= 3 else Decimal("0.6")
    spread_score = (
        max(Decimal("0"), Decimal("1") - (spread / WEATHER_WIDE_SPREAD_THRESHOLD))
        if spread
        else Decimal("1")
    )
    maker_fill_probability = _maker_fill_probability(recent_trades, side)
    maker_adverse_selection_penalty = _maker_adverse_selection_penalty(recent_trades, side)
    same_side_imbalance = _recent_trade_imbalance(recent_trades, side)
    tradability_score = (
        (Decimal("0.30") * depth_score)
        + (Decimal("0.20") * durability)
        + (Decimal("0.15") * stability)
        + (Decimal("0.15") * (Decimal("1") - ghost))
        + (Decimal("0.20") * spread_score)
    )
    tradability_score *= _tradability_time_multiplier(time_to_close_bucket)
    block_reasons = []
    if spread > WEATHER_WIDE_SPREAD_THRESHOLD:
        block_reasons.append("wide_spread")
    if _spread_blowout_flag(recent_history, snapshot, side):
        block_reasons.append("spread_blowout")
    if tradability_score < WEATHER_LOW_TRADABILITY_THRESHOLD:
        block_reasons.append("low_tradability")

    taker_adverse_selection_penalty = (
        Decimal("0.015")
        + (ghost * Decimal("0.025"))
        + (same_side_imbalance * Decimal("0.015"))
    ) * _taker_adverse_selection_multiplier(time_to_close_bucket)

    micro = MicrostructureAssessment(
        market_ticker=snapshot.market_ticker,
        side=side,
        quantity_fp=quantity,
        top_of_book_price=top_price,
        executable_wap_price=wap,
        depth_consumed_levels=levels,
        slippage_cost=slippage,
        top_of_book_durability_score=durability,
        quote_stability_score=stability,
        ghost_liquidity_ratio=ghost,
        maker_fill_probability=maker_fill_probability,
        maker_adverse_selection_penalty=maker_adverse_selection_penalty,
        taker_adverse_selection_penalty=taker_adverse_selection_penalty,
        time_to_close_bucket=time_to_close_bucket,
    )
    tradability = TradabilityAssessment(
        market_ticker=snapshot.market_ticker,
        side=side,
        tradability_score=tradability_score,
        thin_book_flag=False,
        stale_book_flag=False,
        friction_overload_flag=False,
        allowed_taker_flag=not block_reasons,
        allowed_maker_flag=(
            maker_fill_probability >= Decimal("0.18")
            and maker_adverse_selection_penalty <= Decimal("0.05")
            and durability >= Decimal("0.45")
            and stability >= Decimal("0.45")
            and "spread_blowout" not in block_reasons
        ),
        block_reasons=tuple(block_reasons),
    )
    return micro, tradability
