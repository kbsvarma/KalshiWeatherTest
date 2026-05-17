from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import DefaultDict
from zoneinfo import ZoneInfo

from kalshi_weather.domain.enums import ReportStatus
from kalshi_weather.domain.models import SettlementReportSnapshot, StationReference


@dataclass(frozen=True, slots=True)
class SettlementRevisionPolicy:
    stable_minutes: int = 90
    final_monitor_end_hour_local: int = 5
    final_monitor_end_minute_local: int = 15


@dataclass(frozen=True, slots=True)
class RevisionResolution:
    resolved: bool
    final_report: SettlementReportSnapshot | None
    reason: str


class SettlementRevisionMonitor:
    def __init__(self, policy: SettlementRevisionPolicy | None = None) -> None:
        self.policy = policy or SettlementRevisionPolicy()
        self._reports: DefaultDict[tuple[str, date], list[SettlementReportSnapshot]] = defaultdict(list)

    def record(self, report: SettlementReportSnapshot) -> None:
        key = (report.climate_product_id, report.local_standard_window_start.date())
        reports = self._reports[key]
        duplicate = any(
            existing.report_version == report.report_version
            and existing.issue_time == report.issue_time
            for existing in reports
        )
        if duplicate:
            return
        reports.append(report)
        reports.sort(key=lambda item: (item.issue_time, item.report_version))

    def history(
        self, climate_product_id: str, settlement_date: date
    ) -> tuple[SettlementReportSnapshot, ...]:
        key = (climate_product_id, settlement_date)
        return tuple(self._reports.get(key, ()))

    def latest(
        self, climate_product_id: str, settlement_date: date
    ) -> SettlementReportSnapshot | None:
        history = self.history(climate_product_id, settlement_date)
        return history[-1] if history else None

    def resolve_final(
        self,
        climate_product_id: str,
        settlement_date: date,
        station: StationReference,
        as_of: datetime,
    ) -> RevisionResolution:
        history = self.history(climate_product_id, settlement_date)
        if not history:
            return RevisionResolution(False, None, "no reports observed")

        latest = history[-1]
        if latest.report_status == ReportStatus.FINALIZED:
            return RevisionResolution(True, latest, "explicitly finalized")

        tz = ZoneInfo(station.timezone)
        deadline = datetime.combine(
            settlement_date + timedelta(days=1),
            time(
                self.policy.final_monitor_end_hour_local,
                self.policy.final_monitor_end_minute_local,
                tzinfo=tz,
            ),
        )
        stable_until = latest.issue_time + timedelta(minutes=self.policy.stable_minutes)

        if latest.report_status == ReportStatus.CANDIDATE_FINAL and as_of >= stable_until:
            if as_of >= deadline:
                return RevisionResolution(True, latest, "monitoring deadline passed")
            return RevisionResolution(True, latest, "next-day candidate stable")

        if latest.report_status == ReportStatus.PRELIMINARY and as_of >= deadline:
            return RevisionResolution(False, None, "preliminary_only_past_deadline")

        return RevisionResolution(False, None, "awaiting later revisions")
