"""Recommendation log — capture every decision (BET / WATCH / SKIP) for later
counterfactual P&L analysis.

The decision cycle calls `record_recommendation(...)` after each market
evaluation. Records flow into the `market_recommendations` table with
enough state to:
 - know what side the bot would have bet if it had bet
 - know why it skipped (rejection_reasons list)
 - resolve counterfactual P&L once settlement arrives

A separate resolver (`resolve_pending_recommendations`) walks unresolved
rows nightly and stamps the actual outcome from `market_settlements`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from kalshi_weather.domain.enums import DecisionType
from kalshi_weather.domain.models import StrategyDecisionExplanation
from kalshi_weather.storage.state_store import SQLiteStateStore


# Match the volume-grinder gates so we can classify each decision.
# 2026-05-18 PM: aligned with the operational values in
# engines/shadow.py and analytics/volume_selection.py after the bug audit
# found three-way drift. Same logical concept, three different numbers.
# Keep these in sync — see notes/volume_grinder_thresholds.md.
_FAVORITE_MIN_P = Decimal("0.70")
_FAVORITE_MAX_P = Decimal("0.97")
_MAX_SPREAD_F = Decimal("10.0")          # was 8.0 — drifted from shadow=10
_MIN_EXEC_EV = Decimal("0.005")
_MIN_TRADABILITY = Decimal("0.55")
_MAX_MARKET_DISAGREEMENT = Decimal("0.30")  # surface as REVIEW above this


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _float(value: Any) -> float | None:
    d = _decimal(value)
    return float(d) if d is not None else None


def _classify_recommendation(
    explanation: StrategyDecisionExplanation,
    *,
    p_model: Decimal | None,
    p_market: Decimal | None,
    exec_ev: Decimal | None,
    provider_spread_f: Decimal | None,
    tradability: Decimal | None,
) -> tuple[str, list[str]]:
    """Return (recommendation_kind, rejection_reasons).

    recommendation_kind values:
      - "BET"  : All gates pass + decision_engine says TAKER_ALLOWED. Bot auto-fills.
      - "REVIEW" : All gates pass BUT model and market disagree by >30pp.
                   Bot does NOT auto-fill. Surfaced in report for human review.
      - "WATCH_RECOMMEND": Has positive raw edge but failed a gate. Manual eval.
      - "SKIP" : No actionable edge.
    """
    reasons: list[str] = []
    final = explanation.final_decision

    # Gate evaluation (matching VolumeSelectionThresholds + shadow.py)
    if p_model is not None and p_model < _FAVORITE_MIN_P:
        reasons.append("below_favorite_floor")
    if p_model is not None and p_model > _FAVORITE_MAX_P:
        reasons.append("near_certain_capped")
    if exec_ev is not None and exec_ev < _MIN_EXEC_EV:
        reasons.append("ev_below_floor")
    if provider_spread_f is not None and provider_spread_f > _MAX_SPREAD_F:
        reasons.append("model_consensus_too_weak")
    if tradability is not None and tradability < _MIN_TRADABILITY:
        reasons.append("tradability_below_floor")

    market_disagreement = None
    if p_model is not None and p_market is not None:
        market_disagreement = abs(p_model - p_market)
        if market_disagreement > _MAX_MARKET_DISAGREEMENT:
            reasons.append("high_market_disagreement")

    if final != DecisionType.TAKER_ALLOWED:
        reasons.append(f"decision_{final.value.lower()}")

    # Classification flow:
    # 1. If decision_engine said TAKER_ALLOWED AND no rejection reasons → BET
    # 2. If decision_engine said TAKER_ALLOWED AND only market_disagreement
    #    is the issue → REVIEW (manual judgement call, data still saved)
    # 3. Otherwise if there's still positive edge → WATCH_RECOMMEND
    # 4. Otherwise → SKIP
    gate_reasons_excluding_decision = [r for r in reasons if not r.startswith("decision_")]
    if final == DecisionType.TAKER_ALLOWED and not gate_reasons_excluding_decision:
        return "BET", []
    if final == DecisionType.TAKER_ALLOWED and gate_reasons_excluding_decision == ["high_market_disagreement"]:
        # Only the market-disagreement gate triggered — REVIEW.
        return "REVIEW", reasons

    edge_summary = explanation.edge_summary or {}
    raw_edge = _decimal(edge_summary.get("selected_raw_edge"))
    has_raw_edge = raw_edge is not None and raw_edge > Decimal("0.02")
    if final == DecisionType.TAKER_ALLOWED:
        return "WATCH_RECOMMEND", reasons
    if final == DecisionType.WATCH and has_raw_edge:
        return "WATCH_RECOMMEND", reasons
    return "SKIP", reasons


def _window_status_for(
    as_of_utc: datetime,
    window_start_local: datetime | None,
    window_end_local: datetime | None,
) -> tuple[str, int | None, int | None]:
    """Return (window_status, minutes_to_close, local_hour).

    window_status one of:
        "pre_window"   — settlement window hasn't started yet
        "in_window"    — currently inside the heating/cooling window
        "near_close"   — < 60 minutes until settlement window close
        "past_close"   — window has already closed, awaiting CLI report
    """
    if window_start_local is None or window_end_local is None:
        return "unknown", None, None
    tz = window_start_local.tzinfo
    as_of_local = as_of_utc.astimezone(tz) if tz else as_of_utc
    local_hour = as_of_local.hour
    minutes_to_close = int((window_end_local - as_of_local).total_seconds() / 60)
    if as_of_local < window_start_local:
        return "pre_window", minutes_to_close, local_hour
    if as_of_local > window_end_local:
        return "past_close", minutes_to_close, local_hour
    if minutes_to_close <= 60:
        return "near_close", minutes_to_close, local_hour
    return "in_window", minutes_to_close, local_hour


def record_recommendation(
    store: SQLiteStateStore,
    explanation: StrategyDecisionExplanation,
    settlement_rule,
) -> str | None:
    """Persist a comprehensive signal row for this decision.

    Captures BOTH the recommendation classification (BET/REVIEW/WATCH/SKIP)
    AND timing/orderbook/temperature context so we can later analyze:
      - which signals fired when (hour-of-day, minutes-to-close)
      - how the orderbook looked at signal time
      - which signals were filled vs blocked vs missed

    Returns the recommendation_id, or None if we chose not to record (e.g.,
    pure hard-halt with no actionable info).
    """
    final = explanation.final_decision
    if final in (
        DecisionType.HALT_CITY,
        DecisionType.HALT_GLOBAL,
        DecisionType.EXIT,
        DecisionType.REDUCE,
    ):
        return None

    edge_summary = explanation.edge_summary or {}
    forecast_summary = explanation.forecast_summary or {}
    micro_summary = explanation.microstructure_summary or {}
    path_state = explanation.path_state or {}
    current_state = explanation.current_state or {}

    p_model = _decimal(edge_summary.get("selected_p_model"))
    market_price = _decimal(edge_summary.get("selected_p_market_exec"))
    exec_ev = _decimal(edge_summary.get("selected_executable_ev"))
    raw_edge = _decimal(edge_summary.get("selected_raw_edge"))
    provider_spread_f = _decimal(forecast_summary.get("provider_spread_f"))
    tradability = _decimal(micro_summary.get("selected_taker_tradability_score"))
    side = edge_summary.get("selected_side")

    kind, reasons = _classify_recommendation(
        explanation,
        p_model=p_model,
        p_market=market_price,
        exec_ev=exec_ev,
        provider_spread_f=provider_spread_f,
        tradability=tradability,
    )

    # ── Timing context ───────────────────────────────────────────────────────
    as_of_utc = explanation.as_of_time
    window_status, minutes_to_close, local_hour = _window_status_for(
        as_of_utc,
        settlement_rule.local_standard_window_start,
        settlement_rule.local_standard_window_end,
    )
    settlement_close_iso = (
        settlement_rule.local_standard_window_end.astimezone(timezone.utc).isoformat()
        if settlement_rule.local_standard_window_end is not None
        else None
    )

    # ── Temperature context ─────────────────────────────────────────────────
    current_temp_f = _float(_decimal(current_state.get("current_temp_est_f")))
    high_so_far_f = _float(_decimal(path_state.get("current_high_so_far_f")))
    threshold_gap_f = _float(_decimal(path_state.get("threshold_gap_f")))

    # ── Orderbook context ───────────────────────────────────────────────────
    yes_bid = _float(_decimal(micro_summary.get("best_yes_bid")))
    yes_ask = _float(_decimal(micro_summary.get("best_yes_ask")))
    orderbook_spread = None
    if yes_bid is not None and yes_ask is not None:
        orderbook_spread = yes_ask - yes_bid

    record = {
        "recommendation_id": uuid4().hex,
        "decision_id": explanation.decision_id,
        "city_id": explanation.city_id,
        "market_ticker": explanation.market_ticker,
        "settlement_variable": settlement_rule.settlement_variable,
        "operator": settlement_rule.operator,
        "threshold_f": float(settlement_rule.threshold_f) if settlement_rule.threshold_f is not None else None,
        "as_of_time": as_of_utc.isoformat(),
        "recommendation_kind": kind,
        "recommended_side": side,
        "model_p_yes": _float(p_model),
        "market_price": _float(market_price),
        "raw_edge": _float(raw_edge),
        "executable_ev": _float(exec_ev),
        "provider_spread_f": _float(provider_spread_f),
        "rejection_reasons": reasons,
        # Timing context
        "local_time_of_day_hour": local_hour,
        "minutes_to_settlement_close": minutes_to_close,
        "window_status": window_status,
        "settlement_close_time_utc": settlement_close_iso,
        # Temperature context
        "current_temp_f": current_temp_f,
        "high_so_far_f": high_so_far_f,
        "threshold_gap_f_signed": threshold_gap_f,
        # Orderbook context
        "orderbook_yes_bid": yes_bid,
        "orderbook_yes_ask": yes_ask,
        "orderbook_spread": orderbook_spread,
        "tradability_score": _float(tradability),
        # Default fill state — updated later if the bot actually fills.
        "actually_filled": False,
        "fill_blocker_reason": None,
        # Full payload for any field we didn't materialize as a column.
        "payload": {
            "final_decision": final.value,
            "side": side,
            "p_model": _float(p_model),
            "market_price": _float(market_price),
            "raw_edge": _float(raw_edge),
            "executable_ev": _float(exec_ev),
            "provider_spread_f": _float(provider_spread_f),
            "tradability": _float(tradability),
            "explanation_codes": list(explanation.explanation_codes or ()),
            "current_temp_f": current_temp_f,
            "high_so_far_f": high_so_far_f,
            "threshold_gap_f": threshold_gap_f,
            "shock_risk_score": _float(_decimal(current_state.get("shock_risk_score"))),
            "regime": (explanation.regime_summary or {}).get("active_regime") if explanation.regime_summary else None,
        },
    }
    store.save_market_recommendation(record)
    return record["recommendation_id"]


def _market_local_date(market_ticker: str) -> str | None:
    """Decode YYYY-MM-DD from a Kalshi market ticker like KXHIGHNY-26MAY16-T76."""
    import re
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-T", market_ticker)
    if not m:
        return None
    yy, mon_str, dd = m.groups()
    month_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                 "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    month = month_map.get(mon_str.upper())
    if not month:
        return None
    return f"20{yy}-{month:02d}-{int(dd):02d}"


def resolve_pending_recommendations(store: SQLiteStateStore) -> dict[str, int]:
    """Walk unresolved recommendations, compute counterfactual P&L using
    settled daily highs/lows from market_settlements.

    Returns counts: {resolved, still_pending, missing_settlement}.
    """
    pending = store.list_recommendations(unresolved_only=True)
    resolved = 0
    missing = 0
    still_pending = 0

    for rec in pending:
        local_date = _market_local_date(rec["market_ticker"])
        if not local_date:
            still_pending += 1
            continue
        settlement = store.get_market_settlement(rec["city_id"], local_date)
        if not settlement:
            missing += 1
            continue

        # Pick the right realized value based on settlement_variable.
        if rec["settlement_variable"] == "daily_high_temperature_f":
            realized = settlement.get("daily_high_f")
        elif rec["settlement_variable"] == "daily_low_temperature_f":
            realized = settlement.get("daily_low_f")
        else:
            still_pending += 1
            continue

        if realized is None or rec["threshold_f"] is None:
            still_pending += 1
            continue

        threshold = float(rec["threshold_f"])
        operator = rec["operator"]

        # Determine YES outcome based on parsed operator.
        if operator in (">", ">="):
            yes_won = realized > threshold or (operator == ">=" and realized == threshold)
        else:
            yes_won = realized < threshold or (operator == "<=" and realized == threshold)

        # Counterfactual P&L assumes we would have bought 1 contract at the
        # modeled market_price on the recommended_side. Payoff = $1 if right,
        # $0 if wrong, minus the entry price + 7%*p*(1-p) Kalshi fee.
        side = rec.get("recommended_side")
        price = rec.get("market_price")
        if side is None or price is None:
            still_pending += 1
            continue
        won = (yes_won and side == "yes") or (not yes_won and side == "no")
        payoff = 1.0 if won else 0.0
        fee = 0.07 * price * (1.0 - price)
        pnl = payoff - price - fee

        store.update_recommendation_outcome(
            recommendation_id=rec["recommendation_id"],
            counterfactual_won=won,
            counterfactual_pnl_usd=pnl,
        )
        resolved += 1

    return {
        "resolved": resolved,
        "still_pending": still_pending,
        "missing_settlement": missing,
    }
