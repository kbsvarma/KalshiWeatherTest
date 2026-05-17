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
_VOLUME_GRINDER_MIN_P = Decimal("0.70")
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
_MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY = 3
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
    # Pull ALL open positions in this city (not just the single one returned
    # by get_shadow_position) so we can enforce a "max N distinct markets per
    # city per day" cap. Pre-2026-05-17 behaviour was effectively "max 1".
    all_open_in_city = [
        p for p in store.list_shadow_positions()
        if p.city_id == city_id and p.lifecycle_status == "OPEN"
    ]
    distinct_open_markets = {p.market_ticker for p in all_open_in_city}

    if explanation.final_decision != DecisionType.TAKER_ALLOWED:
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 1: favorite filter — skip longshots and near-certain markets.
    if edge.p_model < _VOLUME_GRINDER_MIN_P or edge.p_model > _VOLUME_GRINDER_MAX_P:
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 2: model consensus check — refuse to bet when the 11-source
    # ensemble disagrees by more than max_provider_spread_f. Wide disagreement
    # means the regime is too uncertain and the headline edge is unreliable.
    forecast_summary = explanation.forecast_summary or {}
    spread_raw = forecast_summary.get("provider_spread_f")
    if spread_raw is not None:
        try:
            spread_f = Decimal(str(spread_raw))
            if spread_f > _VOLUME_GRINDER_MAX_SPREAD_F:
                return ShadowApplicationResult(fill=None, position=position)
        except Exception:
            pass

    # ── Gate 3: minimum executable EV.
    if edge.executable_ev_per_contract < _VOLUME_GRINDER_MIN_EXEC_EV:
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
        return ShadowApplicationResult(fill=None, position=position)

    # ── Gate 6: per-city cap — allow multiple DISTINCT market bets per city
    # (max _MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY) so the bot can fire on
    # multiple -B bins / -T thresholds for the same city. Re-betting the
    # SAME market is still prevented (the existing market_ticker dedup
    # below catches it).
    if explanation.market_ticker in distinct_open_markets:
        # Already have a position on this exact market — skip duplicate.
        return ShadowApplicationResult(fill=None, position=position)
    if len(distinct_open_markets) >= _MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY:
        # City has hit its per-day distinct-markets cap.
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
