from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import unittest

from kalshi_weather.domain.models import StationReference
from kalshi_weather.settlement.cli_parser import parse_cli_climate_report
from kalshi_weather.settlement.revision_monitor import SettlementRevisionMonitor


PRELIM_TEXT = """CLINYC
CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK NY
1024 PM EDT SUN APR 05 2026
...THE CENTRAL PARK NY CLIMATE SUMMARY FOR APRIL 5 2026...
TEMPERATURE (F)
  MAXIMUM         57    214 PM  80    1928  58     -1       59
  MINIMUM         42    251 AM  20    1874  42      0       43
"""

FINAL_TEXT = """CLINYC
CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK NY
0245 AM EDT MON APR 06 2026
...THE CENTRAL PARK NY CLIMATE SUMMARY FOR APRIL 5 2026...
TEMPERATURE (F)
  MAXIMUM         58    255 PM  80    1928  58      0       59
  MINIMUM         42    251 AM  20    1874  42      0       43
"""


class RevisionMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.station = StationReference(
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

    def test_revision_monitor_waits_for_later_report_then_resolves(self) -> None:
        monitor = SettlementRevisionMonitor()
        prelim = parse_cli_climate_report(
            PRELIM_TEXT,
            station=self.station,
            raw_text_payload_id="prelim",
            source_url="https://example.test?version=1",
        )
        final_candidate = parse_cli_climate_report(
            FINAL_TEXT,
            station=self.station,
            raw_text_payload_id="final",
            source_url="https://example.test?version=2",
        )
        monitor.record(prelim)

        unresolved = monitor.resolve_final(
            climate_product_id="CLINYC",
            settlement_date=prelim.local_standard_window_start.date(),
            station=self.station,
            as_of=datetime(2026, 4, 5, 23, 30, tzinfo=prelim.issue_time.tzinfo),
        )
        self.assertFalse(unresolved.resolved)

        monitor.record(final_candidate)
        resolved = monitor.resolve_final(
            climate_product_id="CLINYC",
            settlement_date=prelim.local_standard_window_start.date(),
            station=self.station,
            as_of=datetime(2026, 4, 6, 5, 30, tzinfo=prelim.issue_time.tzinfo),
        )
        self.assertTrue(resolved.resolved)
        self.assertIsNotNone(resolved.final_report)
        self.assertEqual(resolved.final_report.report_version, 2)
        self.assertEqual(resolved.final_report.max_temp_f, 58)

    def test_preliminary_only_report_does_not_resolve_after_deadline(self) -> None:
        monitor = SettlementRevisionMonitor()
        prelim = parse_cli_climate_report(
            PRELIM_TEXT,
            station=self.station,
            raw_text_payload_id="prelim",
            source_url="https://example.test?version=1",
        )
        monitor.record(prelim)
        unresolved = monitor.resolve_final(
            climate_product_id="CLINYC",
            settlement_date=prelim.local_standard_window_start.date(),
            station=self.station,
            as_of=datetime(2026, 4, 6, 6, 0, tzinfo=prelim.issue_time.tzinfo),
        )
        self.assertFalse(unresolved.resolved)
        self.assertEqual(unresolved.reason, "preliminary_only_past_deadline")


if __name__ == "__main__":
    unittest.main()
