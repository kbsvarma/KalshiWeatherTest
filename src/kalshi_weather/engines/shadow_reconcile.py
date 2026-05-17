from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from kalshi_weather.domain.models import ShadowFill, ShadowPosition
from kalshi_weather.storage import SQLiteStateStore


@dataclass(frozen=True, slots=True)
class ShadowReconciliationResult:
    market_ticker: str
    settled: bool
    total_settled_pnl_dollars: Decimal
    updated_fill_count: int
    notes: tuple[str, ...]


def _settlement_payout(side: str, market_result: str) -> Decimal:
    normalized = market_result.lower()
    if normalized not in {"yes", "no"}:
        raise ValueError(f"unsupported market result: {market_result}")
    return Decimal("1") if side == normalized else Decimal("0")


def _fill_net_pnl(fill: ShadowFill, market_result: str) -> Decimal:
    payout = _settlement_payout(fill.side, market_result)
    gross = (payout - fill.modeled_fill_price_dollars) * fill.quantity_fp
    return gross - fill.modeled_fee_dollars - fill.modeled_slippage_dollars - fill.modeled_adverse_selection_dollars


def _resolve_market_result(
    market_payload: Mapping[str, object] | None,
    validation_payload: Mapping[str, object] | None,
) -> tuple[str | None, str | None]:
    payload = market_payload or {}
    status = str(payload.get("status") or "").lower()
    result = str(payload.get("result") or "").lower()
    if status in {"settled", "finalized"} and result in {"yes", "no"}:
        return result, "market_status"
    if validation_payload is not None and bool(validation_payload.get("revision_resolved")):
        validation_result = str(validation_payload.get("actual_result") or "").lower()
        if validation_result in {"yes", "no"}:
            return validation_result, "settlement_validation"
    return None, None


def reconcile_shadow_position(
    store: SQLiteStateStore,
    *,
    city_id: str,
    market_payload: Mapping[str, object] | None,
) -> ShadowReconciliationResult:
    position = store.get_shadow_position(city_id)
    if position is None:
        return ShadowReconciliationResult("", False, Decimal("0"), 0, ("no_shadow_position",))
    if position.lifecycle_status != "OPEN":
        return ShadowReconciliationResult(position.market_ticker, False, Decimal("0"), 0, ("position_not_open",))
    payload_ticker = str((market_payload or {}).get("ticker") or "")
    if payload_ticker and position.market_ticker != payload_ticker:
        return ShadowReconciliationResult(position.market_ticker, False, Decimal("0"), 0, ("market_ticker_mismatch",))

    validation_payload = store.get_settlement_validation_payload(city_id=city_id, market_ticker=position.market_ticker)
    if validation_payload is not None and not bool(validation_payload.get("revision_resolved")):
        return ShadowReconciliationResult(
            position.market_ticker,
            False,
            Decimal("0"),
            0,
            ("settlement_revision_unresolved",),
        )

    result, settlement_source = _resolve_market_result(market_payload, validation_payload)
    if result not in {"yes", "no"}:
        return ShadowReconciliationResult(position.market_ticker, False, Decimal("0"), 0, ("market_not_settled",))

    fills = store.list_shadow_fills(city_id=city_id, market_ticker=position.market_ticker)
    if not fills:
        return ShadowReconciliationResult(position.market_ticker, False, Decimal("0"), 0, ("no_shadow_fills",))

    total_pnl = Decimal("0")
    updated_count = 0
    for fill in fills:
        if fill.reconciliation_status == "SETTLED":
            continue
        updated_fill = ShadowFill(
            shadow_fill_id=fill.shadow_fill_id,
            decision_id=fill.decision_id,
            market_ticker=fill.market_ticker,
            side=fill.side,
            quantity_fp=fill.quantity_fp,
            modeled_fill_price_dollars=fill.modeled_fill_price_dollars,
            fill_scenario=fill.fill_scenario,
            fill_confidence=fill.fill_confidence,
            modeled_fee_dollars=fill.modeled_fee_dollars,
            modeled_slippage_dollars=fill.modeled_slippage_dollars,
            modeled_adverse_selection_dollars=fill.modeled_adverse_selection_dollars,
            fill_time=fill.fill_time,
            reconciliation_status="SETTLED",
            predicted_executable_ev_per_contract=fill.predicted_executable_ev_per_contract,
            predicted_executable_ev_total=fill.predicted_executable_ev_total,
            model_confidence=fill.model_confidence,
            execution_confidence=fill.execution_confidence,
            governance_confidence=fill.governance_confidence,
            overall_trade_confidence=fill.overall_trade_confidence,
            confidence_reasons=fill.confidence_reasons,
        )
        store.save_shadow_fill(city_id, updated_fill)
        total_pnl += _fill_net_pnl(fill, result)
        updated_count += 1

    updated_position = ShadowPosition(
        city_id=position.city_id,
        market_ticker=position.market_ticker,
        side=position.side,
        open_quantity_fp=Decimal("0"),
        avg_cost_dollars=position.avg_cost_dollars,
        cumulative_fees_dollars=position.cumulative_fees_dollars,
        mark_pnl_dollars=Decimal("0"),
        settled_pnl_dollars=position.settled_pnl_dollars + total_pnl,
        lifecycle_status="CLOSED",
    )
    store.save_shadow_position(updated_position)
    return ShadowReconciliationResult(
        market_ticker=position.market_ticker,
        settled=True,
        total_settled_pnl_dollars=total_pnl,
        updated_fill_count=updated_count,
        notes=("shadow_position_reconciled", f"settled_from_{settlement_source or 'unknown'}"),
    )
