from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.enums import ReportStatus
from kalshi_weather.domain.models import SettlementReportSnapshot, StationReference
from kalshi_weather.settlement import build_ncei_proxy_report
from kalshi_weather.settlement.corpus import (
    build_report_index,
    summarize_proxy_overlap,
    summarize_validation_entries,
    validate_settlement_corpus,
)


class SettlementCorpusTest(unittest.TestCase):
    def _station(self) -> StationReference:
        return StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7"),
            longitude=Decimal("-73.9"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=1,
            grid_y=1,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
            ncei_station_id="USW00094728",
        )

    def test_validate_settlement_corpus(self) -> None:
        station = self._station()
        report = SettlementReportSnapshot(
            station_id=station.station_id,
            climate_product_id=station.climate_product_id,
            issue_time=datetime(2026, 4, 5, 12, tzinfo=timezone.utc),
            report_version=2,
            report_status=ReportStatus.FINALIZED,
            local_standard_window_start=datetime(2026, 4, 4, 1, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 5, 0, 59, tzinfo=timezone.utc),
            max_temp_f=76,
            max_temp_time_local=None,
            min_temp_f=55,
            raw_text_payload_id="p1",
            parser_version="v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "close_time": "2026-04-05T04:59:00Z",
            "result": "yes",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
        }
        entries = validate_settlement_corpus(
            city_id="nyc",
            market_payloads=[market_payload],
            station=station,
            reports=[report],
        )
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].matched)
        summary = summarize_validation_entries(entries)
        self.assertEqual(summary["critical_mismatch_count"], 0)
        self.assertEqual(summary["resolved_validation_count"], 1)
        self.assertEqual(summary["revision_resolved_count"], 1)

    def test_candidate_final_report_is_not_resolved_before_policy_window(self) -> None:
        station = self._station()
        report = SettlementReportSnapshot(
            station_id=station.station_id,
            climate_product_id=station.climate_product_id,
            issue_time=datetime(2026, 4, 5, 1, 30, tzinfo=timezone.utc),
            report_version=2,
            report_status=ReportStatus.CANDIDATE_FINAL,
            local_standard_window_start=datetime(2026, 4, 4, 1, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 5, 0, 59, tzinfo=timezone.utc),
            max_temp_f=76,
            max_temp_time_local=None,
            min_temp_f=55,
            raw_text_payload_id="p1",
            parser_version="v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "close_time": "2026-04-05T04:59:00Z",
            "result": "yes",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
        }
        entries = validate_settlement_corpus(
            city_id="nyc",
            market_payloads=[market_payload],
            station=station,
            reports=[report],
            as_of=datetime(2026, 4, 5, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].revision_resolved)
        self.assertIn("awaiting_revision_resolution", entries[0].notes)
        summary = summarize_validation_entries(entries)
        self.assertEqual(summary["resolved_validation_count"], 0)

    def test_proxy_report_is_used_when_exact_history_is_missing_and_overlap_is_clean(self) -> None:
        station = self._station()
        exact_reports = [
            SettlementReportSnapshot(
                station_id=station.station_id,
                climate_product_id=station.climate_product_id,
                issue_time=datetime(2026, 3, day + 1, 12, tzinfo=timezone.utc),
                report_version=2,
                report_status=ReportStatus.FINALIZED,
                local_standard_window_start=datetime(2026, 3, day, 0, tzinfo=timezone.utc),
                local_standard_window_end=datetime(2026, 3, day, 23, 59, tzinfo=timezone.utc),
                max_temp_f=max_temp,
                max_temp_time_local=None,
                min_temp_f=42,
                raw_text_payload_id=f"exact-{day}",
                parser_version="v1",
                source_kind="NWS_CLI_EXACT",
                source_locator="CLINYC",
            )
            for day, max_temp in ((7, 55), (8, 55), (9, 55), (11, 55), (12, 58))
        ]
        proxy_target = build_ncei_proxy_report(
            station=station,
            row={"DATE": "2026-03-10", "TMAX": "61", "TMIN": "40", "STATION": "USW00094728"},
            source_url="https://example.test/target",
        )
        proxy_reports = [
            build_ncei_proxy_report(
                station=station,
                row={"DATE": f"2026-03-{day:02d}", "TMAX": "55", "TMIN": "39", "STATION": "USW00094728"},
                source_url=f"https://example.test/{day}",
            )
            for day in (7, 8, 9, 11)
        ]
        proxy_reports.append(
            build_ncei_proxy_report(
                station=station,
                row={"DATE": "2026-03-12", "TMAX": "58", "TMIN": "42", "STATION": "USW00094728"},
                source_url="https://example.test/overlap",
            )
        )
        market_payload = {
            "ticker": "KXHIGHNY-26MAR10-T60",
            "close_time": "2026-03-11T03:59:00Z",
            "result": "yes",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for March 10, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 60°, then the market resolves to Yes.",
        }
        entries = validate_settlement_corpus(
            city_id="nyc",
            market_payloads=[market_payload],
            station=station,
            reports=exact_reports,
            proxy_reports=[*proxy_reports, proxy_target],
        )
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].matched)
        self.assertEqual(entries[0].report_source_kind, "NCEI_DAILY_SUMMARIES_PROXY")
        self.assertTrue(entries[0].revision_resolved)
        summary = summarize_validation_entries(entries)
        self.assertEqual(summary["proxy_resolved_count"], 1)
        self.assertEqual(summary["resolved_validation_count"], 1)

    def test_proxy_overlap_mismatch_blocks_proxy_resolution(self) -> None:
        station = self._station()
        exact_reports = [
            SettlementReportSnapshot(
                station_id=station.station_id,
                climate_product_id=station.climate_product_id,
                issue_time=datetime(2026, 3, day + 1, 12, tzinfo=timezone.utc),
                report_version=2,
                report_status=ReportStatus.FINALIZED,
                local_standard_window_start=datetime(2026, 3, day, 0, tzinfo=timezone.utc),
                local_standard_window_end=datetime(2026, 3, day, 23, 59, tzinfo=timezone.utc),
                max_temp_f=max_temp,
                max_temp_time_local=None,
                min_temp_f=42,
                raw_text_payload_id=f"exact-{day}",
                parser_version="v1",
                source_kind="NWS_CLI_EXACT",
                source_locator="CLINYC",
            )
            for day, max_temp in ((7, 55), (8, 55), (9, 55), (10, 55), (11, 55), (12, 58))
        ]
        proxy_reports = [
            build_ncei_proxy_report(
                station=station,
                row={
                    "DATE": f"2026-03-{day:02d}",
                    "TMAX": "57" if day == 12 else "55",
                    "TMIN": "39",
                    "STATION": "USW00094728",
                },
                source_url=f"https://example.test/{day}",
            )
            for day in (7, 8, 9, 10, 11, 12)
        ]
        overlap = summarize_proxy_overlap(exact_reports, proxy_reports)
        self.assertFalse(overlap["proxy_eligible"])

    def test_build_report_index_prefers_later_issue_time_over_higher_version(self) -> None:
        station = self._station()
        earlier_higher_version = SettlementReportSnapshot(
            station_id=station.station_id,
            climate_product_id=station.climate_product_id,
            issue_time=datetime(2026, 4, 2, 1, 0, tzinfo=timezone.utc),
            report_version=21,
            report_status=ReportStatus.PRELIMINARY,
            local_standard_window_start=datetime(2026, 4, 1, 0, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 1, 23, 59, tzinfo=timezone.utc),
            max_temp_f=72,
            max_temp_time_local=None,
            min_temp_f=55,
            raw_text_payload_id="higher-version",
            parser_version="v1",
        )
        later_lower_version = SettlementReportSnapshot(
            station_id=station.station_id,
            climate_product_id=station.climate_product_id,
            issue_time=datetime(2026, 4, 2, 3, 0, tzinfo=timezone.utc),
            report_version=20,
            report_status=ReportStatus.CANDIDATE_FINAL,
            local_standard_window_start=datetime(2026, 4, 1, 0, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 1, 23, 59, tzinfo=timezone.utc),
            max_temp_f=91,
            max_temp_time_local=None,
            min_temp_f=55,
            raw_text_payload_id="later-issue",
            parser_version="v1",
        )
        indexed = build_report_index([earlier_higher_version, later_lower_version])
        self.assertEqual(
            indexed[earlier_higher_version.local_standard_window_start.date()].max_temp_f,
            91,
        )

    def test_preliminary_exact_report_can_fall_back_to_verified_proxy(self) -> None:
        station = self._station()
        overlap_exact = [
            SettlementReportSnapshot(
                station_id=station.station_id,
                climate_product_id=station.climate_product_id,
                issue_time=datetime(2026, 3, day + 1, 5, tzinfo=timezone.utc),
                report_version=2,
                report_status=ReportStatus.CANDIDATE_FINAL,
                local_standard_window_start=datetime(2026, 3, day, 0, tzinfo=timezone.utc),
                local_standard_window_end=datetime(2026, 3, day, 23, 59, tzinfo=timezone.utc),
                max_temp_f=55,
                max_temp_time_local=None,
                min_temp_f=42,
                raw_text_payload_id=f"exact-{day}",
                parser_version="v1",
                source_kind="NWS_CLI_EXACT",
                source_locator="CLINYC",
            )
            for day in (7, 8, 9, 10, 11)
        ]
        unresolved_prelim = SettlementReportSnapshot(
            station_id=station.station_id,
            climate_product_id=station.climate_product_id,
            issue_time=datetime(2026, 3, 13, 1, tzinfo=timezone.utc),
            report_version=99,
            report_status=ReportStatus.PRELIMINARY,
            local_standard_window_start=datetime(2026, 3, 12, 0, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 3, 12, 23, 59, tzinfo=timezone.utc),
            max_temp_f=50,
            max_temp_time_local=None,
            min_temp_f=40,
            raw_text_payload_id="prelim-only",
            parser_version="v1",
            source_kind="NWS_CLI_EXACT",
            source_locator="CLINYC",
        )
        proxy_reports = [
            build_ncei_proxy_report(
                station=station,
                row={
                    "DATE": f"2026-03-{day:02d}",
                    "TMAX": "55" if day != 12 else "61",
                    "TMIN": "39",
                    "STATION": "USW00094728",
                },
                source_url=f"https://example.test/{day}",
            )
            for day in (7, 8, 9, 10, 11, 12)
        ]
        market_payload = {
            "ticker": "KXHIGHNY-26MAR12-T60",
            "close_time": "2026-03-13T03:59:00Z",
            "result": "yes",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for March 12, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 60°, then the market resolves to Yes.",
        }
        entries = validate_settlement_corpus(
            city_id="nyc",
            market_payloads=[market_payload],
            station=station,
            reports=[*overlap_exact, unresolved_prelim],
            proxy_reports=proxy_reports,
            as_of=datetime(2026, 3, 14, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].matched)
        self.assertEqual(entries[0].report_source_kind, "NCEI_DAILY_SUMMARIES_PROXY")
        self.assertEqual(entries[0].revision_resolution_reason, "proxy_fallback_after_preliminary_only")


if __name__ == "__main__":
    unittest.main()
