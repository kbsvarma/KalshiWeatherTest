from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .opportunities import select_scannable_market_decisions


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _clamp_probability(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


@dataclass(frozen=True, slots=True)
class DailySelectionThresholds:
    min_confidence_adjusted_win_probability: Decimal = Decimal("0.55")
    cheap_contract_price_ceiling: Decimal = Decimal("0.20")
    cheap_contract_min_win_probability: Decimal = Decimal("0.10")
    cheap_contract_win_probability_multiplier: Decimal = Decimal("1.7")
    min_confidence_margin: Decimal = Decimal("0")
    min_executable_ev: Decimal = Decimal("0.01")
    min_overall_trade_confidence: Decimal = Decimal("0.40")
    min_tradability_score: Decimal = Decimal("0.55")
    min_risk_reward_ratio: Decimal = Decimal("0")


def _candidate_source(edge_summary: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    if isinstance(edge_summary.get("best_taker_candidate"), Mapping):
        return "best_taker_candidate", edge_summary["best_taker_candidate"]  # type: ignore[index]
    if edge_summary.get("selected_side") is not None:
        return "selected_candidate", edge_summary
    return None, {}


def _confidence_summary(edge_summary: Mapping[str, Any]) -> Mapping[str, Any]:
    confidence = edge_summary.get("confidence_summary")
    if isinstance(confidence, Mapping):
        return confidence
    return {}


def _candidate_stale_data(payload: Mapping[str, Any]) -> bool:
    explanation_codes = {str(code) for code in (payload.get("explanation_codes") or ())}
    if "risk_halt_active" in explanation_codes:
        return True
    if "dominant_block_observation_lag_halt" in explanation_codes:
        return True
    risk_summary = payload.get("risk_summary")
    if isinstance(risk_summary, Mapping):
        if bool(risk_summary.get("hard_halt_flag")):
            return True
        if any("observation" in str(reason) for reason in (risk_summary.get("hard_halt_reasons") or ())):
            return True
    freshness = payload.get("data_freshness")
    if isinstance(freshness, Mapping):
        excess_lag = _decimal(freshness.get("observation_excess_lag_minutes"))
        if excess_lag is not None and excess_lag > Decimal("20"):
            return True
    return False


def _candidate_metrics(
    payload: Mapping[str, Any],
    *,
    thresholds: DailySelectionThresholds,
) -> dict[str, Any]:
    edge_summary = payload.get("edge_summary")
    if not isinstance(edge_summary, Mapping):
        edge_summary = {}
    confidence_summary = _confidence_summary(edge_summary)
    source_name, source_payload = _candidate_source(edge_summary)
    micro_summary = payload.get("microstructure_summary")
    if not isinstance(micro_summary, Mapping):
        micro_summary = {}
    regime_summary = payload.get("regime_summary")
    if not isinstance(regime_summary, Mapping):
        regime_summary = {}
    risk_summary = payload.get("risk_summary")
    if not isinstance(risk_summary, Mapping):
        risk_summary = {}

    side = str(source_payload.get("side") or edge_summary.get("selected_side") or "") or None
    current_final_decision = str(payload.get("final_decision") or "")
    p_win_model = _decimal(source_payload.get("p_model") or edge_summary.get("selected_p_model"))
    market_price = _decimal(source_payload.get("p_market_exec") or edge_summary.get("selected_p_market_exec"))
    executable_ev = _decimal(source_payload.get("executable_ev") or edge_summary.get("selected_executable_ev"))
    total_friction = _decimal(source_payload.get("total_friction") or edge_summary.get("selected_total_friction"))
    tradability_score = _decimal(
        source_payload.get("tradability_score") or micro_summary.get("selected_taker_tradability_score")
    )
    uncertainty_haircut = _decimal(
        source_payload.get("uncertainty_haircut") or edge_summary.get("selected_uncertainty_haircut")
    ) or Decimal("0")
    regime_haircut = _decimal(source_payload.get("regime_haircut") or regime_summary.get("haircut_value")) or Decimal(
        "0"
    )
    portfolio_haircut = _decimal(
        source_payload.get("portfolio_haircut") or edge_summary.get("selected_portfolio_haircut")
    ) or Decimal("0")
    raw_edge = _decimal(source_payload.get("raw_edge") or edge_summary.get("selected_raw_edge"))
    overall_trade_confidence = _decimal(confidence_summary.get("overall_trade_confidence"))
    if overall_trade_confidence is None:
        confidence_parts = [
            _decimal(confidence_summary.get("model_confidence")),
            _decimal(confidence_summary.get("execution_confidence")),
            _decimal(confidence_summary.get("governance_confidence")),
        ]
        available_confidence = [value for value in confidence_parts if value is not None]
        overall_trade_confidence = (
            sum(available_confidence) / Decimal(len(available_confidence))
            if available_confidence
            else Decimal("0.50")
        )

    confidence_adjusted_win_probability: Decimal | None = None
    reward_if_win: Decimal | None = None
    loss_if_lose: Decimal | None = None
    risk_reward_ratio: Decimal | None = None
    breakeven_win_probability: Decimal | None = None
    confidence_margin: Decimal | None = None
    selection_score: Decimal | None = None
    if p_win_model is not None and market_price is not None:
        friction = total_friction or Decimal("0")
        breakeven_win_probability = _clamp_probability(market_price + friction)
        confidence_weight = overall_trade_confidence or Decimal("0.50")
        confidence_adjusted_win_probability = _clamp_probability(
            breakeven_win_probability
            + (confidence_weight * (p_win_model - breakeven_win_probability))
        )
        reward_if_win = Decimal("1") - market_price
        loss_if_lose = market_price
        if loss_if_lose > Decimal("0"):
            risk_reward_ratio = reward_if_win / loss_if_lose
        confidence_margin = confidence_adjusted_win_probability - breakeven_win_probability
        executable_ev_value = executable_ev or Decimal("0")
        selection_score = executable_ev_value * confidence_adjusted_win_probability * confidence_weight

    applied_min_win_probability_floor = thresholds.min_confidence_adjusted_win_probability
    if market_price is not None and market_price < thresholds.cheap_contract_price_ceiling:
        applied_min_win_probability_floor = max(
            thresholds.cheap_contract_min_win_probability,
            market_price * thresholds.cheap_contract_win_probability_multiplier,
        )

    rejection_reasons: list[str] = []
    if side is None or p_win_model is None or market_price is None or executable_ev is None:
        rejection_reasons.append("missing_candidate_math")
    if current_final_decision != "TAKER_ALLOWED":
        rejection_reasons.append("decision_not_taker_allowed")
    if _candidate_stale_data(payload):
        rejection_reasons.append("stale_data")
    if risk_summary.get("hard_halt_flag"):
        rejection_reasons.append("hard_halt_active")
    if executable_ev is not None and executable_ev < thresholds.min_executable_ev:
        rejection_reasons.append("executable_ev_below_floor")
    if (
        confidence_adjusted_win_probability is not None
        and confidence_adjusted_win_probability < applied_min_win_probability_floor
    ):
        rejection_reasons.append("win_probability_below_floor")
    if (
        thresholds.min_confidence_margin > Decimal("0")
        and confidence_margin is not None
        and confidence_margin < thresholds.min_confidence_margin
    ):
        rejection_reasons.append("confidence_margin_below_floor")
    if tradability_score is not None and tradability_score < thresholds.min_tradability_score:
        rejection_reasons.append("tradability_below_floor")
    if overall_trade_confidence is not None and overall_trade_confidence < thresholds.min_overall_trade_confidence:
        rejection_reasons.append("overall_confidence_below_floor")
    if (
        thresholds.min_risk_reward_ratio > Decimal("0")
        and risk_reward_ratio is not None
        and risk_reward_ratio < thresholds.min_risk_reward_ratio
    ):
        rejection_reasons.append("risk_reward_below_floor")

    acceptance_reasons: list[str] = []
    if not rejection_reasons:
        acceptance_reasons.extend(
            [
                "positive_executable_ev",
                "confidence_adjusted_win_probability_above_floor",
                "confidence_margin_above_floor",
                "tradability_above_floor",
                f"side_{side}",
            ]
        )

    return {
        "market_ticker": str(payload.get("market_ticker") or ""),
        "as_of_time": payload.get("as_of_time"),
        "market_date": payload.get("market_date"),
        "city_id": str(payload.get("city_id") or ""),
        "candidate_source": source_name,
        "current_final_decision": str(payload.get("final_decision") or ""),
        "selected_side": side,
        "p_win_model": str(p_win_model) if p_win_model is not None else None,
        "confidence_adjusted_win_probability": str(confidence_adjusted_win_probability)
        if confidence_adjusted_win_probability is not None
        else None,
        "applied_min_win_probability_floor": str(applied_min_win_probability_floor),
        "market_price": str(market_price) if market_price is not None else None,
        "reward_if_win": str(reward_if_win) if reward_if_win is not None else None,
        "loss_if_lose": str(loss_if_lose) if loss_if_lose is not None else None,
        "risk_reward_ratio": str(risk_reward_ratio) if risk_reward_ratio is not None else None,
        "breakeven_win_probability": str(breakeven_win_probability)
        if breakeven_win_probability is not None
        else None,
        "confidence_margin_over_breakeven": str(confidence_margin) if confidence_margin is not None else None,
        "selected_executable_ev": str(executable_ev) if executable_ev is not None else None,
        "selected_raw_edge": str(raw_edge) if raw_edge is not None else None,
        "selected_total_friction": str(total_friction) if total_friction is not None else None,
        "selected_taker_tradability_score": str(tradability_score) if tradability_score is not None else None,
        "overall_trade_confidence": str(overall_trade_confidence)
        if overall_trade_confidence is not None
        else None,
        "acceptance_reasons": acceptance_reasons,
        "rejection_reasons": rejection_reasons,
        "accepted": not rejection_reasons,
        "selection_score": str(selection_score) if selection_score is not None else None,
        "explanation_codes": list(payload.get("explanation_codes") or ()),
        "risk_summary": dict(risk_summary),
        "regime_summary": dict(regime_summary),
    }


def _city_candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[float, float, float, float]:
    accepted = 1.0 if bool(candidate.get("accepted")) else 0.0
    confidence_adjusted_win_probability = float(_decimal(candidate.get("confidence_adjusted_win_probability")) or Decimal("0"))
    confidence_margin = float(_decimal(candidate.get("confidence_margin_over_breakeven")) or Decimal("-999"))
    executable_ev = float(_decimal(candidate.get("selected_executable_ev")) or Decimal("-999"))
    return (
        -accepted,
        -confidence_adjusted_win_probability,
        -confidence_margin,
        -executable_ev,
    )


def build_city_daily_selection(
    decision_payloads: list[dict[str, Any]],
    *,
    market_day_mode: str,
    timezone_by_city: Mapping[str, str] | None = None,
    thresholds: DailySelectionThresholds | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or DailySelectionThresholds()
    latest_payloads = select_scannable_market_decisions(
        [dict(payload) for payload in decision_payloads],
        market_day_mode=market_day_mode,
        timezone_by_city=timezone_by_city,
        exclude_open_cities=False,
    )
    candidates = [
        _candidate_metrics(payload, thresholds=thresholds)
        for payload in latest_payloads
    ]
    candidates.sort(key=_city_candidate_sort_key)
    best_candidate = candidates[0] if candidates else None
    result = "SKIP"
    acceptance_reasons: list[str] = []
    rejection_reasons: list[str] = ["no_market_candidates_for_day"]
    if best_candidate is not None:
        if bool(best_candidate.get("accepted")):
            result = "BET"
            acceptance_reasons = list(best_candidate.get("acceptance_reasons") or ())
            rejection_reasons = []
        else:
            rejection_reasons = list(best_candidate.get("rejection_reasons") or ())
    return {
        "market_day_mode": market_day_mode,
        "market_count": len(latest_payloads),
        "evaluated_candidate_count": len(candidates),
        "accepted_candidate_count": sum(1 for item in candidates if item.get("accepted")),
        "result": result,
        "best_candidate": best_candidate,
        "acceptance_reasons": acceptance_reasons,
        "rejection_reasons": rejection_reasons,
        "thresholds": {
            "min_confidence_adjusted_win_probability": str(
                thresholds.min_confidence_adjusted_win_probability
            ),
            "min_confidence_margin": str(thresholds.min_confidence_margin),
            "min_executable_ev": str(thresholds.min_executable_ev),
            "min_overall_trade_confidence": str(thresholds.min_overall_trade_confidence),
            "min_tradability_score": str(thresholds.min_tradability_score),
            "min_risk_reward_ratio": str(thresholds.min_risk_reward_ratio),
        },
        "evaluated_candidates": candidates,
    }
