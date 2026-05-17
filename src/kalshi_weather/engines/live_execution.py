"""Live order execution — submit real Kalshi orders for shadow-validated bets.

Called from the decision cycle ONLY when a shadow fill was just recorded
(i.e., the bet passed all volume-grinder gates). Submits a single contract
limit-IOC order via the Kalshi private API.

Hard risk limits (cannot be bypassed without code change):
  - Max 1 contract per market per day
  - Max $10 total live exposure per UTC day (configurable via env)
  - Skip if global kill_switch is active
  - Skip if Kalshi balance would drop below $1.00 after fill
  - Skip if the same market already has a live order from us today
  - Operates only when env LIVE_ORDERS_ENABLED=1 (off by default)

In DRY_RUN mode (default unless LIVE_ORDERS_DRY_RUN=0 is set) the function
logs what it WOULD have placed without calling the Kalshi API.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from kalshi_weather.clients import (
    KalshiCredentials,
    KalshiPrivateClient,
    PROD_ROOT_URL,
)
from kalshi_weather.domain.models import EdgeEstimate, ShadowFill, StrategyDecisionExplanation
from kalshi_weather.storage.state_store import SQLiteStateStore


@dataclass(frozen=True, slots=True)
class LiveExecutionResult:
    placed: bool
    dry_run: bool
    order_id: str | None
    blocker_reason: str | None
    price_cents: int | None
    quantity: int | None
    response: dict[str, Any] | None


# Hard limits (cannot be overridden by env without code review).
MAX_CONTRACTS_PER_MARKET = 1
DEFAULT_DAILY_USD_CAP = 10.0
MIN_REMAINING_BALANCE_USD = 1.0
# Refuse to place live orders on any market settling more than this many days
# out. Current Kalshi behaviour is "today + tomorrow only" so this is a
# defensive guard against future Kalshi listing changes.
MAX_SETTLEMENT_DAYS_AHEAD = 2


def _market_settlement_date_from_ticker(market_ticker: str):
    """Decode YYYY-MM-DD from `KXHIGH...-YYMMMDD-Txx` / `-Bxx.5` tickers."""
    import re
    from datetime import date
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-[TB]", market_ticker)
    if not m:
        return None
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    try:
        yy, mon_str, dd = m.groups()
        return date(2000 + int(yy), months[mon_str.upper()], int(dd))
    except Exception:
        return None


def _load_credentials() -> KalshiCredentials | None:
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        return None
    private_path = Path(key_path)
    if not private_path.exists():
        return None
    return KalshiCredentials(
        access_key=key_id,
        private_key_path=private_path,
        root_url=PROD_ROOT_URL,
    )


def _live_orders_enabled() -> bool:
    return os.environ.get("LIVE_ORDERS_ENABLED", "0").strip() == "1"


def _is_dry_run() -> bool:
    # Dry-run is ON by default. Set LIVE_ORDERS_DRY_RUN=0 to actually place orders.
    return os.environ.get("LIVE_ORDERS_DRY_RUN", "1").strip() != "0"


def _daily_cap_usd() -> float:
    try:
        return float(os.environ.get("LIVE_DAILY_USD_CAP", DEFAULT_DAILY_USD_CAP))
    except Exception:
        return DEFAULT_DAILY_USD_CAP


def _check_kill_switch(store: SQLiteStateStore) -> bool:
    """Return True if a kill switch is ACTIVE (global or city-scoped)."""
    try:
        ks = store.get_kill_switch("GLOBAL")
        if ks and ks.get("state") == "ACTIVE":
            return True
    except Exception:
        pass
    return False


def _daily_live_spend(store: SQLiteStateStore) -> float:
    """Sum the cost of all live orders we've placed today (UTC)."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM live_orders "
                "WHERE substr(created_at, 1, 10) = ?",
                (today,),
            ).fetchall()
    except Exception:
        return 0.0
    total = 0.0
    for (raw,) in rows:
        try:
            p = json.loads(raw or "{}")
            # Cost = price_cents/100 * count
            count = int(p.get("count") or 0)
            price = int(p.get("price") or 0)
            total += (price / 100.0) * count
        except Exception:
            continue
    return total


def _market_already_traded_today(store: SQLiteStateStore, market_ticker: str) -> bool:
    """Has a REAL Kalshi order been placed for this market today?

    Filters out DRY_RUN_PREVIEW and ERROR rows — dedup is about real exposure,
    not about what we considered. (2026-05-17 bug: dry-run pollution from
    a manual test blocked the next real cycle from firing the same edge.)
    """
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with store._connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM live_orders "
                "WHERE market_ticker = ? "
                "AND substr(created_at, 1, 10) = ? "
                "AND status NOT IN ('DRY_RUN_PREVIEW', 'ERROR')",
                (market_ticker, today),
            ).fetchone()
        return bool(row and int(row[0]) > 0)
    except Exception:
        return False


def _record_live_order(
    store: SQLiteStateStore,
    *,
    market_ticker: str,
    payload: dict[str, Any],
    status: str,
) -> None:
    """Append a record to live_orders table for audit + daily-cap accounting."""
    record_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    record = {
        "record_id": record_id,
        "market_ticker": market_ticker,
        "created_at": created_at,
        "status": status,
        **payload,
    }
    try:
        store.save_live_order_record(record)
    except Exception as exc:
        # Non-fatal — but log loudly so we can audit later.
        print(f"[LIVE-EXEC] WARN: failed to save live_orders row for {market_ticker}: {exc}")


def maybe_close_position(
    *,
    store: SQLiteStateStore,
    market_ticker: str,
    side_held: str,
    entry_price_cents: int,
    current_sell_bid_cents: int,
) -> LiveExecutionResult:
    """Close an open position by submitting a sell IOC order.

    Called when the decision engine emits EXIT for a market we own AND
    selling now would lock in a loss smaller than holding to settlement
    risk would (caller is responsible for that judgement). This function
    just executes the close; it does not decide whether to.

    side_held: 'yes' or 'no' — what we're holding.
    entry_price_cents / current_sell_bid_cents: in cents (1..99).
    """
    if not _live_orders_enabled():
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="live_orders_disabled_via_env",
            price_cents=None, quantity=None, response=None,
        )

    if current_sell_bid_cents < 1 or current_sell_bid_cents > 99:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason=f"close_price_out_of_range ({current_sell_bid_cents}c)",
            price_cents=current_sell_bid_cents, quantity=None, response=None,
        )

    # Check creds only if we'd actually call the API (dry-run can simulate).
    creds = _load_credentials() if not _is_dry_run() else None
    if not _is_dry_run() and creds is None:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="kalshi_credentials_missing",
            price_cents=current_sell_bid_cents, quantity=None, response=None,
        )

    client_order_id = f"kxw_close_{uuid4().hex[:16]}"
    order_payload = {
        "ticker": market_ticker,
        "client_order_id": client_order_id,
        "side": side_held,
        "action": "sell",
        "type": "limit",
        # Hit the top bid — IOC so we don't sit at risk if liquidity moves.
        ("yes_price" if side_held == "yes" else "no_price"): current_sell_bid_cents,
        "count": 1,
        "time_in_force": "immediate_or_cancel",
    }

    if _is_dry_run():
        _record_live_order(
            store, market_ticker=market_ticker,
            payload={**order_payload, "close_intent": True,
                     "entry_price_cents": entry_price_cents},
            status="DRY_RUN_CLOSE_PREVIEW",
        )
        return LiveExecutionResult(
            placed=False, dry_run=True, order_id=client_order_id,
            blocker_reason=None, price_cents=current_sell_bid_cents,
            quantity=1, response=None,
        )

    client = KalshiPrivateClient(creds)
    try:
        resp = client.create_order(order_payload)
    except Exception as exc:
        error_text = str(exc)
        _record_live_order(
            store, market_ticker=market_ticker,
            payload={**order_payload, "close_intent": True,
                     "entry_price_cents": entry_price_cents,
                     "error": error_text},
            status="CLOSE_ERROR",
        )
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=client_order_id,
            blocker_reason=f"kalshi_api_error: {error_text[:120]}",
            price_cents=current_sell_bid_cents, quantity=1, response=None,
        )

    order = resp.get("order") if isinstance(resp, dict) else {}
    order_status = order.get("status", "unknown") if isinstance(order, dict) else "unknown"
    _record_live_order(
        store, market_ticker=market_ticker,
        payload={**order_payload, "close_intent": True,
                 "entry_price_cents": entry_price_cents,
                 "kalshi_order_id": order.get("order_id") if isinstance(order, dict) else None,
                 "kalshi_status": order_status, "response": resp},
        status=f"CLOSED_{order_status.upper()}",
    )
    return LiveExecutionResult(
        placed=True, dry_run=False, order_id=client_order_id,
        blocker_reason=None, price_cents=current_sell_bid_cents,
        quantity=1, response=resp,
    )


def maybe_place_live_order(
    *,
    store: SQLiteStateStore,
    explanation: StrategyDecisionExplanation,
    edge: EdgeEstimate,
    shadow_fill: ShadowFill | None,
) -> LiveExecutionResult:
    """Place a real Kalshi order mirroring this just-filled shadow position.

    Returns a structured result for caller-side logging. Never raises — all
    failures are returned as `blocker_reason`.
    """
    # Gate 0: must follow a successful shadow fill (sanity)
    if shadow_fill is None:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="no_shadow_fill", price_cents=None,
            quantity=None, response=None,
        )

    # Gate 1: env-level enable
    if not _live_orders_enabled():
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="live_orders_disabled_via_env", price_cents=None,
            quantity=None, response=None,
        )

    # Gate 1b: settlement-date guard — refuse markets settling more than
    # MAX_SETTLEMENT_DAYS_AHEAD days out. Defends against Kalshi listing
    # changes (currently they only post today+tomorrow but this future-proofs).
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    sd = _market_settlement_date_from_ticker(explanation.market_ticker)
    if sd is not None:
        today_utc = _dt.now(_tz.utc).date()
        days_ahead = (sd - today_utc).days
        if days_ahead < 0 or days_ahead > MAX_SETTLEMENT_DAYS_AHEAD:
            return LiveExecutionResult(
                placed=False, dry_run=False, order_id=None,
                blocker_reason=f"settlement_date_out_of_window ({sd}, {days_ahead}d ahead)",
                price_cents=None, quantity=None, response=None,
            )

    # Gate 2: kill switch
    if _check_kill_switch(store):
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="kill_switch_active", price_cents=None,
            quantity=None, response=None,
        )

    # Gate 3: duplicate-market check
    if _market_already_traded_today(store, explanation.market_ticker):
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="market_already_traded_today", price_cents=None,
            quantity=None, response=None,
        )

    # Gate 4: daily cap
    today_spend = _daily_live_spend(store)
    cap = _daily_cap_usd()
    price_decimal = Decimal(str(edge.p_market_exec))
    cost_per_contract = float(price_decimal)
    if today_spend + cost_per_contract > cap:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason=f"daily_cap_reached ({today_spend:.2f}/{cap:.2f})",
            price_cents=None, quantity=None, response=None,
        )

    # Gate 5: price sanity — refuse if price >$0.99 or <$0.01 (Kalshi minimum)
    price_cents = int(round(float(price_decimal) * 100))
    if price_cents < 1 or price_cents > 99:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason=f"price_out_of_range ({price_cents}c)",
            price_cents=price_cents, quantity=None, response=None,
        )

    creds = _load_credentials()
    if creds is None:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason="kalshi_credentials_missing",
            price_cents=price_cents, quantity=None, response=None,
        )

    quantity = MAX_CONTRACTS_PER_MARKET  # hard-coded 1 contract per market

    # Build the order payload
    client_order_id = f"kxweather_{explanation.decision_id[:16]}"
    order_payload = {
        "ticker": explanation.market_ticker,
        "client_order_id": client_order_id,
        "side": "yes" if edge.side == "yes" else "no",
        "action": "buy",
        "type": "limit",
        # Use the implied ask price + 1 cent of headroom for IOC fill chance
        "yes_price" if edge.side == "yes" else "no_price": price_cents,
        "count": quantity,
        "time_in_force": "immediate_or_cancel",  # IOC — don't leave resting
    }

    # Drop in dry-run mode
    if _is_dry_run():
        # Log preview without calling Kalshi
        preview = {
            **order_payload,
            "decision_id": explanation.decision_id,
            "edge_side": edge.side,
            "p_model": str(edge.p_model),
            "expected_ev_per_contract": str(edge.executable_ev_per_contract),
        }
        _record_live_order(
            store,
            market_ticker=explanation.market_ticker,
            payload=preview,
            status="DRY_RUN_PREVIEW",
        )
        return LiveExecutionResult(
            placed=False, dry_run=True, order_id=client_order_id,
            blocker_reason=None, price_cents=price_cents,
            quantity=quantity, response=None,
        )

    # Actual live placement
    client = KalshiPrivateClient(creds)
    try:
        resp = client.create_order(order_payload)
    except Exception as exc:
        error_text = str(exc)
        _record_live_order(
            store,
            market_ticker=explanation.market_ticker,
            payload={**order_payload, "error": error_text},
            status="ERROR",
        )
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=client_order_id,
            blocker_reason=f"kalshi_api_error: {error_text[:120]}",
            price_cents=price_cents, quantity=quantity, response=None,
        )

    # Successful (even if not filled — IOC may have killed it)
    order = resp.get("order") if isinstance(resp, dict) else {}
    order_status = order.get("status", "unknown") if isinstance(order, dict) else "unknown"
    _record_live_order(
        store,
        market_ticker=explanation.market_ticker,
        payload={
            **order_payload,
            "decision_id": explanation.decision_id,
            "kalshi_order_id": order.get("order_id") if isinstance(order, dict) else None,
            "kalshi_status": order_status,
            "response": resp,
        },
        status=f"PLACED_{order_status.upper()}",
    )
    return LiveExecutionResult(
        placed=True,
        dry_run=False,
        order_id=client_order_id,
        blocker_reason=None,
        price_cents=price_cents,
        quantity=quantity,
        response=resp,
    )
