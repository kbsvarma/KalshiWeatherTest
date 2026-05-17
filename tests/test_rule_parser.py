from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.models import MarketDefinition, StationReference
from kalshi_weather.settlement.rule_parser import parse_settlement_rule


class RuleParserTest(unittest.TestCase):
    def test_parse_greater_than_rule(self) -> None:
        market = MarketDefinition(
            market_ticker="KXHIGHNY-26APR04-T75",
            event_ticker="KXHIGHNY-26APR04",
            series_ticker="KXHIGHNY",
            market_type="binary_threshold",
            threshold_f=None,
            operator="",
            open_time=datetime(2026, 4, 4, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 5, tzinfo=timezone.utc),
            settlement_ts=None,
            rules_primary="If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
            rules_secondary=None,
            price_level_structure="linear_cent",
        )
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )

        rule = parse_settlement_rule(market, station)
        self.assertEqual(rule.operator, ">")
        self.assertEqual(rule.threshold_f, Decimal("75"))
        self.assertEqual(rule.source_locator, "CLINYC")
        self.assertEqual(rule.local_standard_window_start.hour, 1)
        self.assertEqual(rule.local_standard_window_end.hour, 0)

    def test_parse_winter_rule_uses_standard_midnight_window(self) -> None:
        market = MarketDefinition(
            market_ticker="KXHIGHNY-26JAN04-T35",
            event_ticker="KXHIGHNY-26JAN04",
            series_ticker="KXHIGHNY",
            market_type="binary_threshold",
            threshold_f=None,
            operator="",
            open_time=datetime(2026, 1, 4, tzinfo=timezone.utc),
            close_time=datetime(2026, 1, 5, tzinfo=timezone.utc),
            settlement_ts=None,
            rules_primary="If the highest temperature recorded in Central Park, New York for January 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 35°, then the market resolves to Yes.",
            rules_secondary=None,
            price_level_structure="linear_cent",
        )
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        rule = parse_settlement_rule(market, station)
        self.assertEqual(rule.local_standard_window_start.hour, 0)
        self.assertEqual(rule.local_standard_window_end.hour, 23)

    def test_parse_rule_supports_recorded_at_wording(self) -> None:
        market = MarketDefinition(
            market_ticker="KXHIGHCHI-26APR07-T45",
            event_ticker="KXHIGHCHI-26APR07",
            series_ticker="KXHIGHCHI",
            market_type="binary_threshold",
            threshold_f=None,
            operator="",
            open_time=datetime(2026, 4, 7, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 8, tzinfo=timezone.utc),
            settlement_ts=None,
            rules_primary=(
                "If the highest temperature recorded at Chicago Midway, IL for April 07, 2026 "
                "according to the National Weather Service's Climatological Report (Daily), "
                "is greater than 45°, then the market resolves to Yes."
            ),
            rules_secondary=None,
            price_level_structure="linear_cent",
        )
        station = StationReference(
            station_id="chi-midway",
            station_name="Chicago Midway Airport",
            metar_code="KMDW",
            nws_station_api_id="KMDW",
            climate_product_id="CLIMDW",
            latitude=Decimal("41.78417"),
            longitude=Decimal("-87.75528"),
            timezone="America/Chicago",
            wfo_office="LOT",
            grid_x=72,
            grid_y=69,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        rule = parse_settlement_rule(market, station)
        self.assertEqual(rule.operator, ">")
        self.assertEqual(rule.threshold_f, Decimal("45"))

    def test_parse_rule_supports_boston_maximum_abbrev_month(self) -> None:
        market = MarketDefinition(
            market_ticker="KXHIGHTBOS-26APR05-T68",
            event_ticker="KXHIGHTBOS-26APR05",
            series_ticker="KXHIGHTBOS",
            market_type="binary_threshold",
            threshold_f=None,
            operator="",
            open_time=datetime(2026, 4, 5, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 6, tzinfo=timezone.utc),
            settlement_ts=None,
            rules_primary=(
                "If the maximum temperature recorded at Boston for Apr 5, 2026, "
                "is greater than 68° fahrenheit according to the National Weather "
                "Service's Climatological Report (Daily), then the market resolves to Yes."
            ),
            rules_secondary=None,
            price_level_structure="linear_cent",
        )
        station = StationReference(
            station_id="bos-logan",
            station_name="Boston Logan International Airport",
            metar_code="KBOS",
            nws_station_api_id="KBOS",
            climate_product_id="CLIBOS",
            latitude=Decimal("42.36056"),
            longitude=Decimal("-71.01056"),
            timezone="America/New_York",
            wfo_office="BOX",
            grid_x=73,
            grid_y=90,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        rule = parse_settlement_rule(market, station)
        self.assertEqual(rule.operator, ">")
        self.assertEqual(rule.threshold_f, Decimal("68"))


if __name__ == "__main__":
    unittest.main()
