from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.enums import ReportStatus, SettlementValidationStatus
from kalshi_weather.domain.models import SettlementReportSnapshot, SettlementRule
from kalshi_weather.settlement.validation import validate_market_against_report


class SettlementValidationTest(unittest.TestCase):
    def test_validate_market_against_report(self) -> None:
        rule = SettlementRule(
            settlement_rule_id="rule-1",
            market_ticker="KXHIGHNY-26APR04-T75",
            settlement_variable="daily_high_temperature_f",
            operator=">",
            threshold_f=Decimal("75"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="NWS_DAILY_CLIMATE_REPORT",
            source_locator="CLINYC",
            local_standard_window_start=datetime(2026, 4, 4, 1, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 5, 0, 59, 59, tzinfo=timezone.utc),
            parser_version="v1",
            validation_status=SettlementValidationStatus.PENDING,
        )
        report = SettlementReportSnapshot(
            station_id="nyc-central-park",
            climate_product_id="CLINYC",
            issue_time=datetime(2026, 4, 5, 3, tzinfo=timezone.utc),
            report_version=2,
            report_status=ReportStatus.CANDIDATE_FINAL,
            local_standard_window_start=datetime(2026, 4, 4, 1, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 5, 0, 59, 59, tzinfo=timezone.utc),
            max_temp_f=76,
            max_temp_time_local=datetime(2026, 4, 4, 18, tzinfo=timezone.utc),
            min_temp_f=42,
            raw_text_payload_id="raw-1",
            parser_version="cli_v1",
        )
        result = validate_market_against_report(
            {"ticker": "KXHIGHNY-26APR04-T75", "result": "yes"},
            rule,
            report,
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.expected_result, "yes")
        self.assertEqual(result.actual_result, "yes")


if __name__ == "__main__":
    unittest.main()
