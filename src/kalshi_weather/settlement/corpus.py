from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from kalshi_weather.domain.enums import ReportStatus
from kalshi_weather.domain.models import SettlementReportSnapshot, SettlementRule, StationReference
from kalshi_weather.market.payloads import market_definition_from_payload
from kalshi_weather.settlement.revision_monitor import SettlementRevisionMonitor
from kalshi_weather.settlement.rule_parser import SettlementRuleParseError, parse_settlement_rule
from kalshi_weather.settlement.validation import validate_market_against_report


MIN_PROXY_OVERLAP_DAYS = 5

REPORT_STATUS_PRIORITY = {
    "PRELIMINARY": 0,
    "CANDIDATE_FINAL": 1,
    "FINALIZED": 2,
}


@dataclass(frozen=True, slots=True)
class SettlementCorpusEntry:
    validation_id: str
    city_id: str
    market_ticker: str
    local_date: date
    expected_result: str | None
    actual_result: str | None
    matched: bool
    critical_mismatch: bool
    report_status: str | None
    report_version: int | None
    revision_resolved: bool
    revision_resolution_reason: str | None
    notes: tuple[str, ...]
    report_source_kind: str | None = None
    report_source_locator: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "city_id": self.city_id,
            "market_ticker": self.market_ticker,
            "local_date": self.local_date.isoformat(),
            "expected_result": self.expected_result,
            "actual_result": self.actual_result,
            "matched": self.matched,
            "critical_mismatch": self.critical_mismatch,
            "report_status": self.report_status,
            "report_version": self.report_version,
            "revision_resolved": self.revision_resolved,
            "revision_resolution_reason": self.revision_resolution_reason,
            "notes": list(self.notes),
            "report_source_kind": self.report_source_kind,
            "report_source_locator": self.report_source_locator,
        }


def _validation_id(city_id: str, market_ticker: str) -> str:
    return f"{city_id}:{market_ticker}"


def build_report_index(
    reports: Sequence[SettlementReportSnapshot],
) -> dict[date, SettlementReportSnapshot]:
    indexed: dict[date, SettlementReportSnapshot] = {}
    for report in sorted(
        reports,
        key=lambda item: (
            item.local_standard_window_start.date(),
            item.issue_time,
            REPORT_STATUS_PRIORITY.get(item.report_status.value, 0),
            item.report_version,
        ),
    ):
        report_date = report.local_standard_window_start.date()
        existing = indexed.get(report_date)
        if existing is None or (
            report.issue_time,
            REPORT_STATUS_PRIORITY.get(report.report_status.value, 0),
            report.report_version,
        ) > (
            existing.issue_time,
            REPORT_STATUS_PRIORITY.get(existing.report_status.value, 0),
            existing.report_version,
        ):
            indexed[report_date] = report
    return indexed


def _build_revision_state(
    reports: Sequence[SettlementReportSnapshot],
    station: StationReference,
    as_of: datetime,
) -> tuple[dict[date, SettlementReportSnapshot], dict[date, SettlementReportSnapshot], dict[date, str]]:
    latest_reports = build_report_index(reports)
    monitor = SettlementRevisionMonitor()
    for report in reports:
        monitor.record(report)

    resolved_reports: dict[date, SettlementReportSnapshot] = {}
    resolution_reasons: dict[date, str] = {}
    for report_date, latest_report in latest_reports.items():
        resolution = monitor.resolve_final(
            climate_product_id=latest_report.climate_product_id,
            settlement_date=report_date,
            station=station,
            as_of=as_of,
        )
        resolution_reasons[report_date] = resolution.reason
        if resolution.resolved and resolution.final_report is not None:
            resolved_reports[report_date] = resolution.final_report
    return resolved_reports, latest_reports, resolution_reasons


def summarize_proxy_overlap(
    exact_reports: Sequence[SettlementReportSnapshot],
    proxy_reports: Sequence[SettlementReportSnapshot],
) -> dict[str, Any]:
    exact_index = build_report_index(
        [
            report
            for report in exact_reports
            if report.report_status != ReportStatus.PRELIMINARY
        ]
    )
    proxy_index = build_report_index(proxy_reports)
    overlap_dates = sorted(set(exact_index).intersection(proxy_index))
    mismatch_dates = [
        report_date
        for report_date in overlap_dates
        if exact_index[report_date].max_temp_f != proxy_index[report_date].max_temp_f
    ]
    return {
        "proxy_report_count": len(proxy_index),
        "exact_report_count": len(exact_index),
        "overlap_count": len(overlap_dates),
        "mismatch_count": len(mismatch_dates),
        "mismatch_dates": [report_date.isoformat() for report_date in mismatch_dates],
        "proxy_eligible": bool(proxy_index)
        and len(overlap_dates) >= MIN_PROXY_OVERLAP_DAYS
        and not mismatch_dates,
    }


def validate_settlement_corpus(
    *,
    city_id: str,
    market_payloads: Sequence[Mapping[str, Any]],
    station: StationReference,
    reports: Sequence[SettlementReportSnapshot],
    proxy_reports: Sequence[SettlementReportSnapshot] = (),
    as_of: datetime | None = None,
) -> list[SettlementCorpusEntry]:
    as_of = as_of or datetime.now(timezone.utc)
    report_index, latest_report_index, resolution_reasons = _build_revision_state(reports, station, as_of)
    proxy_index = build_report_index(proxy_reports)
    proxy_overlap = summarize_proxy_overlap(tuple(report_index.values()), proxy_reports)
    proxy_eligible = bool(proxy_overlap.get("proxy_eligible"))
    results: list[SettlementCorpusEntry] = []
    for payload in market_payloads:
        market_definition = market_definition_from_payload(payload)
        try:
            rule = parse_settlement_rule(market_definition, station)
        except SettlementRuleParseError as exc:
            close_time = str(payload.get("close_time") or "")[:10]
            if not close_time:
                continue
            rules_text = str(payload.get("rules_primary") or "").lower()
            ticker = str(payload.get("ticker") or "")
            unsupported_mvp_market = ("between" in rules_text) or ("-B" in ticker)
            notes = [f"parse_error:{exc}"]
            if unsupported_mvp_market:
                notes.insert(0, "scope_excluded_unsupported_market")
            results.append(
                SettlementCorpusEntry(
                    validation_id=_validation_id(city_id, ticker),
                    city_id=city_id,
                    market_ticker=ticker,
                    local_date=date.fromisoformat(close_time),
                    expected_result=None,
                    actual_result=str(payload.get("result", "")).lower() or None,
                    matched=False,
                    critical_mismatch=False,
                    report_status=None,
                    report_version=None,
                    revision_resolved=False,
                    revision_resolution_reason=None,
                    notes=tuple(notes),
                    report_source_kind=None,
                    report_source_locator=None,
                )
            )
            continue
        local_date = rule.local_standard_window_start.date()
        report = report_index.get(local_date)
        if report is None:
            latest_report = latest_report_index.get(local_date)
            proxy_report = proxy_index.get(local_date)
            if latest_report is not None:
                reason = resolution_reasons.get(local_date, "awaiting_revision_resolution")
                if (
                    latest_report.report_status.name == "PRELIMINARY"
                    and proxy_report is not None
                    and proxy_eligible
                ):
                    validation = validate_market_against_report(payload, rule, proxy_report)
                    results.append(
                        SettlementCorpusEntry(
                            validation_id=_validation_id(city_id, validation.market_ticker),
                            city_id=city_id,
                            market_ticker=validation.market_ticker,
                            local_date=local_date,
                            expected_result=validation.expected_result,
                            actual_result=validation.actual_result,
                            matched=validation.matched,
                            critical_mismatch=not validation.matched,
                            report_status=proxy_report.report_status.value,
                            report_version=proxy_report.report_version,
                            revision_resolved=True,
                            revision_resolution_reason="proxy_fallback_after_preliminary_only",
                            notes=validation.notes
                            + (
                                "exact_preliminary_unresolved",
                                f"revision_resolution_reason={reason}",
                                f"proxy_overlap_count={proxy_overlap['overlap_count']}",
                                "proxy_overlap_verified",
                            ),
                            report_source_kind=proxy_report.source_kind,
                            report_source_locator=proxy_report.source_locator,
                        )
                    )
                    continue
                reason = resolution_reasons.get(local_date, "awaiting_revision_resolution")
                results.append(
                    SettlementCorpusEntry(
                        validation_id=_validation_id(city_id, str(payload.get("ticker") or "")),
                        city_id=city_id,
                        market_ticker=str(payload.get("ticker") or ""),
                        local_date=local_date,
                        expected_result=None,
                        actual_result=str(payload.get("result", "")).lower() or None,
                        matched=False,
                        critical_mismatch=False,
                        report_status=latest_report.report_status.value,
                        report_version=latest_report.report_version,
                        revision_resolved=False,
                        revision_resolution_reason=reason,
                        notes=("awaiting_revision_resolution", f"revision_resolution_reason={reason}"),
                        report_source_kind=latest_report.source_kind,
                        report_source_locator=latest_report.source_locator,
                    )
                )
                continue
            if proxy_report is not None:
                notes = [
                    "proxy_source_blocked",
                    f"proxy_overlap_count={proxy_overlap['overlap_count']}",
                    f"proxy_mismatch_count={proxy_overlap['mismatch_count']}",
                ]
                if proxy_overlap["mismatch_dates"]:
                    notes.append(f"proxy_mismatch_dates={','.join(proxy_overlap['mismatch_dates'])}")
                if proxy_eligible:
                    validation = validate_market_against_report(payload, rule, proxy_report)
                    results.append(
                        SettlementCorpusEntry(
                            validation_id=_validation_id(city_id, validation.market_ticker),
                            city_id=city_id,
                            market_ticker=validation.market_ticker,
                            local_date=local_date,
                            expected_result=validation.expected_result,
                            actual_result=validation.actual_result,
                            matched=validation.matched,
                            critical_mismatch=not validation.matched,
                            report_status=proxy_report.report_status.value,
                            report_version=proxy_report.report_version,
                            revision_resolved=True,
                            revision_resolution_reason="proxy_overlap_verified",
                            notes=validation.notes
                            + (
                                f"proxy_overlap_count={proxy_overlap['overlap_count']}",
                                "proxy_overlap_verified",
                            ),
                            report_source_kind=proxy_report.source_kind,
                            report_source_locator=proxy_report.source_locator,
                        )
                    )
                    continue
                results.append(
                    SettlementCorpusEntry(
                        validation_id=_validation_id(city_id, str(payload.get("ticker") or "")),
                        city_id=city_id,
                        market_ticker=str(payload.get("ticker") or ""),
                        local_date=local_date,
                        expected_result=None,
                        actual_result=str(payload.get("result", "")).lower() or None,
                        matched=False,
                        critical_mismatch=False,
                        report_status=proxy_report.report_status.value,
                        report_version=proxy_report.report_version,
                        revision_resolved=False,
                        revision_resolution_reason="proxy_overlap_not_verified",
                        notes=tuple(notes),
                        report_source_kind=proxy_report.source_kind,
                        report_source_locator=proxy_report.source_locator,
                    )
                )
                continue
            results.append(
                SettlementCorpusEntry(
                    validation_id=_validation_id(city_id, str(payload.get("ticker") or "")),
                    city_id=city_id,
                    market_ticker=str(payload.get("ticker") or ""),
                    local_date=local_date,
                    expected_result=None,
                    actual_result=str(payload.get("result", "")).lower() or None,
                    matched=False,
                    critical_mismatch=False,
                    report_status=None,
                    report_version=None,
                    revision_resolved=False,
                    revision_resolution_reason=None,
                    notes=("missing_settlement_report",),
                    report_source_kind=None,
                    report_source_locator=None,
                )
            )
            continue
        validation = validate_market_against_report(payload, rule, report)
        critical = not validation.matched
        results.append(
            SettlementCorpusEntry(
                validation_id=_validation_id(city_id, validation.market_ticker),
                city_id=city_id,
                market_ticker=validation.market_ticker,
                local_date=local_date,
                expected_result=validation.expected_result,
                actual_result=validation.actual_result,
                matched=validation.matched,
                critical_mismatch=critical,
                report_status=report.report_status.value,
                report_version=report.report_version,
                revision_resolved=True,
                revision_resolution_reason=resolution_reasons.get(local_date),
                notes=validation.notes,
                report_source_kind=report.source_kind,
                report_source_locator=report.source_locator,
            )
        )
    return results


def summarize_validation_entries(entries: Sequence[Mapping[str, Any] | SettlementCorpusEntry]) -> dict[str, Any]:
    total = 0
    matched = 0
    critical_mismatches = 0
    missing_reports = 0
    finalized_count = 0
    parse_error_count = 0
    scope_excluded_count = 0
    resolved_count = 0
    revision_resolved_count = 0
    exact_resolved_count = 0
    proxy_resolved_count = 0
    for entry in entries:
        payload = entry.to_dict() if isinstance(entry, SettlementCorpusEntry) else dict(entry)
        total += 1
        if payload.get("matched"):
            matched += 1
        if payload.get("critical_mismatch"):
            critical_mismatches += 1
        notes = tuple(payload.get("notes", ()))
        if "missing_settlement_report" in notes:
            missing_reports += 1
        if any(str(note).startswith("parse_error:") for note in notes):
            parse_error_count += 1
        if "scope_excluded_unsupported_market" in notes:
            scope_excluded_count += 1
        if payload.get("report_status") == "FINALIZED":
            finalized_count += 1
        if payload.get("revision_resolved"):
            revision_resolved_count += 1
        if (
            payload.get("expected_result") is not None
            and payload.get("report_status") is not None
            and bool(payload.get("revision_resolved"))
        ):
            resolved_count += 1
            if payload.get("report_source_kind") == "NWS_CLI_EXACT":
                exact_resolved_count += 1
            if payload.get("report_source_kind") == "NCEI_DAILY_SUMMARIES_PROXY":
                proxy_resolved_count += 1
    supportable_count = total - scope_excluded_count
    unresolved_count = supportable_count - resolved_count
    return {
        "validated_market_count": total,
        "supportable_market_count": supportable_count,
        "resolved_validation_count": resolved_count,
        "matched_market_count": matched,
        "critical_mismatch_count": critical_mismatches,
        "missing_report_count": missing_reports,
        "parse_error_count": parse_error_count,
        "scope_excluded_count": scope_excluded_count,
        "unresolved_entry_count": unresolved_count,
        "finalized_report_count": finalized_count,
        "revision_resolved_count": revision_resolved_count,
        "exact_resolved_count": exact_resolved_count,
        "proxy_resolved_count": proxy_resolved_count,
        "eligible_for_shadow_only": resolved_count >= 50 and critical_mismatches == 0 and unresolved_count == 0,
    }
