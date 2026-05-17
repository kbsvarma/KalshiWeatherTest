from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import unittest

from kalshi_weather.domain.enums import ReportStatus
from kalshi_weather.domain.models import StationReference
from kalshi_weather.settlement.cli_parser import parse_cli_climate_report


CLI_TEXT = """CDUS41 KOKX 060224
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK NY
1024 PM EDT SUN APR 05 2026

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR APRIL 5 2026...

CLIMATE NORMAL PERIOD 1991 TO 2020
CLIMATE RECORD PERIOD 1869 TO 2026

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
              VALUE   (LST)    VALUE       VALUE  FROM     YEAR
............................................................
TEMPERATURE (F)
  MAXIMUM         57    214 PM  80    1928  58     -1       59
  MINIMUM         42    251 AM  20    1874  42      0       43
"""

CLI_TEXT_COLON_TIME = """000
CDUS43 KLOT 060635
CLIMDW

CLIMATE REPORT
NATIONAL WEATHER SERVICE CHICAGO IL
135 AM CDT MON APR 06 2026

...THE CHICAGO-MIDWAY CLIMATE SUMMARY FOR APRIL 5 2026...

CLIMATE NORMAL PERIOD: 1991 TO 2020
CLIMATE RECORD PERIOD: 1928 TO 2026

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
              VALUE   (LST)  VALUE       VALUE  FROM      YEAR
...................................................................
TEMPERATURE (F)
  YESTERDAY
  MAXIMUM         53   4:10 PM  85    1988  56     -3       54
  MINIMUM         38   7:53 AM  18    1979  38      0       41
"""

CLI_TEXT_SUFFIX_VALUE = """000
CDUS41 KPHI 050608
CLIPHL

CLIMATE REPORT
NATIONAL WEATHER SERVICE MOUNT HOLLY NJ
208 AM EDT SUN APR 05 2026

...THE PHILADELPHIA PA CLIMATE SUMMARY FOR APRIL 4 2026...

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
...................................................................
TEMPERATURE (F)
  YESTERDAY
  MAXIMUM         84R 12:14 PM  80    1892  60     24       71
  MINIMUM         48  11:59 PM  25    1874  40      8       60
"""


class CliParserTest(unittest.TestCase):
    def test_parse_cli_report_extracts_maximum_and_local_standard_window(self) -> None:
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="station/KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )

        report = parse_cli_climate_report(
            CLI_TEXT,
            station=station,
            raw_text_payload_id="raw-1",
            source_url="https://forecast.weather.gov/product.php?site=OKX&issuedby=NYC&product=CLI&format=TXT&version=1&glossary=0",
        )

        self.assertEqual(report.climate_product_id, "CLINYC")
        self.assertEqual(report.max_temp_f, 57)
        self.assertEqual(report.min_temp_f, 42)
        self.assertEqual(report.report_version, 1)
        self.assertEqual(report.report_status, ReportStatus.PRELIMINARY)
        self.assertEqual(report.max_temp_time_local.hour, 14)
        self.assertEqual(report.max_temp_time_local.minute, 14)
        self.assertEqual(report.local_standard_window_start.hour, 1)
        self.assertEqual(report.local_standard_window_end.hour, 0)
        self.assertEqual(report.local_standard_window_end.minute, 59)
        self.assertEqual(report.issue_time, datetime(2026, 4, 5, 22, 24, tzinfo=report.issue_time.tzinfo))

    def test_parse_cli_report_accepts_colon_observed_times(self) -> None:
        station = StationReference(
            station_id="chi-midway",
            station_name="Chicago Midway Airport",
            metar_code="KMDW",
            nws_station_api_id="station/KMDW",
            climate_product_id="CLIMDW",
            latitude=Decimal("41.7868"),
            longitude=Decimal("-87.7522"),
            timezone="America/Chicago",
            wfo_office="LOT",
            grid_x=76,
            grid_y=70,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )

        report = parse_cli_climate_report(
            CLI_TEXT_COLON_TIME,
            station=station,
            raw_text_payload_id="raw-colon",
            source_url="https://api.weather.gov/products/example",
        )

        self.assertEqual(report.climate_product_id, "CLIMDW")
        self.assertEqual(report.max_temp_f, 53)
        self.assertEqual(report.min_temp_f, 38)
        self.assertEqual(report.max_temp_time_local.hour, 16)
        self.assertEqual(report.max_temp_time_local.minute, 10)

    def test_parse_cli_report_accepts_suffix_on_temperature_value(self) -> None:
        station = StationReference(
            station_id="phl-airport",
            station_name="Philadelphia International Airport",
            metar_code="KPHL",
            nws_station_api_id="station/KPHL",
            climate_product_id="CLIPHL",
            latitude=Decimal("39.87327"),
            longitude=Decimal("-75.22678"),
            timezone="America/New_York",
            wfo_office="PHI",
            grid_x=48,
            grid_y=72,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )

        report = parse_cli_climate_report(
            CLI_TEXT_SUFFIX_VALUE,
            station=station,
            raw_text_payload_id="raw-suffix",
            source_url="https://api.weather.gov/products/example",
        )

        self.assertEqual(report.climate_product_id, "CLIPHL")
        self.assertEqual(report.max_temp_f, 84)
        self.assertEqual(report.min_temp_f, 48)


if __name__ == "__main__":
    unittest.main()
