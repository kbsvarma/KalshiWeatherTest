from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from kalshi_weather.domain.enums import DecisionType
from kalshi_weather.domain.models import EdgeEstimate, ShadowFill, ShadowPosition, StrategyDecisionExplanation
from kalshi_weather.engines.fill_simulation import build_fill_simulations
from kalshi_weather.storage.state_store import SQLiteStateStore


@dataclass(frozen=True, slots=True)
class ShadowApplicationResult:
    fill: ShadowFill | None
    position: ShadowPosition | None


# Volume-grinder gates — MUST match VolumeSelectionThresholds.
# These run at the fill layer so the shadow_fills table never contains
# trades that the volume-grinder selector would have rejected. This keeps the
# decision engine + selector consistent and prevents drift between what
# *would* be bet live vs what's recorded in shadow.
# 2026-05-19 PM shadow-mode wide-net: lowered 0.40 → 0.20 to match the new
# selector floor while we calibrate during shadow. MUST stay in lockstep
# with analytics.volume_selection.VolumeSelectionThresholds.min_model_probability.
_VOLUME_GRINDER_MIN_P = Decimal("0.10")  # 2026-05-28: 0.30 → 0.10, lockstep with volume_selection.py — corrected backtest showed greater-yes longshots are profitable (+86% ROI); less-yes is already structurally blocked by Gate 2c so this only opens the proven-good lane
_VOLUME_GRINDER_MAX_P = Decimal("0.97")
# Raised 2026-05-17 from 8.0 → 10.0 after data showed it was leaving real
# edge on the table. Audit (n=148 TAKER_ALLOWED candidates) found 69
# strong-favorite candidates with EV up to $0.13/contract being blocked
# because morning forecast spreads run 8-12°F (vs evening 5-7°F). The
# 8.0 cap was tuned from last night's 7 successful fills which all happened
# to be in the 5.7-7.1°F range — but those weren't a ceiling, just where
# the bot fired late at night. 10.0 still excludes the genuinely noisy
# 15-25°F spread markets while letting morning-window favorites through.
_VOLUME_GRINDER_MAX_SPREAD_F = Decimal("10.0")    # model-consensus check
_VOLUME_GRINDER_MIN_EXEC_EV = Decimal("0.005")    # 0.5¢
_VOLUME_GRINDER_MIN_TRADABILITY = Decimal("0.55")

# Maximum distinct markets per city per day. Was 1 ("one position per city")
# which was correct for -T threshold markets but blocked all subsequent -B
# range market bets after the first one fired (e.g., NO on B85.5 prevented
# NO on B87.5 even though they're INDEPENDENT non-overlapping bins).
# Allowing 3 distinct markets per city × ~$0.60 avg price = ~$1.80 per-city
# risk concentration, well within the $15/day cap. Same-market dedup is still
# enforced via the existing dedup check at the live-execution layer.
# 2026-05-19 PM shadow-mode: raised 3 → 99 to log every candidate per city.
# Restore to 3 (or your chosen value) when re-enabling live orders.
_MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY = 5
# Market-disagreement gate: DISABLED 2026-05-16 evening.
#
# Originally added defensively after the LAX T71 longshot bust to refuse
# auto-fill when model and market disagreed by >30 percentage points. Today's
# 4 high-disagreement bets (PHX/OKC/DAL/SAT) are tracking toward MODEL wins,
# suggesting the gate is over-blocking real edge for high-conviction favorites.
#
# We're now letting these through but the recommendation_log still classifies
# them as REVIEW so we can compare gated-vs-ungated counterfactual P&L after
# settlements arrive. Set this back to e.g. Decimal("0.30") to re-enable.
_VOLUME_GRINDER_MAX_MARKET_DISAGREEMENT = Decimal("0.99")


def apply_shadow_decision(
    store: SQLiteStateStore,
    city_id: str,
    explanation: StrategyDecisionExplanation,
    edge: EdgeEstimate,
) -> ShadowApplicationResult:
    position = store.get_shadow_position(city_id)
    # 2026-05-17 fix: the per-city cap should be (city, SETTLEMENT_DATE) —
    # not pooled across all dates. NOLA's MAY 17 positions were blocking
    # NOLA-MAY18 candidates even though they settle on different weather
    # days with mostly independent outcomes.
    import re as _re
    _date_re = _re.compile(r"-(\d{2}[A-Z]{3}\d{2})-")
    def _settle_date(ticker: str) -> str:
        m = _date_re.search(ticker)
        return m.group(1) if m else ""
    candidate_settle_date = _settle_date(explanation.market_ticker)
    all_open_in_city = [
        p for p in store.list_shadow_positions()
        if p.city_id == city_id and p.lifecycle_status == "OPEN"
    ]
    # Distinct open markets for the SAME settlement date as the candidate.
    # Total city positions (any date) are tracked separately for logging.
    same_day_open = [
        p for p in all_open_in_city
        if _settle_date(p.market_ticker) == candidate_settle_date
    ]
    distinct_open_markets = {p.market_ticker for p in same_day_open}

    if explanation.final_decision != DecisionType.TAKER_ALLOWED:
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 1: favorite filter — skip longshots and near-certain markets.
    if edge.p_model < _VOLUME_GRINDER_MIN_P or edge.p_model > _VOLUME_GRINDER_MAX_P:
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 2: model consensus check — DISABLED ─────────────────────────
    # 2026-05-19 PM data audit: this gate would have rejected bets netting
    # +$7.56 across May 17-19 at 60-86% win rates. On all 3 days it was a
    # winner-rejector. Disabled entirely — the path engine already
    # incorporates provider spread into the posterior PMF via the weighted
    # ensemble; gating here was double-counting.
    is_between_strike = "-B" in (explanation.market_ticker or "")
    is_no_side = (edge.side or "").lower() == "no"
    forecast_summary = explanation.forecast_summary or {}
    # (Spread gate intentionally not enforced.)

    # ── Gate 2a: B-no DISABLED (2026-05-23 data audit) ──────────────────
    # B-no settled n=27, ROI -1.9%; T-yes settled n=6, ROI +227%.
    # Must stay in lockstep with analytics.volume_selection.
    if is_between_strike and is_no_side:
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 2c: "less-yes" DISABLED (2026-05-26 corrected-backtest audit) ──
    # Honest backtest (using Kalshi's authoritative `result` field instead of
    # my broken weather-vs-threshold math) showed:
    #   strike_type=less, side=yes:  n=57, W=4, L=35, win%=10%, P&L=-$4.61
    #   strike_type=greater, side=yes: n=10, W=2, L=4, win%=33%, P&L=+$1.56
    # The bot's p_model systematically overestimates P(high < threshold)
    # for far-OTM cool outcomes — bets at 0.05-0.40 model probability are
    # losing at 90% rate. Until the cool-tail mis-calibration is diagnosed
    # (task #16), refuse T-yes on "less" markets entirely.
    # NB: "less" markets are still tradeable from the NO side; this gate
    # only blocks the YES-on-less combo where we systematically lose.
    settlement_op = (explanation.edge_summary or {}).get('settlement_operator', '')
    if settlement_op in ('<', '<=') and (edge.side or '').lower() == 'yes':
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 2b: bin-proximity for B-no ──────────────────────────────────
    # 2026-05-19 PM data audit: B-no bets with |threshold - forecast_median|
    # between 1-3°F lost at 25-33% win rate. ≥3°F gap won at 75-80%. When
    # the bin straddles the forecast median, reality lands in it most
    # often — betting NO is paying for the modal outcome.
    if is_between_strike and is_no_side:
        pmaxima = forecast_summary.get("provider_maxima_f") or {}
        try:
            vals = sorted(float(v) for v in pmaxima.values())
        except Exception:
            vals = []
        if vals:
            forecast_median = Decimal(str(vals[len(vals)//2]))
            import re as _re
            tm = _re.search(r"-B(\d+(?:\.\d+)?)$", explanation.market_ticker or "")
            if tm:
                threshold = Decimal(tm.group(1))
                # 2026-05-22: raised 3°F → 5°F. Live data on 12 B-no trades
                # in the 3-5°F band showed -43% ROI; the profitable zone is
                # ≥5°F. Selector + shadow must stay in lockstep.
                if abs(threshold - forecast_median) < Decimal("5.0"):
                    return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 3: minimum executable EV.
    if edge.executable_ev_per_contract < _VOLUME_GRINDER_MIN_EXEC_EV:
        print(f"[SHADOW-DROP] {explanation.market_ticker} side={edge.side}: "
              f"exec_ev={float(edge.executable_ev_per_contract):.4f} < "
              f"floor {_VOLUME_GRINDER_MIN_EXEC_EV}")
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 4: market liquidity — refuse to record a fill when the
    # orderbook isn't real (e.g. Kalshi shows no bid/ask). The
    # microstructure summary tracks tradability over recent snapshots.
    micro = explanation.microstructure_summary or {}
    trad_raw = micro.get("selected_taker_tradability_score")
    if trad_raw is not None:
        try:
            tradability = Decimal(str(trad_raw))
            if tradability < _VOLUME_GRINDER_MIN_TRADABILITY:
                print(f"[SHADOW-DROP] {explanation.market_ticker} side={edge.side}: "
                      f"tradability={float(tradability):.3f} < "
                      f"floor {_VOLUME_GRINDER_MIN_TRADABILITY}")
                return ShadowApplicationResult(fill=None, position=position)
        except Exception:
            pass

    # ── Gate 5: market disagreement — refuse auto-fill when our model and
    # Kalshi disagree by more than _VOLUME_GRINDER_MAX_MARKET_DISAGREEMENT.
    # Rationale: when |p_model - p_market| > 0.30, one of us is way off.
    # For uncalibrated cities the market is usually closer to truth, so we
    # surface these as REVIEW recommendations instead of auto-filling.
    # The recommendation log STILL captures these for later calibration.
    market_disagreement = abs(edge.p_model - edge.p_market_exec)
    if market_disagreement > _VOLUME_GRINDER_MAX_MARKET_DISAGREEMENT:
        print(f"[SHADOW-DROP] {explanation.market_ticker} side={edge.side}: "
              f"market_disagreement={float(market_disagreement):.3f} > "
              f"cap {_VOLUME_GRINDER_MAX_MARKET_DISAGREEMENT} "
              f"(p_model={float(edge.p_model):.3f}, p_market={float(edge.p_market_exec):.3f})")
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 6: per-city cap — allow multiple DISTINCT market bets per city
    # (max _MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY) so the bot can fire on
    # multiple -B bins / -T thresholds for the same city. Re-betting the
    # SAME market is still prevented (the existing market_ticker dedup
    # below catches it).
    if explanation.market_ticker in distinct_open_markets:
        print(f"[SHADOW-DROP] {explanation.market_ticker} side={edge.side}: "
              f"already-open duplicate")
        return ShadowApplicationResult(fill=None, position=position)
    if len(distinct_open_markets) >= _MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY:
        print(f"[SHADOW-DROP] {explanation.market_ticker} side={edge.side}: "
              f"city_cap_hit ({len(distinct_open_markets)}/{_MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY} "
              f"distinct markets open in {city_id})")
        return ShadowApplicationResult(fill=None, position=position)

    base_fill = build_fill_simulations(explanation, edge)["base"]
    if base_fill.filled_quantity_fp <= Decimal("0"):
        return ShadowApplicationResult(fill=None, position=position)
    confidence_summary = explanation.edge_summary.get("confidence_summary", {})

    fill = ShadowFill(
        shadow_fill_id=uuid4().hex,
        decision_id=explanation.decision_id,
        market_ticker=explanation.market_ticker,
        side=edge.side,
        quantity_fp=base_fill.filled_quantity_fp,
        modeled_fill_price_dollars=base_fill.modeled_fill_price_dollars,
        fill_scenario="base",
        fill_confidence=base_fill.fill_ratio,
        modeled_fee_dollars=base_fill.modeled_fee_dollars,
        modeled_slippage_dollars=base_fill.modeled_slippage_dollars,
        modeled_adverse_selection_dollars=base_fill.modeled_adverse_selection_dollars,
        fill_time=explanation.as_of_time if explanation.as_of_time.tzinfo else datetime.now(timezone.utc),
        reconciliation_status="OPEN",
        predicted_executable_ev_per_contract=edge.executable_ev_per_contract,
        predicted_executable_ev_total=base_fill.executable_ev_total,
        model_confidence=Decimal(str(confidence_summary["model_confidence"]))
        if confidence_summary.get("model_confidence") is not None
        else None,
        execution_confidence=Decimal(str(confidence_summary["execution_confidence"]))
        if confidence_summary.get("execution_confidence") is not None
        else None,
        governance_confidence=Decimal(str(confidence_summary["governance_confidence"]))
        if confidence_summary.get("governance_confidence") is not None
        else None,
        overall_trade_confidence=Decimal(str(confidence_summary["overall_trade_confidence"]))
        if confidence_summary.get("overall_trade_confidence") is not None
        else None,
        confidence_reasons=tuple(confidence_summary.get("confidence_reasons") or ()),
    )
    position = ShadowPosition(
        city_id=city_id,
        market_ticker=explanation.market_ticker,
        side=edge.side,
        open_quantity_fp=base_fill.filled_quantity_fp,
        avg_cost_dollars=base_fill.modeled_fill_price_dollars,
        cumulative_fees_dollars=base_fill.modeled_fee_dollars,
        mark_pnl_dollars=Decimal("0"),
        settled_pnl_dollars=Decimal("0"),
        lifecycle_status="OPEN",
    )
    store.save_shadow_fill(city_id, fill)
    store.save_shadow_position(position)
    return ShadowApplicationResult(fill=fill, position=position)
