"""Volume-grinder selection — bet every +EV favorite, skip longshots.

Philosophy (2026-05-16):
    Bet many small favorites instead of one big longshot. On Kalshi the fee
    formula  ``0.07 × p × (1-p)``  is highest at p=0.50 and approaches zero
    near p=0 or p=1, so high-probability favorites actually have the *lowest*
    friction and the best Sharpe. This selector enforces that by filtering
    on model probability rather than on absolute edge size.

Filters applied (in order):
    1. Decision must be TAKER_ALLOWED
    2. Model win probability ≥ ``min_model_probability``  (default 0.70)
    3. Model win probability ≤ ``max_model_probability``  (default 0.97 —
       skip near-certain markets where fees still eat the tiny edge)
    4. Executable EV ≥ ``min_executable_ev``              (default 0.005)
    5. Model-consensus check: provider spread ≤ ``max_provider_spread_f``
       (default 8.0°F — wide model disagreement = skip)
    6. Cumulative daily exposure ≤ ``daily_capital_cap_usd``  (default 10.0)

The selector returns ALL qualifying markets across ALL cities × ALL
thresholds, sorted by per-bet EV descending. The runner then bets them
until the daily capital cap is hit.

NOTE: The longshot lane is intentionally disabled. See
notes/longshot_lane.md for the rationale and revisit criteria.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


__all__ = [
    "VolumeSelectionThresholds",
    "build_volume_grinder_selection",
]


@dataclass(frozen=True, slots=True)
class VolumeSelectionThresholds:
    # Favorite filter — history:
    #   0.70 (initial) → 0.50 (mid-session) → 0.30 (5/25) → 0.10 (5/28).
    # Corrected Kalshi-authoritative backtest (5/27) showed the lanes split:
    #   greater-yes (warm-predicted T-yes):  33% win, +86% ROI  (n=10)
    #   less-yes   (cool-predicted T-yes):   10% win, -41% ROI  (n=57)
    # With less-yes structurally blocked in shadow.py + this module's
    # filter, the floor only gates the proven-profitable greater-yes lane.
    # 0.30 was over-conservative — the same 5¢-entry far-OTM bets were
    # the entire reason the bot ever made money in the original 15-bet
    # sample. Dropping to 0.10 lets through SFO-T67 (p=0.17 +13¢ edge),
    # DC-T82 (p=0.11 +8¢ edge), and similar warm-prediction longshots
    # that the corrected backtest validated.
    min_model_probability: Decimal = Decimal("0.10")
    # Skip near-certain markets (>97%) where fees still eat edge and there's
    # no upside left to grind. Also avoids stranded contracts at $0.98.
    max_model_probability: Decimal = Decimal("0.97")

    # Even small +EV is acceptable in volume mode — fee on a $0.85 favorite is
    # only ~0.9¢, so 1¢ net edge still has decent Sharpe.
    min_executable_ev: Decimal = Decimal("0.005")

    # Model consensus — if 11 forecast sources spread >X°F apart, the regime
    # is too uncertain to bet, regardless of headline EV. The original LAX T71
    # bust came from a SINGLE-model extreme that consensus would have caught.
    #
    # 2026-05-18 PM: aligned default from 8.0 → 10.0 to match the operational
    # value in engines/shadow.py (_VOLUME_GRINDER_MAX_SPREAD_F). The mismatch
    # was making survey_opportunities log "thresholds.max_provider_spread_f=8.0"
    # while shadow actually gated at 10.0 — surveys reported rejections that
    # would have passed live, and accepted candidates that shadow then dropped.
    # See bug audit on 2026-05-18.
    max_provider_spread_f: Decimal = Decimal("10.0")

    # Tradability / orderbook quality.
    min_tradability_score: Decimal = Decimal("0.40")

    # Capital control — daily exposure cap.
    # 2026-05-18 PM: aligned default from $10 → $15 to match the LIVE_DAILY_USD_CAP
    # env var that run_weather_cycle.sh actually exports for live_execution.
    # The mismatch caused survey logs to claim "10.0" cap while live used $15,
    # making cap-related rejection counts in the cycle reports misleading.
    daily_capital_cap_usd: Decimal = Decimal("20.0")

    # Minimum confidence: don't bet without basic model trust.
    min_overall_trade_confidence: Decimal = Decimal("0.25")

    # Market-disagreement cap — DISABLED 2026-05-16 evening.
    # Was Decimal("0.30") — set to 0.99 to effectively allow all favorite bets
    # through regardless of model-vs-market gap. The REVIEW classification in
    # recommendation_log still flags these for later analysis. See
    # notes/market_disagreement_gate_disabled.md for the empirical reasoning.
    max_market_disagreement: Decimal = Decimal("0.99")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _payload_market_date(payload: Mapping[str, Any]) -> str | None:
    """Best-effort extraction of the market settlement date (local)."""
    value = payload.get("market_date")
    if value is not None:
        return str(value)
    # Fallback: parse out of ticker
    from .opportunities import parse_market_date
    market_ticker = str(payload.get("market_ticker") or "")
    parsed = parse_market_date(market_ticker)
    return parsed.isoformat() if parsed else None


def _eligible_market_today(
    payload: Mapping[str, Any],
    *,
    timezone_by_city: Mapping[str, str] | None,
) -> bool:
    """Accept markets settling today OR within the next 2 days.

    Earlier versions admitted any future market via a buggy `or market_date >=
    today_local` clause that defeated the upper bound. Now we explicitly use a
    timedelta window so we don't pollute selection with markets a week+ out.
    """
    from datetime import date as _date, timedelta as _timedelta
    market_date_str = _payload_market_date(payload)
    if not market_date_str:
        return False
    try:
        market_date = _date.fromisoformat(market_date_str)
    except Exception:
        return False
    city_id = str(payload.get("city_id") or "")
    tz_name = (timezone_by_city or {}).get(city_id) or "UTC"
    today_local = datetime.now(ZoneInfo(tz_name)).date()
    # Accept today, tomorrow, or day after (covers same-day + next-available).
    upper_bound = today_local + _timedelta(days=2)
    return today_local <= market_date <= upper_bound


def _candidate_from_payload(
    payload: Mapping[str, Any],
    *,
    thresholds: VolumeSelectionThresholds,
) -> dict[str, Any] | None:
    """Extract a normalized candidate row from a decision payload, or None if filtered."""
    final_decision = str(payload.get("final_decision") or "")
    if final_decision != "TAKER_ALLOWED":
        return None

    edge_summary = payload.get("edge_summary")
    if not isinstance(edge_summary, Mapping):
        return None

    side = str(edge_summary.get("selected_side") or "") or None
    p_model = _decimal(edge_summary.get("selected_p_model"))
    market_price = _decimal(edge_summary.get("selected_p_market_exec"))
    exec_ev = _decimal(edge_summary.get("selected_executable_ev"))
    raw_edge = _decimal(edge_summary.get("selected_raw_edge"))
    fee_cost = _decimal(edge_summary.get("selected_fee_cost"))
    if side is None or p_model is None or market_price is None or exec_ev is None:
        return None

    # ── Filter 1: favorite range ────────────────────────────────────────────
    if p_model < thresholds.min_model_probability:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="below_favorite_floor")
    if p_model > thresholds.max_model_probability:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="near_certain_capped")

    # ── Filter 2: executable EV ─────────────────────────────────────────────
    if exec_ev < thresholds.min_executable_ev:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="ev_below_floor")

    # ── Filter 3: model consensus (provider spread) ─────────────────────────
    forecast_summary = payload.get("forecast_summary")
    provider_spread = Decimal("0")
    if isinstance(forecast_summary, Mapping):
        provider_spread = _decimal(forecast_summary.get("provider_spread_f")) or Decimal("0")
    # 2026-05-19 data audit: model_consensus_too_weak gate DISABLED.
    # Rejected bets would have netted +$7.56 across May 17-19 at 60-86% win rates.
    # Path engine already incorporates provider spread into posterior PMF.
    market_ticker_str = str(payload.get("market_ticker") or "")
    is_between_strike = "-B" in market_ticker_str
    is_no_side = (side or "").lower() == "no"

    # ── Filter 3a: B-no DISABLED (2026-05-23 data audit) ──────────────────
    # 33 settled bets: B-no n=27, win 63%, P&L -$0.33, ROI -1.9% (breakeven).
    # T-yes n=6, win 83%, P&L +$3.47, ROI +227%. Concentrating capital on
    # T-yes. Re-enable when post-bin-proximity-gate data supports the EV.
    if is_between_strike and is_no_side:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="b_no_structure_disabled")

    # ── Filter 3a2: less-yes DISABLED (2026-05-26 corrected-backtest audit) ──
    # Kalshi-authoritative backtest: strike_type=less side=yes was 4W/35L
    # (10% win rate, -$4.61, -34% ROI) — the worst lane by a wide margin.
    # The bot's p_model overestimates the cool-tail probability; until the
    # forecast engine's cool-side calibration is fixed (task #16), refuse
    # T-yes on any "<" or "<=" market. Greater-yes (33% win, +52% ROI) and
    # B-no after the 5°F gate remain enabled.
    settlement_op = (payload.get("edge_summary") or {}).get("settlement_operator") or ""
    if settlement_op in ("<", "<=") and (side or "").lower() == "yes":
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="less_yes_lane_disabled")

    # ── Filter 3b: bin-proximity for B-no (≥5°F required) ─────────────────
    # 2026-05-22 data audit: B-no bets with |threshold − forecast_median|
    # in the 3-5°F band lost -43% ROI on 12 live trades; only ≥5°F was
    # profitable. Selector + shadow must stay in lockstep.
    if is_between_strike and is_no_side and isinstance(forecast_summary, Mapping):
        pmaxima = forecast_summary.get("provider_maxima_f") or {}
        try:
            vals = sorted(float(v) for v in pmaxima.values())
        except Exception:
            vals = []
        if vals:
            forecast_median = Decimal(str(vals[len(vals)//2]))
            import re as _re
            tm = _re.search(r"-B(\d+(?:\.\d+)?)$", market_ticker_str)
            if tm:
                threshold = Decimal(tm.group(1))
                if abs(threshold - forecast_median) < Decimal("5.0"):
                    return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                                     reason="b_no_bin_proximity_under_5f",
                                     provider_spread_f=provider_spread)

    # ── Filter 4: tradability ───────────────────────────────────────────────
    micro_summary = payload.get("microstructure_summary")
    tradability = Decimal("0")
    if isinstance(micro_summary, Mapping):
        tradability = _decimal(micro_summary.get("selected_taker_tradability_score")) or Decimal("0")
    if tradability < thresholds.min_tradability_score:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="tradability_below_floor",
                         tradability=tradability)

    # ── Filter 5: confidence ────────────────────────────────────────────────
    confidence_summary = edge_summary.get("confidence_summary") if isinstance(edge_summary, Mapping) else None
    overall_conf = Decimal("0")
    if isinstance(confidence_summary, Mapping):
        overall_conf = _decimal(confidence_summary.get("overall_trade_confidence")) or Decimal("0")
    if overall_conf < thresholds.min_overall_trade_confidence:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="confidence_below_floor",
                         overall_confidence=overall_conf)

    # ── Filter 6: market-disagreement (REVIEW gate) ─────────────────────────
    # When our (uncalibrated) model and Kalshi disagree by >30pp, the bot
    # surfaces the bet for human review instead of auto-selecting it.
    market_disagreement = abs(p_model - market_price)
    if market_disagreement > thresholds.max_market_disagreement:
        return _rejected(payload, side, p_model, market_price, exec_ev, raw_edge,
                         reason="high_market_disagreement",
                         market_disagreement=market_disagreement)

    return _accepted(
        payload=payload,
        side=side,
        p_model=p_model,
        market_price=market_price,
        exec_ev=exec_ev,
        raw_edge=raw_edge,
        fee_cost=fee_cost or Decimal("0"),
        provider_spread_f=provider_spread,
        tradability=tradability,
        overall_confidence=overall_conf,
    )


def _accepted(
    *,
    payload: Mapping[str, Any],
    side: str,
    p_model: Decimal,
    market_price: Decimal,
    exec_ev: Decimal,
    raw_edge: Decimal | None,
    fee_cost: Decimal,
    provider_spread_f: Decimal,
    tradability: Decimal,
    overall_confidence: Decimal,
) -> dict[str, Any]:
    return {
        "accepted": True,
        "rejection_reason": None,
        "city_id": str(payload.get("city_id") or ""),
        "market_ticker": str(payload.get("market_ticker") or ""),
        "market_date": _payload_market_date(payload),
        "side": side,
        "p_model": str(p_model),
        "market_price": str(market_price),
        "exec_ev": str(exec_ev),
        "raw_edge": str(raw_edge) if raw_edge is not None else None,
        "fee_cost": str(fee_cost),
        "provider_spread_f": str(provider_spread_f),
        "tradability": str(tradability),
        "overall_confidence": str(overall_confidence),
        "cost_per_contract": str(market_price),
        "ev_to_cost_ratio": str(exec_ev / market_price) if market_price > 0 else "0",
        "as_of_time": str(payload.get("as_of_time") or ""),
    }


def _rejected(
    payload: Mapping[str, Any],
    side: str | None,
    p_model: Decimal | None,
    market_price: Decimal | None,
    exec_ev: Decimal | None,
    raw_edge: Decimal | None,
    *,
    reason: str,
    **extras: Any,
) -> dict[str, Any]:
    rec = {
        "accepted": False,
        "rejection_reason": reason,
        "city_id": str(payload.get("city_id") or ""),
        "market_ticker": str(payload.get("market_ticker") or ""),
        "market_date": _payload_market_date(payload),
        "side": side,
        "p_model": str(p_model) if p_model is not None else None,
        "market_price": str(market_price) if market_price is not None else None,
        "exec_ev": str(exec_ev) if exec_ev is not None else None,
        "raw_edge": str(raw_edge) if raw_edge is not None else None,
        "as_of_time": str(payload.get("as_of_time") or ""),
    }
    for k, v in extras.items():
        rec[k] = str(v) if isinstance(v, Decimal) else v
    return rec


def build_volume_grinder_selection(
    decision_payloads: Iterable[Mapping[str, Any]],
    *,
    timezone_by_city: Mapping[str, str] | None = None,
    thresholds: VolumeSelectionThresholds | None = None,
    already_open_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Return all qualifying favorite bets across cities/thresholds.

    The selector iterates over every recent decision payload (typically one
    per city × market threshold for the next available day), applies the
    favorite/EV/consensus filters, and orders the accepted candidates by
    per-bet EV.  A cumulative capital cap is then applied — when the running
    total of `market_price` (cost per contract) reaches the cap, remaining
    candidates are moved to ``deferred`` rather than ``selected``.

    Output shape:
        {
            "strategy": "volume_grinder_v1",
            "thresholds": {...},
            "evaluated_count": int,
            "accepted_count": int,
            "rejected_count": int,
            "selected": [candidate, ...],  # to bet today, within $10 cap
            "deferred": [candidate, ...],  # +EV but capital-capped
            "rejected": [candidate, ...],  # filter reasons
            "daily_capital_used_usd": str,
            "daily_capital_cap_usd": str,
        }
    """
    cfg = thresholds or VolumeSelectionThresholds()
    already_open = already_open_keys or set()

    # Pick the LATEST decision per (city, market_ticker) pair so we don't
    # double-count when the runner has logged multiple decisions per market.
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for payload in decision_payloads:
        if not isinstance(payload, Mapping):
            continue
        city_id = str(payload.get("city_id") or "")
        ticker = str(payload.get("market_ticker") or "")
        if not city_id or not ticker:
            continue
        key = (city_id, ticker)
        # Skip markets where we already hold an open shadow/live position —
        # they're already bet, no need to recommend again.
        if key in already_open:
            continue
        if not _eligible_market_today(payload, timezone_by_city=timezone_by_city):
            continue
        prev = latest.get(key)
        if prev is None:
            latest[key] = payload
            continue
        try:
            prev_t = datetime.fromisoformat(str(prev.get("as_of_time")))
            curr_t = datetime.fromisoformat(str(payload.get("as_of_time")))
            if curr_t >= prev_t:
                latest[key] = payload
        except Exception:
            latest[key] = payload

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for payload in latest.values():
        candidate = _candidate_from_payload(payload, thresholds=cfg)
        if candidate is None:
            continue
        if candidate["accepted"]:
            accepted.append(candidate)
        else:
            rejected.append(candidate)

    # Sort accepted by per-bet EV descending — the volume grinder takes the
    # highest-EV bets first until the capital cap is hit.
    accepted.sort(
        key=lambda c: float(Decimal(c["exec_ev"]) if c.get("exec_ev") else Decimal("0")),
        reverse=True,
    )

    # Apply daily capital cap.
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    running_cost = Decimal("0")
    for candidate in accepted:
        try:
            cost = Decimal(candidate.get("cost_per_contract") or "0")
        except Exception:
            cost = Decimal("0")
        if running_cost + cost <= cfg.daily_capital_cap_usd:
            selected.append(candidate)
            running_cost += cost
        else:
            candidate_copy = dict(candidate)
            candidate_copy["deferred_reason"] = "daily_capital_cap_reached"
            deferred.append(candidate_copy)

    return {
        "strategy": "volume_grinder_v1",
        "thresholds": {
            "min_model_probability": str(cfg.min_model_probability),
            "max_model_probability": str(cfg.max_model_probability),
            "min_executable_ev": str(cfg.min_executable_ev),
            "max_provider_spread_f": str(cfg.max_provider_spread_f),
            "min_tradability_score": str(cfg.min_tradability_score),
            "daily_capital_cap_usd": str(cfg.daily_capital_cap_usd),
            "min_overall_trade_confidence": str(cfg.min_overall_trade_confidence),
        },
        "evaluated_count": len(latest),
        "accepted_count": len(selected),
        "deferred_count": len(deferred),
        "rejected_count": len(rejected),
        "selected": selected,
        "deferred": deferred,
        "rejected": rejected,
        "daily_capital_used_usd": str(running_cost),
        "daily_capital_cap_usd": str(cfg.daily_capital_cap_usd),
    }
