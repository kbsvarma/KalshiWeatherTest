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
# 2026-05-18 PM: aligned default from 10 → 15 to match LIVE_DAILY_USD_CAP in
# run_weather_cycle.sh and analytics.volume_selection.daily_capital_cap_usd.
# The default only kicks in if the env var is unset; in normal operation
# the env var ($15) wins. Keeping the default consistent so that someone
# running a Python tool outside the wrapper sees the same cap.
DEFAULT_DAILY_USD_CAP = 15.0
MIN_REMAINING_BALANCE_USD = 1.0
# Refuse to place live orders on any market settling more than this many days
# out. Current Kalshi behaviour is "today + tomorrow only" so this is a
# defensive guard against future Kalshi listing changes.
MAX_SETTLEMENT_DAYS_AHEAD = 2

# ── Re-entry policy ──────────────────────────────────────────────────────
# When we CLOSE a position (loss-cut or exit signal), we don't want to
# re-bet that exact ticker the same day on weak signals — that's
# ping-pong, racks up fees, and is usually noise. But sometimes the
# model has new information after close that strongly supports the
# original direction. The re-entry policy threads that needle.
#
# A market is allowed to be re-bet on the same day only when ALL of:
#   1. Cooldown elapsed since CLOSE  → REENTRY_MIN_COOLDOWN_MINUTES
#   2. New p_model exceeds at-exit p_model by REENTRY_P_MODEL_BOOST_REQUIRED
#   3. We haven't already used our daily re-entry budget for this ticker
#      (REENTRY_MAX_PER_TICKER_PER_DAY)
#   4. Exit metadata (p_model_at_close) was recorded — legacy CLOSE rows
#      without metadata are treated as "strong conviction exit" → blocked
#      (conservative default)
#
# Bug-class context: replaces the older _market_already_traded_today() gate
# which was binary (ANY same-day touch → block). That gate was correct as
# a starting policy but missed the case where the bot exited at the WRONG
# moment and refused to re-enter even when the model strongly disagreed
# with the exit. Today (2026-05-18) MIA-B86.5 close at 1:01 AM ET blocked
# a re-entry at 2:58 PM ET where p_model had risen to 0.94 — likely a
# legitimate signal we missed.
REENTRY_MIN_COOLDOWN_MINUTES = 60
REENTRY_P_MODEL_BOOST_REQUIRED = Decimal("0.10")
REENTRY_MAX_PER_TICKER_PER_DAY = 1


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
    """Sum the cost of all REAL live orders placed today (UTC).

    2026-05-18 fix: the original implementation read ``p.get("price")`` —
    but the payload schema only stores ``yes_price`` / ``no_price`` (no
    plain ``price`` field). As a result ``today_spend`` always evaluated
    to $0.00 and the daily-cap gate in ``determine_live_execution`` was
    silently non-functional. We were safe by luck (organic daily spend
    stayed below the cap), but the gate wasn't guarding anything.

    Correct cost-per-contract is the price of the SIDE we bought:
      * side == "yes"  →  yes_price (cents)
      * side == "no"   →  no_price  (cents)

    We also now filter to status LIKE 'PLACED_%' so DRY_RUN_PREVIEW and
    ERROR rows don't pollute the tally — same convention used by
    ``_market_already_traded_today`` and the dashboard's account snapshot.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM live_orders "
                "WHERE substr(created_at, 1, 10) = ? "
                "  AND status LIKE 'PLACED_%'",
                (today,),
            ).fetchall()
    except Exception:
        return 0.0
    total = 0.0
    for (raw,) in rows:
        try:
            p = json.loads(raw or "{}")
            count = int(p.get("count") or 0)
            side = (p.get("side") or "").lower()
            if side == "yes":
                price = int(p.get("yes_price") or 0)
            elif side == "no":
                price = int(p.get("no_price") or 0)
            else:
                # Defensive fallback — try yes_price then no_price then 0.
                price = int(p.get("yes_price") or p.get("no_price") or 0)
            total += (price / 100.0) * count
        except Exception:
            continue
    return total


@dataclass(frozen=True, slots=True)
class ReentryDecision:
    """Outcome of the same-day re-entry check.

    block_reason
        ``None`` → market may be (re-)entered; else a short snake_case
        reason suitable for ``blocker_reason``.
    is_reentry
        ``True`` when the new bet is a same-day re-entry after a prior
        close (informational: caller tags the new PLACED row so the
        daily re-entry cap counts it). ``False`` when this is the
        first touch today or when blocked.
    """
    block_reason: str | None
    is_reentry: bool


def _check_reentry_policy(
    store: SQLiteStateStore,
    market_ticker: str,
    *,
    current_p_model: Decimal,
) -> ReentryDecision:
    """Return a ``ReentryDecision`` describing whether re-entry is allowed.

    block_reason is ``None`` when the market may be (re-)entered today,
    else a snake_case reason suitable for ``blocker_reason``.

    Replaces the older binary ``_market_already_traded_today`` gate. The
    new policy threads four conditions; failing any one returns a
    descriptive reason naming the failure mode so daily reports can
    distinguish "re-entry signal weak" from "ticker already held" from
    "in cooldown."

    Semantics:

      * **No same-day rows (excluding DRY_RUN_*/ERROR)** → allow.
      * **PLACED_* row without a matching CLOSED_* on the same day** →
        block. The position is currently held — this isn't a re-entry
        case, it's a duplicate-bet case, handled the same as before.
      * **PLACED_* + CLOSED_* on the same day** → re-entry candidate.
        Evaluate cooldown + signal-boost + daily-cap; allow only if all
        pass. Legacy CLOSED_* rows lacking ``exit_metadata.p_model_at_close``
        are treated as strong-conviction exits → blocked (conservative
        default for rows recorded before this policy shipped).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT created_at, status, payload_json FROM live_orders "
                "WHERE market_ticker = ? "
                "AND substr(created_at, 1, 10) = ? "
                "AND status NOT IN ('DRY_RUN_PREVIEW', 'ERROR', 'DRY_RUN', 'DRY_RUN_CLOSE_PREVIEW') "
                "ORDER BY created_at",
                (market_ticker, today),
            ).fetchall()
    except Exception:
        # If we can't reach the DB, fail closed — don't risk a duplicate bet.
        return ReentryDecision("reentry_check_db_unavailable", False)

    if not rows:
        return ReentryDecision(None, False)  # First touch today — allow.

    # Categorize the same-day rows.
    placed_rows: list[tuple[str, dict[str, Any]]] = []
    closed_rows: list[tuple[str, dict[str, Any]]] = []
    reentry_rows: list[tuple[str, dict[str, Any]]] = []
    for created_at, status, payload_raw in rows:
        try:
            payload = json.loads(payload_raw or "{}")
        except Exception:
            payload = {}
        if status.startswith("PLACED_"):
            if payload.get("is_reentry"):
                reentry_rows.append((created_at, payload))
            else:
                placed_rows.append((created_at, payload))
        elif status.startswith("CLOSED_"):
            closed_rows.append((created_at, payload))
        elif status.startswith("CLOSE_"):
            # CLOSE_ERROR — close attempt that failed at the exchange. Be
            # conservative: don't allow re-entry on top of an unknown exit
            # state, the existing position may still be open.
            return ReentryDecision("reentry_blocked_after_close_error", False)

    # If the most recent thing we did wasn't a close, we still hold the
    # position. Block (duplicate-bet case).
    if not closed_rows:
        return ReentryDecision("market_already_held_today", False)
    last_placed_at = max((ts for ts, _ in placed_rows + reentry_rows), default="")
    last_closed_at = max(ts for ts, _ in closed_rows)
    if last_placed_at > last_closed_at:
        # PLACED after CLOSED → currently held.
        return ReentryDecision("market_already_held_today", False)

    # Re-entry gate: most recent action was a CLOSE. Check the three
    # re-entry conditions. We're in a re-entry SITUATION; whether it's
    # allowed is what the checks below decide.

    # (1) Daily re-entry cap — already used the budget?
    if len(reentry_rows) >= REENTRY_MAX_PER_TICKER_PER_DAY:
        return ReentryDecision(
            f"reentry_cap_reached ({len(reentry_rows)}/{REENTRY_MAX_PER_TICKER_PER_DAY})",
            True,
        )

    # (2) Cooldown since most recent close.
    try:
        close_dt = datetime.fromisoformat(last_closed_at.replace("Z", "+00:00"))
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        minutes_since_close = (
            (datetime.now(timezone.utc) - close_dt).total_seconds() / 60.0
        )
    except Exception:
        return ReentryDecision("reentry_close_timestamp_unparseable", True)
    if minutes_since_close < REENTRY_MIN_COOLDOWN_MINUTES:
        return ReentryDecision(
            f"reentry_cooldown ({minutes_since_close:.0f}min "
            f"< {REENTRY_MIN_COOLDOWN_MINUTES}min)",
            True,
        )

    # (3) Signal-boost requirement: new p_model must exceed at-exit p_model
    #     by at least REENTRY_P_MODEL_BOOST_REQUIRED. Legacy CLOSE rows
    #     without metadata fail this check (conservative default).
    last_close_payload = max(closed_rows, key=lambda x: x[0])[1]
    exit_meta = last_close_payload.get("exit_metadata") or {}
    exit_p_model_raw = exit_meta.get("p_model_at_close")
    if exit_p_model_raw is None:
        return ReentryDecision("reentry_blocked_legacy_close_no_metadata", True)
    try:
        exit_p_model = Decimal(str(exit_p_model_raw))
    except Exception:
        return ReentryDecision("reentry_exit_p_model_unparseable", True)
    boost = current_p_model - exit_p_model
    if boost < REENTRY_P_MODEL_BOOST_REQUIRED:
        return ReentryDecision(
            f"reentry_signal_too_weak (boost={boost:.3f} "
            f"< {REENTRY_P_MODEL_BOOST_REQUIRED})",
            True,
        )

    # All checks passed — allow this re-entry.
    return ReentryDecision(None, True)


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
    p_model_at_close: Decimal | None = None,
    close_reason: str = "unspecified",
) -> LiveExecutionResult:
    """Close an open position by submitting a sell IOC order.

    Called when the decision engine emits EXIT for a market we own AND
    selling now would lock in a loss smaller than holding to settlement
    risk would (caller is responsible for that judgement). This function
    just executes the close; it does not decide whether to.

    side_held: 'yes' or 'no' — what we're holding.
    entry_price_cents / current_sell_bid_cents: in cents (1..99).
    p_model_at_close: the model's probability for the held side at the
        moment we decided to close. Written into the CLOSED row's
        ``payload_json.exit_metadata`` so the re-entry policy can later
        compare against a new signal. ``None`` is recorded as missing
        metadata, which the policy treats as a strong-conviction exit.
    close_reason: short tag for the close motive, e.g.
        ``underwater_exit_signal`` or ``manual``.
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
    # exit_metadata travels with every CLOSE row (real, dry-run, error).
    # The re-entry policy reads p_model_at_close from here. Storing it on
    # ALL close rows (not just successful ones) keeps the policy correct
    # even when the close itself partially fails.
    exit_metadata = {
        "p_model_at_close": str(p_model_at_close) if p_model_at_close is not None else None,
        "side_held_at_close": side_held,
        "close_reason": close_reason,
    }

    if _is_dry_run():
        _record_live_order(
            store, market_ticker=market_ticker,
            payload={**order_payload, "close_intent": True,
                     "entry_price_cents": entry_price_cents,
                     "exit_metadata": exit_metadata},
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
                     "exit_metadata": exit_metadata,
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
                 "exit_metadata": exit_metadata,
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

    # Gate 3: duplicate-market / re-entry policy. Replaces the old binary
    # "already traded today → block" with the conditional re-entry policy
    # documented at REENTRY_* constants. Allows re-bet when the model has
    # genuinely new conviction after an earlier close.
    reentry = _check_reentry_policy(
        store,
        explanation.market_ticker,
        current_p_model=Decimal(str(edge.p_model)),
    )
    if reentry.block_reason is not None:
        return LiveExecutionResult(
            placed=False, dry_run=False, order_id=None,
            blocker_reason=reentry.block_reason, price_cents=None,
            quantity=None, response=None,
        )
    # ``reentry.is_reentry`` is True iff this is a same-day re-entry after
    # close (passes through to the PLACED row tag below so the next-cycle
    # re-entry-cap check counts it).
    is_reentry = reentry.is_reentry

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
            # is_reentry: True iff this is a same-day re-buy of a market
            # we closed earlier today. Counted by the re-entry cap on
            # the NEXT evaluation of this ticker.
            "is_reentry": is_reentry,
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
