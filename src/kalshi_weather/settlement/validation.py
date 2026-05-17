from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from kalshi_weather.domain.models import SettlementReportSnapshot, SettlementRule


@dataclass(frozen=True, slots=True)
class SettlementValidationResult:
    market_ticker: str
    expected_result: str
    actual_result: str
    matched: bool
    settlement_temperature_f: Decimal | None
    notes: tuple[str, ...]


def evaluate_settlement_result(
    rule: SettlementRule,
    report: SettlementReportSnapshot,
) -> str:
    if report.max_temp_f is None:
        raise ValueError("settlement report missing max temperature")
    if rule.threshold_f is None:
        raise ValueError("settlement rule missing threshold")

    observed = Decimal(report.max_temp_f)
    threshold = rule.threshold_f

    if rule.operator == ">":
        return "yes" if observed > threshold else "no"
    if rule.operator == "<":
        return "yes" if observed < threshold else "no"
    if rule.operator == ">=":
        return "yes" if observed >= threshold else "no"
    if rule.operator == "<=":
        return "yes" if observed <= threshold else "no"
    raise ValueError(f"unsupported operator: {rule.operator}")


def validate_market_against_report(
    market_payload: Mapping[str, object],
    rule: SettlementRule,
    report: SettlementReportSnapshot,
) -> SettlementValidationResult:
    actual_result = str(market_payload.get("result", "")).lower()
    expected_result = evaluate_settlement_result(rule, report)
    notes: list[str] = []
    if report.report_status.value != "FINALIZED":
        notes.append(f"report_status={report.report_status.value}")
    if report.source_kind:
        notes.append(f"report_source={report.source_kind}")
    return SettlementValidationResult(
        market_ticker=rule.market_ticker,
        expected_result=expected_result,
        actual_result=actual_result,
        matched=expected_result == actual_result,
        settlement_temperature_f=(
            Decimal(report.max_temp_f) if report.max_temp_f is not None else None
        ),
        notes=tuple(notes),
    )
