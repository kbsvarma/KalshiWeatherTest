from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from kalshi_weather.domain.enums import ReportStatus
from kalshi_weather.domain.models import SettlementReportSnapshot, StationReference


PARSER_VERSION = "ncei_daily_summaries_v1"
SOURCE_KIND = "NCEI_DAILY_SUMMARIES_PROXY"


def _local_standard_window(settlement_date: date, station: StationReference) -> tuple[datetime, datetime]:
    tz = ZoneInfo(station.timezone)
    noon = datetime.combine(settlement_date, time(12, 0), tzinfo=tz)
    is_dst = bool(noon.dst())

    start_hour = 1 if is_dst else 0
    end_date = settlement_date + timedelta(days=1 if is_dst else 0)
    end_hour = 0 if is_dst else 23

    start = datetime.combine(settlement_date, time(start_hour, 0), tzinfo=tz)
    end = datetime.combine(end_date, time(end_hour, 59, 59), tzinfo=tz)
    return start, end


def build_ncei_proxy_report(
    *,
    station: StationReference,
    row: Mapping[str, object],
    source_url: str,
) -> SettlementReportSnapshot:
    settlement_date = date.fromisoformat(str(row["DATE"]))
    local_standard_window_start, local_standard_window_end = _local_standard_window(
        settlement_date, station
    )
    issue_time = local_standard_window_end + timedelta(days=2)
    max_temp = int(float(str(row["TMAX"]))) if row.get("TMAX") not in (None, "") else None
    min_temp = int(float(str(row["TMIN"]))) if row.get("TMIN") not in (None, "") else None
    ncei_station_id = station.ncei_station_id or str(row.get("STATION") or "")
    raw_text_payload_id = f"{ncei_station_id}:{settlement_date.isoformat()}"
    return SettlementReportSnapshot(
        station_id=station.station_id,
        climate_product_id=station.climate_product_id,
        issue_time=issue_time,
        report_version=1,
        report_status=ReportStatus.FINALIZED,
        local_standard_window_start=local_standard_window_start,
        local_standard_window_end=local_standard_window_end,
        max_temp_f=max_temp,
        max_temp_time_local=None,
        min_temp_f=min_temp,
        raw_text_payload_id=raw_text_payload_id,
        parser_version=PARSER_VERSION,
        revision_flags=("ncei_daily_summaries_proxy",),
        source_url=source_url,
        source_kind=SOURCE_KIND,
        source_locator=ncei_station_id or None,
    )


def build_ncei_proxy_reports(
    *,
    station: StationReference,
    rows: list[Mapping[str, object]],
    source_url_template: str,
) -> list[SettlementReportSnapshot]:
    reports: list[SettlementReportSnapshot] = []
    for row in rows:
        if row.get("DATE") in (None, "") or row.get("TMAX") in (None, ""):
            continue
        reports.append(
            build_ncei_proxy_report(
                station=station,
                row=row,
                source_url=source_url_template.format(date=str(row["DATE"])),
            )
        )
    return reports
