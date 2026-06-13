from __future__ import annotations

import re as _re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from collections import Counter
from typing import Any, Iterable, Mapping
from uuid import uuid4

# Settlement-date extraction for cross-market dedup. The Kalshi weather ticker
# format embeds the settle date as YYMMMDD between two dashes, e.g.
# "KXHIGHTHOU-26MAY18-B86.5" → "26MAY18". We use this to ensure the decision
# engine treats positions on different settlement days as independent.
_TICKER_SETTLE_DATE_RE = _re.compile(r"-(\d{2}[A-Z]{3}\d{2})-")


def _ticker_settle_date(ticker: str) -> str:
    """Return the YYMMMDD settle-date token from a Kalshi ticker, or "".

    Mirrors the helper in ``engines.shadow``. Kept inline (rather than
    imported) to avoid widening this module's dependency surface.
    """
    m = _TICKER_SETTLE_DATE_RE.search(ticker or "")
    return m.group(1) if m else ""

from kalshi_weather.analytics.forecast_calibration import (
    extract_provider_bias_adjustments,
    extract_provider_day_max_sigmas,
    extract_provider_reliability_weights,
)
from kalshi_weather.domain.enums import DecisionType, QualificationState, RunMode
from kalshi_weather.domain.models import (
    CityProfile,
    EdgeEstimate,
    ForecastSnapshot,
    MarketSnapshot,
    ObservationSnapshot,
    OrderbookSnapshot,
    RiskDecision,
    ShadowPosition,
    StrategyDecisionExplanation,
    TradeSnapshot,
    TradabilityAssessment,
)
from kalshi_weather.engines.ev import DecisionThresholds, compute_edge_estimate, kelly_contract_size
from kalshi_weather.engines.forecast import build_forecast_distribution
from kalshi_weather.engines.microstructure import assess_taker_side
from kalshi_weather.engines.nowcast import build_current_state_estimate
from kalshi_weather.engines.path import apply_path_adjustment
from kalshi_weather.engines.regime import build_regime_assessment
from kalshi_weather.engines.risk import build_risk_decision
from kalshi_weather.portfolio import build_regime_adjusted_correlation_matrix, build_static_correlation_matrix, portfolio_risk_units
from kalshi_weather.settlement.rule_parser import parse_settlement_rule


@dataclass(frozen=True, slots=True)
class DecisionCycleResult:
    explanation: StrategyDecisionExplanation
    selected_edge: EdgeEstimate | None


OPEN_POSITION_EXIT_EV = Decimal("-0.03")
OPEN_POSITION_REDUCE_EV = Decimal("-0.01")


def _clamp_decimal(value: Decimal, *, low: Decimal = Decimal("0"), high: Decimal = Decimal("1")) -> Decimal:
    return max(low, min(high, value))


def _season_key(as_of_time: datetime) -> str:
    month = as_of_time.month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _build_confidence_summary(
    *,
    selected_edge: EdgeEstimate | None,
    selected_micro,
    selected_tradability: TradabilityAssessment | None,
    qualification_state: QualificationState,
    current_state,
    hard_halt_flag: bool,
) -> dict[str, object]:
    if selected_edge is None or selected_micro is None or selected_tradability is None:
        return {
            "model_confidence": None,
            "execution_confidence": None,
            "governance_confidence": None,
            "overall_trade_confidence": None,
            "confidence_reasons": [],
        }
    model_confidence = _clamp_decimal(
        Decimal("1")
        - selected_edge.uncertainty_haircut
        - selected_edge.regime_haircut
        - selected_edge.portfolio_haircut
    )
    execution_confidence = _clamp_decimal(
        (
            selected_tradability.tradability_score
            + selected_micro.quote_stability_score
            + selected_micro.top_of_book_durability_score
            + _clamp_decimal(Decimal("1") - selected_micro.ghost_liquidity_ratio)
        )
        / Decimal("4")
    )
    qualification_base = {
        QualificationState.SHADOW_ONLY: Decimal("0.45"),
        QualificationState.SHADOW_QUALIFIED: Decimal("0.65"),
        QualificationState.LIVE_PILOT: Decimal("0.85"),
    }.get(qualification_state, Decimal("0.25"))
    governance_penalty = current_state.observation_lag_penalty + current_state.confidence_downgrade
    governance_confidence = _clamp_decimal(
        (Decimal("0") if hard_halt_flag else qualification_base) - governance_penalty
    )
    overall_trade_confidence = _clamp_decimal(
        (model_confidence + execution_confidence + governance_confidence) / Decimal("3")
    )
    reasons: list[str] = []
    if model_confidence >= Decimal("0.70"):
        reasons.append("model_confidence_strong")
    elif model_confidence <= Decimal("0.45"):
        reasons.append("model_confidence_soft")
    if execution_confidence >= Decimal("0.70"):
        reasons.append("execution_confidence_strong")
    elif execution_confidence <= Decimal("0.45"):
        reasons.append("execution_confidence_soft")
    if governance_confidence >= Decimal("0.70"):
        reasons.append("governance_confidence_strong")
    elif governance_confidence <= Decimal("0.45"):
        reasons.append("governance_confidence_soft")
    return {
        "model_confidence": str(model_confidence),
        "execution_confidence": str(execution_confidence),
        "governance_confidence": str(governance_confidence),
        "overall_trade_confidence": str(overall_trade_confidence),
        "confidence_reasons": reasons,
    }


def _market_threshold_from_ticker(market_ticker: str) -> Decimal | None:
    if "-T" not in market_ticker:
        return None
    try:
        return Decimal(market_ticker.rsplit("-T", 1)[1])
    except Exception:
        return None


def _candidate_portfolio_risk_units(
    *,
    city_id: str,
    market_ticker: str,
    open_positions: list[tuple[str, ShadowPosition]] | None,
    open_city_ids: set[str],
    correlation_matrix: Mapping[tuple[str, str], Decimal],
) -> Decimal:
    base_positions = [
        (position_city_id, position)
        for position_city_id, position in (open_positions or [])
        if position is not None and position.lifecycle_status == "OPEN"
    ]
    if city_id in open_city_ids:
        return portfolio_risk_units(base_positions, dict(correlation_matrix))
    simulated_positions = [
        *base_positions,
        (
            city_id,
            ShadowPosition(
                city_id=city_id,
                market_ticker=market_ticker,
                side="yes",
                open_quantity_fp=Decimal("1"),
                avg_cost_dollars=Decimal("0"),
                cumulative_fees_dollars=Decimal("0"),
                mark_pnl_dollars=Decimal("0"),
                settled_pnl_dollars=Decimal("0"),
                lifecycle_status="OPEN",
            ),
        ),
    ]
    return portfolio_risk_units(simulated_positions, dict(correlation_matrix))


def _parse_signal_time(payload: Mapping[str, Any] | None) -> datetime | None:
    if payload is None:
        return None
    value = payload.get("as_of_time")
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _position_signal_map(
    signals: Iterable[Mapping[str, Any]] | None,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for payload in signals or ():
        city_id = str(payload.get("city_id") or "")
        market_ticker = str(payload.get("market_ticker") or "")
        as_of_time = _parse_signal_time(payload)
        if not city_id or not market_ticker or as_of_time is None:
            continue
        key = (city_id, market_ticker)
        current = latest.get(key)
        current_time = _parse_signal_time(current) if current is not None else None
        if current_time is None or as_of_time >= current_time:
            latest[key] = payload
    return latest


def _candidate_block_reason_counts(
    candidate_edges: Iterable[
        tuple[str, EdgeEstimate, tuple[str, ...], object, RiskDecision, TradabilityAssessment]
    ],
    *,
    style: str,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for candidate_style, _edge, reasons, _micro, _risk, _tradability in candidate_edges:
        if candidate_style != style:
            continue
        for reason in reasons:
            counts[str(reason)] += 1
    return counts


def _candidate_summary_payload(
    candidate: tuple[str, EdgeEstimate, tuple[str, ...], object, RiskDecision, TradabilityAssessment] | None,
) -> dict[str, object] | None:
    if candidate is None:
        return None
    style, edge, reasons, micro, risk, tradability = candidate
    return {
        "execution_style": style,
        "side": edge.side,
        "p_model": str(edge.p_model),
        "p_market_exec": str(edge.p_market_exec),
        "raw_edge": str(edge.raw_edge),
        "fee_cost": str(edge.fee_cost),
        "slippage_cost": str(edge.slippage_cost),
        "adverse_selection_penalty": str(edge.adverse_selection_penalty),
        "total_friction": str(edge.total_friction),
        "friction_to_edge_ratio": str(edge.friction_to_edge_ratio),
        "uncertainty_haircut": str(edge.uncertainty_haircut),
        "regime_haircut": str(edge.regime_haircut),
        "portfolio_haircut": str(edge.portfolio_haircut),
        "edge_conf_adj": str(edge.edge_conf_adj),
        "executable_ev": str(edge.executable_ev_per_contract),
        "quantity_fp": str(edge.quantity_fp),
        "tradability_score": str(tradability.tradability_score),
        "allowed_taker_flag": tradability.allowed_taker_flag,
        "allowed_maker_flag": tradability.allowed_maker_flag,
        "block_reasons": list(reasons),
        "risk_state": risk.risk_state.value,
        "hard_halt_flag": risk.hard_halt_flag,
        "veto_reasons": list(risk.veto_reasons),
        "ghost_liquidity_ratio": str(micro.ghost_liquidity_ratio),
        "quote_stability_score": str(micro.quote_stability_score),
        "top_of_book_durability_score": str(micro.top_of_book_durability_score),
        "maker_fill_probability": str(micro.maker_fill_probability),
        "time_to_close_bucket": micro.time_to_close_bucket,
    }


def _threshold_concentration_summary(
    *,
    city_id: str,
    market_ticker: str,
    selected_edge: EdgeEstimate | None,
    path_state,
    regime,
    open_positions: list[tuple[str, ShadowPosition]] | None,
    open_position_signals: Iterable[Mapping[str, Any]] | None,
    correlation_matrix: Mapping[tuple[str, str], Decimal],
) -> dict[str, object]:
    if selected_edge is None:
        return {
            "threshold_concentration_score": "0",
            "threshold_concentration_block": False,
            "threshold_concentration_city": None,
            "threshold_concentration_market": None,
        }
    signal_lookup = _position_signal_map(open_position_signals)
    candidate_threshold = _market_threshold_from_ticker(market_ticker)
    candidate_gap = abs(path_state.threshold_gap_f)
    best_score = Decimal("0")
    best_city: str | None = None
    best_market: str | None = None
    for open_city_id, position in open_positions or []:
        if open_city_id == city_id or position.lifecycle_status != "OPEN":
            continue
        payload = signal_lookup.get((open_city_id, position.market_ticker))
        if payload is None:
            continue
        edge_summary = payload.get("edge_summary")
        path_summary = payload.get("path_state")
        regime_summary = payload.get("regime_summary")
        if not isinstance(edge_summary, Mapping) or not isinstance(path_summary, Mapping) or not isinstance(regime_summary, Mapping):
            continue
        selected_side = str(edge_summary.get("selected_side") or "")
        selected_p_model = edge_summary.get("selected_p_model")
        threshold_gap_value = path_summary.get("threshold_gap_f")
        if not selected_side or selected_p_model is None or threshold_gap_value is None:
            continue
        try:
            open_p_model = Decimal(str(selected_p_model))
            open_gap = abs(Decimal(str(threshold_gap_value)))
        except Exception:
            continue
        open_regime = str(regime_summary.get("active_regime") or "")
        open_threshold = _market_threshold_from_ticker(position.market_ticker)
        probability_similarity = _clamp_decimal(
            Decimal("1") - (abs(selected_edge.p_model - open_p_model) / Decimal("0.25"))
        )
        gap_similarity = _clamp_decimal(
            Decimal("1") - (abs(candidate_gap - open_gap) / Decimal("4"))
        )
        threshold_similarity = Decimal("0.5")
        if candidate_threshold is not None and open_threshold is not None:
            threshold_similarity = _clamp_decimal(
                Decimal("1") - (abs(candidate_threshold - open_threshold) / Decimal("8"))
            )
        regime_similarity = Decimal("1") if regime.active_regime == open_regime else Decimal("0.45")
        side_similarity = Decimal("1") if selected_side == selected_edge.side else Decimal("0.30")
        correlation_weight = correlation_matrix.get((city_id, open_city_id), Decimal("0.10"))
        score = (
            (Decimal("0.35") * probability_similarity)
            + (Decimal("0.30") * gap_similarity)
            + (Decimal("0.15") * threshold_similarity)
            + (Decimal("0.20") * regime_similarity)
        ) * side_similarity * correlation_weight
        if score > best_score:
            best_score = score
            best_city = open_city_id
            best_market = position.market_ticker
    return {
        "threshold_concentration_score": str(best_score),
        "threshold_concentration_block": best_score >= Decimal("0.28"),
        "threshold_concentration_city": best_city,
        "threshold_concentration_market": best_market,
    }


def _latest_forecasts_as_of(
    forecasts: Iterable[ForecastSnapshot],
    as_of_time: datetime,
) -> list[ForecastSnapshot]:
    source = [forecast for forecast in forecasts if forecast.provider_run_time <= as_of_time]
    if not source:
        source = list(forecasts)
    latest_by_provider: dict[str, ForecastSnapshot] = {}
    for forecast in sorted(source, key=lambda item: (item.provider_run_time, item.provider_id)):
        latest_by_provider[forecast.provider_id] = forecast
    return sorted(
        latest_by_provider.values(),
        key=lambda item: (item.provider_run_time, item.provider_id),
        reverse=True,
    )


def run_market_decision_cycle(
    market: MarketSnapshot,
    market_definition,
    orderbook: OrderbookSnapshot,
    recent_orderbooks: list[OrderbookSnapshot],
    recent_trades: list[TradeSnapshot] | None,
    observations: list[ObservationSnapshot],
    forecasts: list[ForecastSnapshot],
    city_profile: CityProfile,
    station,
    qualification_state: QualificationState,
    provider_reliability: dict[str, Decimal] | None = None,
    provider_bias_adjustments: dict[str, Decimal] | None = None,
    provider_calibration_report: Mapping[str, Any] | None = None,
    open_position_signals: list[Mapping[str, Any]] | None = None,
    run_mode: RunMode = RunMode.SHADOW,
    fee_multiplier: int | None = 1,
    open_positions: list[tuple[str, ShadowPosition]] | None = None,
    active_kill_switch: bool = False,
    as_of_time: datetime | None = None,
    yesterday_high_f: Decimal | None = None,
    spc_outlook_rank: int = 0,
    afd_confidence: str | None = None,
    afd_model_spread_flag: bool | None = None,
    afd_regime: str | None = None,
    afd_mentioned_today_high_f: int | None = None,
    nws_forecast_revisions_24h: int | None = None,
    gefs_ensemble_std_f: float | None = None,
    gefs_ensemble_mean_f: float | None = None,
    gefs_ensemble_member_count: int | None = None,
    intraday_obs_forecast_bias_f: float | None = None,
    intraday_obs_sample_hours: int | None = None,
) -> DecisionCycleResult:
    as_of_time = as_of_time or datetime.now(timezone.utc)
    settlement_rule = parse_settlement_rule(market_definition, station)
    available_forecasts = _latest_forecasts_as_of(forecasts, as_of_time)
    lead_hours = max(
        0.0,
        (settlement_rule.local_standard_window_end - as_of_time).total_seconds() / 3600.0,
    )
    season_key = _season_key(as_of_time)
    derived_provider_reliability = (
        extract_provider_reliability_weights(
            provider_calibration_report,
            season_key=season_key,
            lead_hours=lead_hours,
        )
        if provider_calibration_report is not None
        else (provider_reliability or {})
    )
    derived_provider_bias_adjustments = (
        extract_provider_bias_adjustments(
            provider_calibration_report,
            season_key=season_key,
            lead_hours=lead_hours,
        )
        if provider_calibration_report is not None
        else (provider_bias_adjustments or {})
    )
    derived_provider_sigma_overrides = (
        extract_provider_day_max_sigmas(
            provider_calibration_report,
            season_key=season_key,
            lead_hours=lead_hours,
        )
        if provider_calibration_report is not None
        else {}
    )
    derived_consensus_residual = (
        provider_calibration_report.get("consensus_residuals")
        if provider_calibration_report is not None
        else None
    )
    forecast_result = build_forecast_distribution(
        snapshots=available_forecasts,
        settlement_rule=settlement_rule,
        city_profile=city_profile,
        as_of_time=as_of_time,
        provider_reliability=derived_provider_reliability,
        provider_bias_adjustments=derived_provider_bias_adjustments,
        provider_sigma_overrides=derived_provider_sigma_overrides,
        consensus_residual=derived_consensus_residual,
    )
    current_state = build_current_state_estimate(
        observations=observations,
        forecast=available_forecasts[0] if available_forecasts else None,
        city_profile=city_profile,
        as_of_time=as_of_time,
        yesterday_high_f=yesterday_high_f,
        station_timezone=station.timezone,
        spc_outlook_rank=spc_outlook_rank,
        afd_confidence=afd_confidence,
        afd_model_spread_flag=afd_model_spread_flag,
        afd_regime=afd_regime,
        afd_mentioned_today_high_f=afd_mentioned_today_high_f,
        nws_forecast_revisions_24h=nws_forecast_revisions_24h,
        gefs_ensemble_std_f=gefs_ensemble_std_f,
        gefs_ensemble_mean_f=gefs_ensemble_mean_f,
        gefs_ensemble_member_count=gefs_ensemble_member_count,
        intraday_obs_forecast_bias_f=intraday_obs_forecast_bias_f,
        intraday_obs_sample_hours=intraday_obs_sample_hours,
    )
    path_result = apply_path_adjustment(
        distribution=forecast_result.distribution,
        current_state=current_state,
        observations=observations,
        settlement_rule=settlement_rule,
        city_profile=city_profile,
        as_of_time=as_of_time,
    )
    regime = build_regime_assessment(
        city_id=city_profile.city_id,
        current_state=current_state,
        path_state=path_result.path_state,
        as_of_time=as_of_time,
    )
    thresholds = DecisionThresholds()

    # 2026-05-18 fix: scope ``open_position`` to positions on the SAME
    # settlement date as the candidate market. Without this filter the
    # bot got stuck overnight: every city that had bet earlier in the
    # session (or had an open MAY-17 position rolling overnight) would
    # hit the ``other_market_position is not None`` branch below for
    # every new MAY-18 evaluation and downgrade to WATCH —
    # explanation_code "city_position_already_open".
    #
    # FOLLOW-UP fix (later 2026-05-18): also lift the per-(city, date)
    # cap from 1 to N where N = _MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY
    # (3, mirroring engines.shadow). Below that cap, an existing position
    # on a DIFFERENT market shouldn't be a hard blocker — the bot is
    # supposed to be able to fire on multiple non-overlapping bins per
    # city per day. shadow.py and live_execution still enforce their own
    # caps, so widening the decision-layer scope is safe.
    #
    # Mechanics:
    #   - ``open_position`` keeps prefer-same-ticker semantics so
    #     same-market position management (add/exit) still works.
    #   - If the only candidate position is a DIFFERENT same-date
    #     market AND we're still under the per-(city, date) cap, we
    #     report ``open_position=None`` so the decision engine's
    #     "other_market_position" gate doesn't fire. The portfolio
    #     diversity bookkeeping below (open_city_ids, active_city_count)
    #     is unaffected — it still sees the full open-position picture.
    market_settle_date = _ticker_settle_date(market.market_ticker)
    _SAME_CITY_DATE_CAP = 3  # mirrors engines.shadow._MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY
    _same_city_same_date_positions = [
        position
        for position_city_id, position in (open_positions or [])
        if position_city_id == city_profile.city_id
        and position.lifecycle_status == "OPEN"
        and _ticker_settle_date(position.market_ticker) == market_settle_date
    ]
    # Prefer an exact-ticker match so same-market position handling stays
    # intact (add-to-position / managed-exit paths in the branches below).
    same_market_position_candidate = next(
        (p for p in _same_city_same_date_positions if p.market_ticker == market.market_ticker),
        None,
    )
    if same_market_position_candidate is not None:
        open_position = same_market_position_candidate
    elif len(_same_city_same_date_positions) < _SAME_CITY_DATE_CAP:
        # Below the per-(city, date) cap — don't let an unrelated same-date
        # position trigger the WATCH gate. shadow.py will still cap the
        # actual fill at 3 distinct markets.
        open_position = None
    else:
        # At/over the cap — surface the first as a blocker so the engine
        # downgrades to WATCH like before.
        open_position = _same_city_same_date_positions[0]
    open_city_ids = {
        position_city_id
        for position_city_id, position in (open_positions or [])
        if position is not None and position.lifecycle_status == "OPEN"
    }
    correlation_matrix = build_regime_adjusted_correlation_matrix(
        sorted(open_city_ids | {city_profile.city_id}),
        regime_by_city={city_profile.city_id: regime.active_regime},
    )
    portfolio_risk = portfolio_risk_units(open_positions or [], correlation_matrix)
    candidate_portfolio_risk = _candidate_portfolio_risk_units(
        city_id=city_profile.city_id,
        market_ticker=market.market_ticker,
        open_positions=open_positions,
        open_city_ids=open_city_ids,
        correlation_matrix=correlation_matrix,
    )

    candidate_edges: list[
        tuple[str, EdgeEstimate, tuple[str, ...], object, RiskDecision, TradabilityAssessment]
    ] = []
    taker_tradability_scores: list[Decimal] = []
    maker_fill_probabilities: list[Decimal] = []
    ghost_liquidity_ratios: list[Decimal] = []
    quote_stability_scores: list[Decimal] = []
    durability_scores: list[Decimal] = []
    for side in ("yes", "no"):
        micro, tradability = assess_taker_side(
            snapshot=orderbook,
            recent_history=recent_orderbooks,
            recent_trades=recent_trades or [],
            market_close_time=market.close_time,
            side=side,
            quantity=Decimal("1"),
        )
        taker_tradability_scores.append(tradability.tradability_score)
        maker_fill_probabilities.append(micro.maker_fill_probability)
        ghost_liquidity_ratios.append(micro.ghost_liquidity_ratio)
        quote_stability_scores.append(micro.quote_stability_score)
        durability_scores.append(micro.top_of_book_durability_score)
        executable_price = micro.executable_wap_price or Decimal("1")
        taker_edge = compute_edge_estimate(
            market_ticker=market.market_ticker,
            side=side,
            quantity=Decimal("1"),
            p_yes=path_result.p_yes,
            executable_price=executable_price,
            micro=micro,
            path_state=path_result.path_state,
            regime=regime,
            fee_multiplier=fee_multiplier,
            execution_style="taker",
            provider_spread_f=forecast_result.provider_spread_f,
            provider_threshold_straddle_f=forecast_result.provider_threshold_straddle_f,
            portfolio_risk_units_value=candidate_portfolio_risk,
        )
        risk = build_risk_decision(
            qualification_state=qualification_state,
            current_state=current_state,
            tradability=tradability,
            thresholds=thresholds,
            raw_edge=taker_edge.raw_edge,
            executable_ev=taker_edge.executable_ev_per_contract,
            friction_to_edge_ratio=taker_edge.friction_to_edge_ratio,
            portfolio_risk_units_value=candidate_portfolio_risk,
            active_kill_switch=active_kill_switch,
        )
        candidate_edges.append(
            (
                "taker",
                taker_edge,
                () if risk.risk_state.value == "ALLOW" else risk.veto_reasons,
                micro,
                risk,
                tradability,
            )
        )

        maker_enabled = qualification_state in {
            QualificationState.SHADOW_QUALIFIED,
            QualificationState.LIVE_PILOT,
        }
        maker_price = (
            max(price for price, _size in orderbook.yes_bids_ladder)
            if side == "yes" and orderbook.yes_bids_ladder
            else max(price for price, _size in orderbook.no_bids_ladder)
            if side == "no" and orderbook.no_bids_ladder
            else None
        )
        if maker_price is not None:
            maker_edge = compute_edge_estimate(
                market_ticker=market.market_ticker,
                side=side,
                quantity=Decimal("1"),
                p_yes=path_result.p_yes,
                executable_price=maker_price,
                micro=micro,
                path_state=path_result.path_state,
                regime=regime,
                fee_multiplier=fee_multiplier,
                execution_style="maker",
                provider_spread_f=forecast_result.provider_spread_f,
                provider_threshold_straddle_f=forecast_result.provider_threshold_straddle_f,
                portfolio_risk_units_value=candidate_portfolio_risk,
            )
            maker_reasons = list(risk.veto_reasons)
            if not tradability.allowed_maker_flag:
                maker_reasons.append("maker_not_viable")
            if not maker_enabled:
                maker_reasons.append("maker_not_enabled_for_qualification")
            candidate_edges.append(("maker", maker_edge, tuple(maker_reasons), micro, risk, tradability))

    positive_taker_candidates = [
        item
        for item in candidate_edges
        if item[0] == "taker" and item[1].executable_ev_per_contract > Decimal("0") and not item[2]
    ]
    positive_maker_candidates = [
        item
        for item in candidate_edges
        if item[0] == "maker" and item[1].executable_ev_per_contract > Decimal("0") and not item[2]
    ]
    selected_edge: EdgeEstimate | None = None
    selected_style: str | None = None
    selected_micro = None
    selected_tradability: TradabilityAssessment | None = None
    final_decision = DecisionType.NO_TRADE
    explanation_codes = ["default_no_trade"]
    taker_block_reason_counts = _candidate_block_reason_counts(candidate_edges, style="taker")
    maker_block_reason_counts = _candidate_block_reason_counts(candidate_edges, style="maker")
    best_taker_candidate = max(
        (item for item in candidate_edges if item[0] == "taker"),
        key=lambda item: item[1].executable_ev_per_contract,
        default=None,
    )
    best_maker_candidate = max(
        (item for item in candidate_edges if item[0] == "maker"),
        key=lambda item: item[1].executable_ev_per_contract,
        default=None,
    )
    hard_halt_risk = next((item[4] for item in candidate_edges if item[4].hard_halt_flag), None)
    same_market_position = (
        open_position if open_position is not None and open_position.market_ticker == market.market_ticker else None
    )
    other_market_position = (
        open_position if open_position is not None and open_position.market_ticker != market.market_ticker else None
    )

    if same_market_position is not None:
        same_side_candidate = max(
            (
                item
                for item in candidate_edges
                if item[0] == "taker" and item[1].side == same_market_position.side
            ),
            key=lambda item: item[1].executable_ev_per_contract,
            default=None,
        )
        opposite_positive_candidate = max(
            (
                item
                for item in positive_taker_candidates
                if item[1].side != same_market_position.side
            ),
            key=lambda item: item[1].executable_ev_per_contract,
            default=None,
        )
        if hard_halt_risk is not None:
            final_decision = DecisionType.EXIT
            explanation_codes = ["exit_due_hard_halt", *hard_halt_risk.veto_reasons]
            if same_side_candidate is not None:
                selected_style, selected_edge, _reasons, selected_micro, _risk, selected_tradability = same_side_candidate
        elif regime.block_flag:
            final_decision = DecisionType.EXIT
            explanation_codes = [
                f"exit_due_regime_hard_block_{regime.active_regime.lower()}",
                f"open_side_{same_market_position.side}",
            ]
            if same_side_candidate is not None:
                (
                    selected_style,
                    selected_edge,
                    _reasons,
                    selected_micro,
                    _risk,
                    selected_tradability,
                ) = same_side_candidate
        elif opposite_positive_candidate is not None:
            (
                selected_style,
                selected_edge,
                _reasons,
                selected_micro,
                _risk,
                selected_tradability,
            ) = opposite_positive_candidate
            final_decision = DecisionType.EXIT
            explanation_codes = ["exit_due_reversal_signal", f"open_side_{same_market_position.side}"]
        elif same_side_candidate is not None:
            (
                selected_style,
                selected_edge,
                _reasons,
                selected_micro,
                _risk,
                selected_tradability,
            ) = same_side_candidate
            if selected_edge.executable_ev_per_contract <= OPEN_POSITION_EXIT_EV:
                final_decision = DecisionType.EXIT
                explanation_codes = ["exit_due_negative_hold_ev", f"open_side_{same_market_position.side}"]
            elif selected_edge.executable_ev_per_contract <= OPEN_POSITION_REDUCE_EV:
                final_decision = DecisionType.REDUCE
                explanation_codes = ["reduce_due_negative_hold_ev", f"open_side_{same_market_position.side}"]
            else:
                final_decision = DecisionType.NO_TRADE
                explanation_codes = ["hold_existing_position", f"open_side_{same_market_position.side}"]
        else:
            final_decision = DecisionType.NO_TRADE
            explanation_codes = ["hold_existing_position_no_reprice_signal"]
    elif hard_halt_risk is not None:
        final_decision = (
            DecisionType.HALT_GLOBAL if hard_halt_risk.global_halt_flag else DecisionType.HALT_CITY
        )
        explanation_codes = ["risk_halt_active", *hard_halt_risk.veto_reasons]
    elif regime.block_flag:
        final_decision = DecisionType.NO_TRADE
        explanation_codes = [f"regime_hard_block_{regime.active_regime.lower()}"]
    elif other_market_position is not None:
        if positive_taker_candidates:
            final_decision = DecisionType.WATCH
            explanation_codes = ["city_position_already_open", f"open_market_{other_market_position.market_ticker}"]
        elif any(
            edge.raw_edge > Decimal("0")
            for _style, edge, _reasons, _micro, _risk, _tradability in candidate_edges
        ):
            final_decision = DecisionType.WATCH
            explanation_codes = ["watch_raw_edge_city_position_open"]
        else:
            final_decision = DecisionType.NO_TRADE
            explanation_codes = ["city_position_already_open_no_signal"]
    elif positive_taker_candidates:
        selected_style, selected_edge, _reasons, selected_micro, _risk, selected_tradability = max(
            positive_taker_candidates,
            key=lambda item: item[1].executable_ev_per_contract,
        )
        if selected_edge.executable_ev_per_contract >= thresholds.min_executable_ev:
            final_decision = DecisionType.TAKER_ALLOWED
            explanation_codes = ["positive_executable_ev", f"side_{selected_edge.side}"]
        else:
            final_decision = DecisionType.WATCH
            explanation_codes = ["watch_executable_ev_below_threshold"]
    elif positive_maker_candidates:
        selected_style, selected_edge, _reasons, selected_micro, _risk, selected_tradability = max(
            positive_maker_candidates,
            key=lambda item: item[1].executable_ev_per_contract,
        )
        final_decision = DecisionType.MAKER_ONLY
        explanation_codes = ["positive_maker_ev", f"side_{selected_edge.side}"]
    elif any(
        edge.raw_edge > Decimal("0") for _style, edge, _reasons, _micro, _risk, _tradability in candidate_edges
    ):
        final_decision = DecisionType.WATCH
        explanation_codes = ["watch_raw_edge_present"]
        explanation_codes.extend(
            f"dominant_block_{reason}"
            for reason, _count in taker_block_reason_counts.most_common(3)
        )

    active_city_count = len(open_city_ids)
    if final_decision == DecisionType.TAKER_ALLOWED and city_profile.city_id not in open_city_ids:
        active_city_count += 1
    active_city_count = max(1, active_city_count)
    confidence_summary = _build_confidence_summary(
        selected_edge=selected_edge,
        selected_micro=selected_micro,
        selected_tradability=selected_tradability,
        qualification_state=qualification_state,
        current_state=current_state,
        hard_halt_flag=bool(hard_halt_risk),
    )
    concentration_summary = _threshold_concentration_summary(
        city_id=city_profile.city_id,
        market_ticker=market.market_ticker,
        selected_edge=selected_edge,
        path_state=path_result.path_state,
        regime=regime,
        open_positions=open_positions,
        open_position_signals=open_position_signals,
        correlation_matrix=correlation_matrix,
    )
    if (
        final_decision == DecisionType.TAKER_ALLOWED
        and bool(concentration_summary["threshold_concentration_block"])
    ):
        final_decision = DecisionType.WATCH
        explanation_codes = [
            "watch_threshold_concentration_risk",
            f"concentration_city_{concentration_summary['threshold_concentration_city']}",
            f"concentration_market_{concentration_summary['threshold_concentration_market']}",
        ]

    kelly_size = (
        kelly_contract_size(selected_edge.executable_ev_per_contract)
        if selected_edge is not None and final_decision == DecisionType.TAKER_ALLOWED
        else 0
    )

    explanation = StrategyDecisionExplanation(
        schema_version="1.0.0",
        decision_id=uuid4().hex,
        run_mode=run_mode,
        as_of_time=as_of_time,
        market_ticker=market.market_ticker,
        city_id=city_profile.city_id,
        station_id=station.station_id,
        settlement_rule_id=settlement_rule.settlement_rule_id,
        data_freshness={
            "observation_lag_minutes": current_state.observation_lag_minutes,
            "expected_observation_cadence_minutes": current_state.expected_observation_cadence_minutes,
            "observation_excess_lag_minutes": current_state.observation_excess_lag_minutes,
            "orderbook_age_seconds": int((as_of_time - orderbook.as_of_time).total_seconds()),
        },
        current_state={
            "current_temp_est_f": str(current_state.current_temp_est_f),
            "current_temp_sigma_f": str(current_state.current_temp_sigma_f),
            "shock_risk_score": str(current_state.shock_risk_score),
            "spc_outlook_rank": current_state.spc_outlook_rank,
            "afd_confidence": current_state.afd_confidence,
            "afd_model_spread_flag": current_state.afd_model_spread_flag,
        },
        forecast_summary={
            "provider_weights": {k: str(v) for k, v in forecast_result.provider_weights.items()},
            "provider_maxima_f": {k: str(v) for k, v in forecast_result.provider_maxima_f.items()},
            "provider_support_status": forecast_result.provider_support_status,
            "provider_spread_f": str(forecast_result.provider_spread_f),
            "provider_threshold_straddle_f": str(forecast_result.provider_threshold_straddle_f),
            "provider_bias_adjustments_f": {
                k: str(v) for k, v in forecast_result.provider_bias_adjustments_f.items()
            },
            "sigma_equivalent_f": str(forecast_result.distribution.sigma_equivalent_f),
            "provider_reliability_weights": {
                key: str(value) for key, value in derived_provider_reliability.items()
            },
            "forecast_provider_count": len(available_forecasts),
            "unsupported_provider_count": sum(
                1 for status in forecast_result.provider_support_status.values() if status != "mvp_supported"
            ),
        },
        path_state={
            "p_yes": str(path_result.p_yes),
            "threshold_gap_f": str(path_result.path_state.threshold_gap_f),
            "reachability_score": str(path_result.path_state.reachability_score),
            "path_uncertainty_addon": str(path_result.path_state.path_uncertainty_addon),
            "persistence_gap_f": (
                str(path_result.path_state.persistence_gap_f)
                if path_result.path_state.persistence_gap_f is not None
                else None
            ),
            "persistence_uncertainty_addon": str(
                path_result.path_state.persistence_uncertainty_addon
            ),
            "p_climatology": (
                str(path_result.path_state.p_climatology)
                if path_result.path_state.p_climatology is not None
                else None
            ),
            "p_pre_shrinkage": (
                str(path_result.path_state.p_pre_shrinkage)
                if path_result.path_state.p_pre_shrinkage is not None
                else None
            ),
            "shrinkage_weight": (
                str(path_result.path_state.shrinkage_weight)
                if path_result.path_state.shrinkage_weight is not None
                else None
            ),
        },
        microstructure_summary={
            "best_yes_ask": str(orderbook.implied_yes_asks_ladder[0][0]) if orderbook.implied_yes_asks_ladder else None,
            "best_no_ask": str(orderbook.implied_no_asks_ladder[0][0]) if orderbook.implied_no_asks_ladder else None,
            "best_yes_bid": str(max(price for price, _size in orderbook.yes_bids_ladder)) if orderbook.yes_bids_ladder else None,
            "best_no_bid": str(max(price for price, _size in orderbook.no_bids_ladder)) if orderbook.no_bids_ladder else None,
            "recent_trade_count": len(recent_trades or []),
            "selected_execution_style": selected_style,
            "max_taker_tradability_score": str(max(taker_tradability_scores)) if taker_tradability_scores else None,
            "max_maker_fill_probability": str(max(maker_fill_probabilities)) if maker_fill_probabilities else None,
            "max_quote_stability_score": str(max(quote_stability_scores)) if quote_stability_scores else None,
            "max_top_of_book_durability_score": str(max(durability_scores)) if durability_scores else None,
            "mean_ghost_liquidity_ratio": str(
                sum(ghost_liquidity_ratios) / Decimal(len(ghost_liquidity_ratios))
            )
            if ghost_liquidity_ratios
            else None,
            "selected_maker_fill_probability": str(selected_micro.maker_fill_probability)
            if selected_micro is not None
            else None,
            "selected_quote_stability_score": str(selected_micro.quote_stability_score)
            if selected_micro is not None
            else None,
            "selected_top_of_book_durability_score": str(selected_micro.top_of_book_durability_score)
            if selected_micro is not None
            else None,
            "selected_ghost_liquidity_ratio": str(selected_micro.ghost_liquidity_ratio)
            if selected_micro is not None
            else None,
            "selected_depth_consumed_levels": selected_micro.depth_consumed_levels if selected_micro is not None else None,
            "selected_time_to_close_bucket": selected_micro.time_to_close_bucket if selected_micro is not None else None,
            "selected_taker_tradability_score": str(selected_tradability.tradability_score)
            if selected_tradability is not None
            else None,
        },
        edge_summary={
            "selected_executable_ev": str(selected_edge.executable_ev_per_contract) if selected_edge else None,
            "selected_side": selected_edge.side if selected_edge else None,
            "selected_execution_style": selected_style,
            "selected_p_model": str(selected_edge.p_model) if selected_edge else None,
            "selected_p_market_exec": str(selected_edge.p_market_exec) if selected_edge else None,
            "selected_fee_cost": str(selected_edge.fee_cost) if selected_edge else None,
            "selected_raw_edge": str(selected_edge.raw_edge) if selected_edge else None,
            "selected_total_friction": str(selected_edge.total_friction) if selected_edge else None,
            "selected_slippage_cost": str(selected_edge.slippage_cost) if selected_edge else None,
            "selected_adverse_selection_penalty": str(selected_edge.adverse_selection_penalty)
            if selected_edge
            else None,
            "selected_friction_to_edge_ratio": str(selected_edge.friction_to_edge_ratio)
            if selected_edge
            else None,
            "selected_uncertainty_haircut": str(selected_edge.uncertainty_haircut)
            if selected_edge
            else None,
            "selected_portfolio_haircut": str(selected_edge.portfolio_haircut)
            if selected_edge
            else None,
            "selected_provider_spread_f": str(forecast_result.provider_spread_f),
            "selected_provider_threshold_straddle_f": str(forecast_result.provider_threshold_straddle_f),
            # 2026-05-26: expose the parsed settlement rule operator so the
            # shadow layer can gate by it without re-parsing. Critical for
            # the "less-yes" disable gate (the lane that produced 4W/35L
            # -$4.61 in the backtest). settlement_rule.operator is one of
            # ">", ">=", "<", "<=", "between".
            "settlement_operator": settlement_rule.operator,
            "settlement_threshold_f": str(settlement_rule.threshold_f) if settlement_rule.threshold_f is not None else None,
            "best_taker_candidate": _candidate_summary_payload(best_taker_candidate),
            "best_maker_candidate": _candidate_summary_payload(best_maker_candidate),
            "confidence_summary": confidence_summary,
            "kelly_recommended_contracts": kelly_size,
        },
        regime_summary={
            "active_regime": regime.active_regime,
            "block_flag": regime.block_flag,
            "haircut_value": str(regime.haircut_value),
            "sizing_multiplier": str(regime.sizing_multiplier),
            "threshold_concentration_score": concentration_summary["threshold_concentration_score"],
            "threshold_concentration_block": concentration_summary["threshold_concentration_block"],
            "threshold_concentration_city": concentration_summary["threshold_concentration_city"],
            "threshold_concentration_market": concentration_summary["threshold_concentration_market"],
        },
        risk_summary={
            "qualification_state": qualification_state.value,
            "portfolio_risk_units": str(portfolio_risk),
            "candidate_portfolio_risk_units": str(candidate_portfolio_risk),
            "active_kill_switch": active_kill_switch,
            "hard_halt_flag": bool(hard_halt_risk),
            "city_halt_flag": hard_halt_risk.city_halt_flag if hard_halt_risk else False,
            "global_halt_flag": hard_halt_risk.global_halt_flag if hard_halt_risk else False,
            "hard_halt_reasons": hard_halt_risk.veto_reasons if hard_halt_risk else (),
            "regime_block_flag": regime.block_flag,
            "regime_block_reason": regime.active_regime if regime.block_flag else None,
            "open_position_market_ticker": open_position.market_ticker if open_position else None,
            "open_position_side": open_position.side if open_position else None,
            "threshold_concentration_score": concentration_summary["threshold_concentration_score"],
            "threshold_concentration_block": concentration_summary["threshold_concentration_block"],
            "active_city_count": active_city_count,
            "taker_candidate_count": sum(
                1
                for style, _edge, reasons, _micro, _risk, _tradability in candidate_edges
                if style == "taker" and not reasons
            ),
            "taker_block_reason_counts": dict(taker_block_reason_counts),
            "maker_candidate_count": sum(
                1
                for style, _edge, reasons, _micro, _risk, _tradability in candidate_edges
                if style == "maker" and not reasons
            ),
            "maker_block_reason_counts": dict(maker_block_reason_counts),
        },
        final_decision=final_decision,
        explanation_codes=tuple(explanation_codes),
        provenance_refs=tuple(obs.source_payload_id for obs in observations[:2]) + orderbook.source_refs,
        module_versions={
            "forecast": "v2",
            "nowcast": "v1",
            "path": "v1",
            "microstructure": "v2",
            "ev": "v1",
            "risk": "v1",
        },
    )
    return DecisionCycleResult(explanation=explanation, selected_edge=selected_edge)
