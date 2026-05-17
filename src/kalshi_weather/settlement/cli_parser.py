from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
import re
from zoneinfo import ZoneInfo

from kalshi_weather.domain.enums import ReportStatus
from kalshi_weather.domain.models import SettlementReportSnapshot, StationReference


PARSER_VERSION = "cli_parser_v1"
SOURCE_KIND = "NWS_CLI_EXACT"

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

CLI_ID_RE = re.compile(r"^(CLI[A-Z0-9]+)\s*$", re.MULTILINE)
ISSUE_TIME_RE = re.compile(
    r"^\s*(\d{1,4})\s+(AM|PM)\s+([A-Z]{3,4})\s+[A-Z]{3}\s+([A-Z]{3})\s+(\d{1,2})\s+(\d{4})\s*$",
    re.MULTILINE,
)
SUMMARY_DATE_RE = re.compile(
    r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})", re.IGNORECASE
)
MAXIMUM_RE = re.compile(
    r"^\s*MAXIMUM\s+(-?\d+)[A-Z]?\s+(\d{1,2}:?\d{2}|\d{1,4})\s+(AM|PM)\b",
    re.MULTILINE,
)
MINIMUM_RE = re.compile(
    r"^\s*MINIMUM\s+(-?\d+)[A-Z]?\s+(\d{1,2}:?\d{2}|\d{1,4})\s+(AM|PM)\b",
    re.MULTILINE,
)


class SettlementCliParseError(ValueError):
    """Raised when a climate report cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class ParsedIssueTime:
    local_issue_time: datetime
    settlement_date: date


def _parse_query_version(source_url: str | None) -> int | None:
    if not source_url:
        return None
    parsed = urlparse(source_url)
    values = parse_qs(parsed.query).get("version")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def _parse_compact_local_time(
    token: str,
    meridiem: str,
    settlement_date: date,
    tz: ZoneInfo,
) -> datetime:
    digits = token.strip().replace(":", "")
    if len(digits) <= 2:
        hour = int(digits)
        minute = 0
    else:
        hour = int(digits[:-2])
        minute = int(digits[-2:])

    hour %= 12
    if meridiem == "PM":
        hour += 12

    return datetime.combine(
        settlement_date,
        time(hour=hour, minute=minute, tzinfo=tz),
    )


def _parse_issue_time(report_text: str, station: StationReference) -> ParsedIssueTime:
    match = ISSUE_TIME_RE.search(report_text)
    if not match:
        raise SettlementCliParseError("could not locate report issue time")

    compact_time, meridiem, _abbr, month_abbr, day_token, year_token = match.groups()
    tz = ZoneInfo(station.timezone)
    issue_date = date(
        int(year_token),
        MONTHS[month_abbr.upper()],
        int(day_token),
    )
    local_issue_time = _parse_compact_local_time(compact_time, meridiem, issue_date, tz)
    settlement_date = _parse_summary_date(report_text)
    return ParsedIssueTime(local_issue_time=local_issue_time, settlement_date=settlement_date)


def _parse_summary_date(report_text: str) -> date:
    match = SUMMARY_DATE_RE.search(report_text)
    if not match:
        raise SettlementCliParseError("could not locate settlement summary date")

    month_name, day_token, year_token = match.groups()
    month_number = MONTHS[month_name[:3].upper()]
    return date(int(year_token), month_number, int(day_token))


def _local_standard_window(settlement_date: date, station: StationReference) -> tuple[datetime, datetime]:
    tz = ZoneInfo(station.timezone)
    noon = datetime.combine(settlement_date, time(12, 0), tzinfo=tz)
    is_dst = bool(noon.dst())

    start_hour = 1 if is_dst else 0
    end_date = settlement_date + timedelta(days=1 if is_dst else 0)
    end_hour = 0 if is_dst else 23
    end_minute = 59

    start = datetime.combine(settlement_date, time(start_hour, 0), tzinfo=tz)
    end = datetime.combine(end_date, time(end_hour, end_minute, 59), tzinfo=tz)
    return start, end


def _parse_temperature_line(
    regex: re.Pattern[str],
    report_text: str,
    settlement_date: date,
    station: StationReference,
) -> tuple[int | None, datetime | None]:
    match = regex.search(report_text)
    if not match:
        return None, None
    value_token, compact_time, meridiem = match.groups()
    tz = ZoneInfo(station.timezone)
    observed_time = _parse_compact_local_time(compact_time, meridiem, settlement_date, tz)
    return int(value_token), observed_time


def _report_status(issue_time: datetime, settlement_date: date) -> ReportStatus:
    if issue_time.date() > settlement_date:
        return ReportStatus.CANDIDATE_FINAL
    return ReportStatus.PRELIMINARY


def parse_cli_climate_report(
    report_text: str,
    station: StationReference,
    raw_text_payload_id: str,
    source_url: str | None = None,
    report_version: int | None = None,
) -> SettlementReportSnapshot:
    climate_id_match = CLI_ID_RE.search(report_text)
    if not climate_id_match:
        raise SettlementCliParseError("could not locate CLI product id")

    climate_product_id = climate_id_match.group(1)
    parsed_issue = _parse_issue_time(report_text, station)
    max_temp_f, max_temp_time_local = _parse_temperature_line(
        MAXIMUM_RE, report_text, parsed_issue.settlement_date, station
    )
    min_temp_f, _min_temp_time_local = _parse_temperature_line(
        MINIMUM_RE, report_text, parsed_issue.settlement_date, station
    )
    if max_temp_f is None:
        raise SettlementCliParseError("could not locate MAXIMUM field")

    resolved_version = report_version or _parse_query_version(source_url) or 1
    local_standard_window_start, local_standard_window_end = _local_standard_window(
        parsed_issue.settlement_date, station
    )

    revision_flags: list[str] = []
    if parsed_issue.local_issue_time.date() > parsed_issue.settlement_date:
        revision_flags.append("next_day_issue")

    return SettlementReportSnapshot(
        station_id=station.station_id,
        climate_product_id=climate_product_id,
        issue_time=parsed_issue.local_issue_time,
        report_version=resolved_version,
        report_status=_report_status(parsed_issue.local_issue_time, parsed_issue.settlement_date),
        local_standard_window_start=local_standard_window_start,
        local_standard_window_end=local_standard_window_end,
        max_temp_f=max_temp_f,
        max_temp_time_local=max_temp_time_local,
        min_temp_f=min_temp_f,
        raw_text_payload_id=raw_text_payload_id,
        parser_version=PARSER_VERSION,
        revision_flags=tuple(revision_flags),
        source_url=source_url,
        source_kind=SOURCE_KIND,
        source_locator=climate_product_id,
    )
