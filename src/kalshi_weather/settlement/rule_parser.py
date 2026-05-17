from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from kalshi_weather.domain.enums import SettlementValidationStatus
from kalshi_weather.domain.models import MarketDefinition, SettlementRule, StationReference


RULE_RE_HIGH = re.compile(
    r"(?:highest|maximum) temperature recorded (?:in|at) (?P<station>.+?) for (?P<month>[A-Z][a-z]{2,8}) (?P<day>\d{1,2}), (?P<year>\d{4}).+? is (?P<operator>greater than or equal to|less than or equal to|greater than|less than) (?P<threshold>\d+(?:\.\d+)?)°",
    re.IGNORECASE,
)

RULE_RE_LOW = re.compile(
    r"(?:lowest|minimum) temperature recorded (?:in|at) (?P<station>.+?) for (?P<month>[A-Z][a-z]{2,8}) (?P<day>\d{1,2}), (?P<year>\d{4}).+? is (?P<operator>greater than or equal to|less than or equal to|greater than|less than) (?P<threshold>\d+(?:\.\d+)?)°",
    re.IGNORECASE,
)

# Range/bracket markets ("-Bxx.5" tickers):
#   "the highest temperature ... for May 17, 2026 ... is between 85-86°"
# Matches both HIGH and LOW range markets.
RULE_RE_RANGE = re.compile(
    r"(?:highest|maximum|lowest|minimum) temperature recorded (?:in|at) (?P<station>.+?) "
    r"for (?P<month>[A-Z][a-z]{2,8}) (?P<day>\d{1,2}), (?P<year>\d{4})"
    r".+? is between (?P<floor>\d+(?:\.\d+)?)-(?P<cap>\d+(?:\.\d+)?)°",
    re.IGNORECASE,
)


class SettlementRuleParseError(ValueError):
    """Raised when a market's rules cannot be converted into settlement semantics."""


def _month_number(month_name: str) -> int:
    months = {
        "January": 1,
        "Jan": 1,
        "February": 2,
        "Feb": 2,
        "March": 3,
        "Mar": 3,
        "April": 4,
        "Apr": 4,
        "May": 5,
        "June": 6,
        "Jun": 6,
        "July": 7,
        "Jul": 7,
        "August": 8,
        "Aug": 8,
        "September": 9,
        "Sep": 9,
        "October": 10,
        "Oct": 10,
        "November": 11,
        "Nov": 11,
        "December": 12,
        "Dec": 12,
    }
    return months[month_name]


def _resolve_operator(operator_text: str) -> tuple[str, bool]:
    normalized = operator_text.lower()
    if normalized == "greater than":
        return ">", False
    if normalized == "less than":
        return "<", False
    if normalized == "greater than or equal to":
        return ">=", True
    if normalized == "less than or equal to":
        return "<=", True
    raise SettlementRuleParseError(f"unsupported operator: {operator_text}")


def parse_settlement_rule(
    market: MarketDefinition,
    station: StationReference,
    parser_version: str = "settlement_rule_parser_v1",
) -> SettlementRule:
    if not market.rules_primary:
        raise SettlementRuleParseError("rules_primary is required")

    # Try range/bracket pattern first ("-B" markets are common and the regex is
    # the most specific). Then HIGH-temp, then LOW-temp.
    range_match = RULE_RE_RANGE.search(market.rules_primary)
    if range_match:
        month_name = range_match.group("month")
        day_token = int(range_match.group("day"))
        year_token = int(range_match.group("year"))
        floor_value = Decimal(range_match.group("floor"))
        cap_value = Decimal(range_match.group("cap"))
        # "Lowest" appears in LOW range markets; default to high.
        is_low_range = bool(
            re.search(r"\b(lowest|minimum)\b", market.rules_primary, re.IGNORECASE)
        )
        settlement_variable = (
            "daily_low_temperature_f" if is_low_range else "daily_high_temperature_f"
        )
        operator = "between"
        inclusive_flag = True  # "between X-Y" is inclusive of both ends
        threshold = floor_value
        threshold_high: Decimal | None = cap_value
        match = range_match
    else:
        match = RULE_RE_HIGH.search(market.rules_primary)
        settlement_variable = "daily_high_temperature_f"
        if not match:
            match = RULE_RE_LOW.search(market.rules_primary)
            settlement_variable = "daily_low_temperature_f"
        if not match:
            raise SettlementRuleParseError("could not parse weather settlement rule")
        month_name = match.group("month")
        day_token = int(match.group("day"))
        year_token = int(match.group("year"))
        threshold = Decimal(match.group("threshold"))
        operator, inclusive_flag = _resolve_operator(match.group("operator"))
        threshold_high = None

    settlement_date = date(year_token, _month_number(month_name), day_token)
    tz = ZoneInfo(station.timezone)
    noon = datetime.combine(settlement_date, time(12, 0), tzinfo=tz)
    is_dst = bool(noon.dst())
    start_hour = 1 if is_dst else 0
    start = datetime.combine(settlement_date, time(start_hour, 0), tzinfo=tz)
    end_date = date.fromordinal(settlement_date.toordinal() + (1 if is_dst else 0))
    end = datetime.combine(
        end_date,
        time(23, 59, 59) if not is_dst else time(0, 59, 59),
        tzinfo=tz,
    )

    return SettlementRule(
        settlement_rule_id=f"{market.market_ticker}:{parser_version}",
        market_ticker=market.market_ticker,
        settlement_variable=settlement_variable,
        operator=operator,
        threshold_f=threshold,
        inclusive_flag=inclusive_flag,
        station_id=station.station_id,
        source_kind="NWS_DAILY_CLIMATE_REPORT",
        source_locator=station.climate_product_id,
        local_standard_window_start=start,
        local_standard_window_end=end,
        parser_version=parser_version,
        validation_status=SettlementValidationStatus.PENDING,
        ambiguity_flags=(),
        threshold_high_f=threshold_high,
    )
