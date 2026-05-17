from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any, Mapping


_TICKER_DATE_PATTERN = re.compile(r"-(\d{2}[A-Z]{3}\d{2})")
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(frozen=True, slots=True)
class SportsCloserConfig:
    entry_price_min: Decimal = Decimal("0.80")
    entry_price_max: Decimal = Decimal("0.92")
    max_spread_dollars: Decimal = Decimal("0.10")
    min_minutes_to_expiration: int = 15
    max_minutes_to_expiration: int = 180
    confirmation_lookback_minutes: int = 5
    min_confirmation_points: int = 3


def _fmt_decimal(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def parse_market_date_from_ticker(ticker: str) -> date | None:
    match = _TICKER_DATE_PATTERN.search(ticker)
    if match is None:
        return None
    token = match.group(1)
    try:
        year = 2000 + int(token[:2])
        month = _MONTHS[token[2:5]]
        day = int(token[5:])
    except Exception:
        return None
    return date(year, month, day)


def filter_markets_for_target_date(
    markets: list[Mapping[str, Any]],
    *,
    target_date: date,
) -> list[Mapping[str, Any]]:
    return [
        payload
        for payload in markets
        if parse_market_date_from_ticker(str(payload.get("ticker") or "")) == target_date
    ]


def group_markets_by_event(markets: list[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for payload in markets:
        event_ticker = str(payload.get("event_ticker") or "")
        if not event_ticker:
            continue
        grouped.setdefault(event_ticker, []).append(payload)
    return grouped


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _expected_expiration(payload: Mapping[str, Any]) -> datetime | None:
    for key in ("expected_expiration_time", "expiration_time", "close_time"):
        value = payload.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                continue
    return None


def _yes_ask_close(candle: Mapping[str, Any]) -> Decimal | None:
    yes_ask = candle.get("yes_ask") or {}
    return _decimal(yes_ask.get("close") if "close" in yes_ask else yes_ask.get("close_dollars"))


def _yes_bid_close(candle: Mapping[str, Any]) -> Decimal | None:
    yes_bid = candle.get("yes_bid") or {}
    return _decimal(yes_bid.get("close") if "close" in yes_bid else yes_bid.get("close_dollars"))


def _side_ask_close(candle: Mapping[str, Any], side: str) -> Decimal | None:
    if side == "yes":
        return _yes_ask_close(candle)
    yes_bid_close = _yes_bid_close(candle)
    if yes_bid_close is None:
        return None
    return Decimal("1") - yes_bid_close


def _risk_reward_ratio(price: Decimal | None) -> Decimal | None:
    if price is None or price <= 0:
        return None
    reward = Decimal("1") - price
    if reward < 0:
        return None
    return reward / price


def _candidate_notes(
    *,
    side: str,
    ask: Decimal | None,
    bid: Decimal | None,
    spread: Decimal | None,
    minutes_to_expiration: int | None,
    recent_closes: list[Decimal],
    config: SportsCloserConfig,
    ready: bool,
    reason: str | None,
) -> tuple[str, ...]:
    notes: list[str] = [f"side={side}"]
    if ask is not None:
        notes.append(f"ask={_fmt_decimal(ask)}")
        notes.append(f"risk_per_share={_fmt_decimal(ask)}")
        notes.append(f"reward_per_share={_fmt_decimal(Decimal('1') - ask)}")
    if bid is not None:
        notes.append(f"bid={_fmt_decimal(bid)}")
    if spread is not None:
        notes.append(
            f"spread={_fmt_decimal(spread)} (max {config.max_spread_dollars})"
        )
    if minutes_to_expiration is not None:
        notes.append(
            "minutes_to_expiration="
            f"{minutes_to_expiration} (window {config.min_minutes_to_expiration}-{config.max_minutes_to_expiration})"
        )
    if recent_closes:
        notes.append(
            "recent_confirmation="
            + " -> ".join(_fmt_decimal(value) for value in recent_closes)
        )
    ratio = _risk_reward_ratio(ask)
    if ratio is not None:
        notes.append(f"risk_reward_ratio={_fmt_decimal(ratio)}")
    if ready:
        notes.append(
            "placed_candidate because price stayed inside the entry band and recent quotes confirmed momentum"
        )
        return tuple(notes)
    reason_notes = {
        "missing_ask": "skipped because the market did not expose an ask quote for this side",
        "missing_expected_expiration": "skipped because expected expiration was missing, so time-to-close could not be trusted",
        "too_close_to_expiration": "skipped because the market is already too close to start/expiry for a clean closer entry",
        "too_early_for_closer_entry": "skipped because the game is still too far away; we only want the late closer window",
        "price_below_entry_band": "skipped because the side is not strong enough yet to count as a closer signal",
        "price_above_entry_band": "skipped because the price is too expensive for the configured entry band",
        "missing_bid": "skipped because bid data was missing, so spread quality could not be checked",
        "spread_too_wide": "skipped because the spread is wider than the strategy allows",
        "insufficient_recent_quote_history": "skipped because there were not enough recent quote points to confirm the move",
        "confirmation_outside_entry_band": "skipped because recent quotes did not hold inside the entry band",
        "momentum_not_confirmed": "skipped because recent quotes were fading instead of holding or strengthening",
    }
    notes.append(reason_notes.get(reason or "", "skipped because the candidate failed the closer rules"))
    return tuple(notes)


def _recent_side_closes(
    candles: list[Mapping[str, Any]],
    *,
    side: str,
    now: datetime,
    lookback_minutes: int,
) -> list[Decimal]:
    cutoff_ts = int(now.timestamp()) - (lookback_minutes * 60)
    recent = [
        _side_ask_close(candle, side)
        for candle in candles
        if int(candle.get("end_period_ts") or 0) >= cutoff_ts
    ]
    return [value for value in recent if value is not None]


def evaluate_market_candidate(
    market: Mapping[str, Any],
    *,
    side: str,
    candles: list[Mapping[str, Any]],
    now: datetime,
    config: SportsCloserConfig,
) -> dict[str, Any]:
    ask = _decimal(market.get(f"{side}_ask_dollars"))
    bid = _decimal(market.get(f"{side}_bid_dollars"))
    expected_expiration = _expected_expiration(market)
    base_title = str(market.get("title") or "")
    base_market_ticker = str(market.get("ticker") or "")
    if ask is None:
        return {
            "ready": False,
            "skip_reason": "missing_ask",
            "side": side,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=None,
                bid=None,
                spread=None,
                minutes_to_expiration=None,
                recent_closes=[],
                config=config,
                ready=False,
                reason="missing_ask",
            ),
        }
    if expected_expiration is None:
        return {
            "ready": False,
            "skip_reason": "missing_expected_expiration",
            "side": side,
            "ask_price": ask,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=None,
                minutes_to_expiration=None,
                recent_closes=[],
                config=config,
                ready=False,
                reason="missing_expected_expiration",
            ),
        }
    minutes_to_expiration = max(
        0,
        int((expected_expiration - now).total_seconds() // 60),
    )
    if minutes_to_expiration < config.min_minutes_to_expiration:
        return {
            "ready": False,
            "skip_reason": "too_close_to_expiration",
            "side": side,
            "ask_price": ask,
            "minutes_to_expiration": minutes_to_expiration,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=None,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=[],
                config=config,
                ready=False,
                reason="too_close_to_expiration",
            ),
        }
    if minutes_to_expiration > config.max_minutes_to_expiration:
        return {
            "ready": False,
            "skip_reason": "too_early_for_closer_entry",
            "side": side,
            "ask_price": ask,
            "minutes_to_expiration": minutes_to_expiration,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=None,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=[],
                config=config,
                ready=False,
                reason="too_early_for_closer_entry",
            ),
        }
    if ask < config.entry_price_min:
        return {
            "ready": False,
            "skip_reason": "price_below_entry_band",
            "side": side,
            "ask_price": ask,
            "minutes_to_expiration": minutes_to_expiration,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=None,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=[],
                config=config,
                ready=False,
                reason="price_below_entry_band",
            ),
        }
    if ask > config.entry_price_max:
        return {
            "ready": False,
            "skip_reason": "price_above_entry_band",
            "side": side,
            "ask_price": ask,
            "minutes_to_expiration": minutes_to_expiration,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=None,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=[],
                config=config,
                ready=False,
                reason="price_above_entry_band",
            ),
        }
    if bid is None:
        return {
            "ready": False,
            "skip_reason": "missing_bid",
            "side": side,
            "ask_price": ask,
            "minutes_to_expiration": minutes_to_expiration,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=None,
                spread=None,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=[],
                config=config,
                ready=False,
                reason="missing_bid",
            ),
        }
    spread = ask - bid
    if spread > config.max_spread_dollars:
        return {
            "ready": False,
            "skip_reason": "spread_too_wide",
            "side": side,
            "ask_price": ask,
            "bid_price": bid,
            "spread_dollars": spread,
            "minutes_to_expiration": minutes_to_expiration,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=spread,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=[],
                config=config,
                ready=False,
                reason="spread_too_wide",
            ),
        }
    recent_closes = _recent_side_closes(
        candles,
        side=side,
        now=now,
        lookback_minutes=config.confirmation_lookback_minutes,
    )
    if len(recent_closes) < config.min_confirmation_points:
        return {
            "ready": False,
            "skip_reason": "insufficient_recent_quote_history",
            "side": side,
            "ask_price": ask,
            "bid_price": bid,
            "spread_dollars": spread,
            "minutes_to_expiration": minutes_to_expiration,
            "recent_closes": recent_closes,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=spread,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=recent_closes,
                config=config,
                ready=False,
                reason="insufficient_recent_quote_history",
            ),
        }
    trailing = recent_closes[-config.min_confirmation_points :]
    if any(
        value < config.entry_price_min or value > config.entry_price_max
        for value in trailing
    ):
        return {
            "ready": False,
            "skip_reason": "confirmation_outside_entry_band",
            "side": side,
            "ask_price": ask,
            "bid_price": bid,
            "spread_dollars": spread,
            "minutes_to_expiration": minutes_to_expiration,
            "recent_closes": trailing,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=spread,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=trailing,
                config=config,
                ready=False,
                reason="confirmation_outside_entry_band",
            ),
        }
    if trailing[-1] < trailing[0]:
        return {
            "ready": False,
            "skip_reason": "momentum_not_confirmed",
            "side": side,
            "ask_price": ask,
            "bid_price": bid,
            "spread_dollars": spread,
            "minutes_to_expiration": minutes_to_expiration,
            "recent_closes": trailing,
            "market_ticker": base_market_ticker,
            "title": base_title,
            "notes": _candidate_notes(
                side=side,
                ask=ask,
                bid=bid,
                spread=spread,
                minutes_to_expiration=minutes_to_expiration,
                recent_closes=trailing,
                config=config,
                ready=False,
                reason="momentum_not_confirmed",
            ),
        }
    return {
        "ready": True,
        "skip_reason": None,
        "side": side,
        "ask_price": ask,
        "bid_price": bid,
        "spread_dollars": spread,
        "minutes_to_expiration": minutes_to_expiration,
        "recent_closes": trailing,
        "selection_score": ask - spread,
        "expected_payout_per_share": Decimal("1") - ask,
        "max_loss_per_share": ask,
        "risk_reward_ratio": _risk_reward_ratio(ask),
        "market_ticker": base_market_ticker,
        "title": base_title,
        "notes": _candidate_notes(
            side=side,
            ask=ask,
            bid=bid,
            spread=spread,
            minutes_to_expiration=minutes_to_expiration,
            recent_closes=trailing,
            config=config,
            ready=True,
            reason=None,
        ),
    }


def evaluate_event(
    event_ticker: str,
    markets: list[Mapping[str, Any]],
    *,
    candles_by_market: Mapping[str, list[Mapping[str, Any]]],
    now: datetime,
    config: SportsCloserConfig,
) -> dict[str, Any]:
    evaluated: list[dict[str, Any]] = []
    for market in sorted(markets, key=lambda payload: str(payload.get("ticker") or "")):
        ticker = str(market.get("ticker") or "")
        title = str(market.get("title") or "")
        for side in ("yes", "no"):
            result = evaluate_market_candidate(
                market,
                side=side,
                candles=list(candles_by_market.get(ticker, ())),
                now=now,
                config=config,
            )
            result.update(
                {
                    "market_ticker": ticker,
                    "event_ticker": event_ticker,
                    "title": title,
                    "series_ticker": str(ticker.split("-", 1)[0] or ""),
                    "expected_expiration_time": _expected_expiration(market),
                }
            )
            evaluated.append(result)
    ready = [item for item in evaluated if bool(item.get("ready"))]
    reason_counts: dict[str, int] = {}
    for item in evaluated:
        reason = str(item.get("skip_reason") or "ready")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    if ready:
        best = max(
            ready,
            key=lambda item: (
                Decimal(str(item.get("selection_score") or "0")),
                Decimal(str(item.get("ask_price") or "0")),
            ),
        )
        best_blocked = max(
            [item for item in evaluated if not bool(item.get("ready"))] or evaluated,
            key=lambda item: (
                Decimal(str(item.get("ask_price") or "0")),
                Decimal("0") - Decimal(str(item.get("spread_dollars") or "999")),
            ),
        )
        return {
            "event_ticker": event_ticker,
            "decision": "BET",
            "selected_candidate": best,
            "best_ready_candidate": best,
            "best_blocked_candidate": best_blocked if not bool(best_blocked.get("ready")) else None,
            "all_candidates": evaluated,
            "skip_reason": None,
            "reason_counts": reason_counts,
            "decision_notes": (
                f"placed {best['side']} on {best['market_ticker']}",
                f"ask {_fmt_decimal(best.get('ask_price'))} stayed inside the {config.entry_price_min}-{config.entry_price_max} band",
                f"spread {_fmt_decimal(best.get('spread_dollars'))} cleared the {config.max_spread_dollars} max",
                f"confirmation window held for {config.min_confirmation_points} recent quotes",
            ),
        }
    if not evaluated:
        return {
            "event_ticker": event_ticker,
            "decision": "SKIP",
            "selected_candidate": None,
            "best_ready_candidate": None,
            "best_blocked_candidate": None,
            "all_candidates": [],
            "skip_reason": "no_markets_available",
            "reason_counts": {},
            "decision_notes": ("skipped because no contracts were available for this event",),
        }
    best_blocked = max(
        evaluated,
        key=lambda item: (
            Decimal(str(item.get("ask_price") or "0")),
            Decimal("0") - Decimal(str(item.get("spread_dollars") or "999")),
        ),
    )
    return {
        "event_ticker": event_ticker,
        "decision": "SKIP",
        "selected_candidate": best_blocked,
        "best_ready_candidate": None,
        "best_blocked_candidate": best_blocked,
        "all_candidates": evaluated,
        "skip_reason": str(best_blocked.get("skip_reason") or "unknown"),
        "reason_counts": reason_counts,
        "decision_notes": (
            f"skipped because the best candidate was {best_blocked.get('side')} on {best_blocked.get('market_ticker')}",
            f"dominant blocker: {best_blocked.get('skip_reason')}",
        )
        + tuple(best_blocked.get("notes") or ()),
    }


def default_now_utc() -> datetime:
    return datetime.now(timezone.utc)
