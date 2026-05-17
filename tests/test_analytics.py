from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from kalshi_weather.analytics import (
    NYC_MVP_LIVE_PROFILE,
    build_drift_report,
    build_live_gate_report,
    build_provider_reliability_report,
    build_sensitivity_matrix,
    build_shadow_report,
    extract_provider_bias_adjustments,
    extract_provider_reliability_weights,
)
from kalshi_weather.analytics.forecast_calibration import _normalized_weight_map
from kalshi_weather.domain.models import ForecastSnapshot, ObservationSnapshot, StationReference


DECISIONS = [
    {
        "schema_version": "1.0.0",
        "final_decision": "TAKER_ALLOWED",
        "data_freshness": {"observation_lag_minutes": 5, "observation_excess_lag_minutes": 0},
        "edge_summary": {
            "selected_executable_ev": "0.05",
            "selected_friction_to_edge_ratio": "0.4",
        },
    },
    {
        "schema_version": "1.0.0",
        "final_decision": "WATCH",
        "data_freshness": {"observation_lag_minutes": 10, "observation_excess_lag_minutes": 0},
        "edge_summary": {},
    },
]

FILLS = [{"shadow_fill_id": "1"}]
POSITIONS = [{"city_id": "nyc", "lifecycle_status": "CLOSED", "settled_pnl_dollars": "0.12"}]


class AnalyticsTest(unittest.TestCase):
    def test_shadow_report(self) -> None:
        report = build_shadow_report(
            DECISIONS,
            [
                {
                    "shadow_fill_id": "1",
                    "reconciliation_status": "SETTLED",
                    "predicted_executable_ev_total": "0.05",
                }
            ],
            position_payloads=POSITIONS,
        )
        self.assertEqual(report["shadow_fill_count"], 1)
        self.assertEqual(report["taker_allowed_count"], 1)
        self.assertEqual(report["settled_position_count"], 1)
        self.assertEqual(report["settled_win_rate"], 1.0)
        self.assertAlmostEqual(report["total_settled_calibration_residual_dollars"], 0.07, places=6)

    def test_sensitivity_matrix(self) -> None:
        report = build_sensitivity_matrix(DECISIONS)
        self.assertTrue(report["scenarios"])
        self.assertIn("parameters", report)

    def test_drift_report(self) -> None:
        report = build_drift_report(DECISIONS, fill_payloads=FILLS)
        self.assertEqual(report["decision_distribution"]["TAKER_ALLOWED"], 1)

    def test_live_gate_report(self) -> None:
        report = build_live_gate_report(
            DECISIONS,
            FILLS,
            position_payloads=POSITIONS,
            settlement_summary={"eligible_for_shadow_only": False},
            nowcast_report={"beats_baselines": False},
            calibration_report={"sample_sufficient": False},
        )
        self.assertIn("all_passed", report)
        self.assertFalse(report["gates"]["forecast_calibration_sample_sufficient"])

    def test_live_gate_report_nyc_mvp_profile(self) -> None:
        decision_payloads = [
            {
                "schema_version": "1.0.0",
                "city_id": "nyc",
                "final_decision": "TAKER_ALLOWED" if index < 15 else "WATCH",
                "data_freshness": {
                    "observation_lag_minutes": 8,
                    "observation_excess_lag_minutes": 0,
                },
                "edge_summary": {
                    "selected_executable_ev": "0.08",
                    "selected_friction_to_edge_ratio": "0.4",
                },
            }
            for index in range(75)
        ]
        positions = [
            {
                "city_id": "nyc",
                "lifecycle_status": "CLOSED",
                "settled_pnl_dollars": "0.12" if index < 18 else "-0.05",
            }
            for index in range(25)
        ]
        report = build_live_gate_report(
            decision_payloads,
            [],
            position_payloads=positions,
            settlement_summary={
                "resolved_validation_count": 50,
                "matched_market_count": 50,
                "critical_mismatch_count": 0,
                "revision_resolved_count": 50,
                "unresolved_entry_count": 0,
            },
            nowcast_report={
                "beats_baselines": True,
                "sample_sufficient": True,
            },
            calibration_report={
                "global_sample_count": 10,
                "global_unique_run_count": 2,
                "global_mean_abs_error_f": 0.8,
            },
            profile=NYC_MVP_LIVE_PROFILE,
        )
        self.assertEqual(report["profile"], NYC_MVP_LIVE_PROFILE)
        self.assertTrue(report["gates"]["settlement_validation_ready"])
        self.assertTrue(report["gates"]["forecast_calibration_sample_sufficient"])

    def test_provider_reliability_report_and_weight_extraction(self) -> None:
        base = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
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
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=base,
                ingest_time=base,
                valid_for_times=(base, base + timedelta(hours=1)),
                hourly_temp_path_f=(Decimal("71"), Decimal("74")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("25")),
                wind_path=(Decimal("6"), Decimal("7")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": station.station_id},
                source_payload_id="f1",
            ),
            ForecastSnapshot(
                provider_id="NWS_GRID",
                provider_run_time=base,
                ingest_time=base,
                valid_for_times=(base, base + timedelta(hours=1)),
                hourly_temp_path_f=(Decimal("70"), Decimal("72")),
                cloud_cover_path_pct=(Decimal("30"), Decimal("35")),
                wind_path=(Decimal("5"), Decimal("6")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": station.station_id},
                source_payload_id="f2",
            ),
        ]
        observations = [
            ObservationSnapshot(
                station_id=station.station_id,
                event_time=base,
                ingest_time=base,
                temperature_f=Decimal("73"),
                dewpoint_f=Decimal("50"),
                wind_dir_deg=180,
                wind_speed_kt=Decimal("5"),
                sky_cover_code="FEW",
                ceiling_ft=5000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="o1",
            ),
            ObservationSnapshot(
                station_id=station.station_id,
                event_time=base + timedelta(minutes=15),
                ingest_time=base + timedelta(minutes=15),
                temperature_f=Decimal("72"),
                dewpoint_f=Decimal("49"),
                wind_dir_deg=185,
                wind_speed_kt=Decimal("5"),
                sky_cover_code="FEW",
                ceiling_ft=5200,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="o2",
            ),
            ObservationSnapshot(
                station_id=station.station_id,
                event_time=base + timedelta(minutes=30),
                ingest_time=base + timedelta(minutes=30),
                temperature_f=Decimal("71"),
                dewpoint_f=Decimal("48"),
                wind_dir_deg=190,
                wind_speed_kt=Decimal("6"),
                sky_cover_code="SCT",
                ceiling_ft=5300,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="o3",
            ),
            ObservationSnapshot(
                station_id=station.station_id,
                event_time=base + timedelta(minutes=45),
                ingest_time=base + timedelta(minutes=45),
                temperature_f=Decimal("70"),
                dewpoint_f=Decimal("47"),
                wind_dir_deg=195,
                wind_speed_kt=Decimal("6"),
                sky_cover_code="BKN",
                ceiling_ft=5400,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="o4",
            ),
        ]

        report = build_provider_reliability_report(forecasts, observations, station)
        weights = extract_provider_reliability_weights(report)
        stratified_weights = extract_provider_reliability_weights(report, season_key="MAM", lead_hours=1.0)
        bias_adjustments = extract_provider_bias_adjustments(report, season_key="MAM", lead_hours=1.0)

        self.assertEqual(report["station_id"], station.station_id)
        self.assertIn("NWS", report["provider_reports"])
        self.assertIn("NWS_GRID", weights)
        self.assertGreater(weights["NWS"], Decimal("0"))
        self.assertIn("MAM", report["provider_reports_by_season"])
        self.assertIn("lt_6h", report["provider_reports_by_season_lead_bucket"]["MAM"])
        self.assertIn("NWS", stratified_weights)
        self.assertIn("NWS", bias_adjustments)

    def test_day_max_error_contributes_to_seasonal_and_lead_strata(self) -> None:
        base = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
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
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=base,
                ingest_time=base,
                valid_for_times=(base + timedelta(hours=10), base + timedelta(hours=11)),
                hourly_temp_path_f=(Decimal("74"), Decimal("76")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("20")),
                wind_path=(Decimal("5"), Decimal("5")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": station.station_id},
                source_payload_id="f1",
            )
        ]
        observations = [
            ObservationSnapshot(
                station_id=station.station_id,
                event_time=base + timedelta(hours=9),
                ingest_time=base + timedelta(hours=9),
                temperature_f=Decimal("75"),
                dewpoint_f=Decimal("50"),
                wind_dir_deg=180,
                wind_speed_kt=Decimal("5"),
                sky_cover_code="FEW",
                ceiling_ft=5000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="o1",
            )
        ]

        report = build_provider_reliability_report(forecasts, observations, station)
        season_report = report["provider_reports_by_season"]["MAM"]["NWS"]
        lead_report = report["provider_reports_by_season_lead_bucket"]["MAM"]["six_to_12h"]["NWS"]

        self.assertEqual(season_report["sample_count"], 1)
        self.assertEqual(season_report["point_sample_count"], 0)
        self.assertEqual(lead_report["sample_count"], 1)
        self.assertEqual(lead_report["point_sample_count"], 0)

    def test_provider_weight_shrinkage_pulls_small_samples_toward_prior(self) -> None:
        weights = _normalized_weight_map(
            {
                "NWS": {
                    "sample_count": 4,
                    "mean_abs_error_f": 0.1,
                },
                "NWS_GRID": {
                    "sample_count": 40,
                    "mean_abs_error_f": 3.0,
                },
            }
        )
        self.assertLess(Decimal(weights["NWS"]), Decimal("0.80"))
        self.assertGreater(Decimal(weights["NWS"]), Decimal("0.55"))
        self.assertLess(Decimal(weights["NWS_GRID"]), Decimal(weights["NWS"]))


if __name__ == "__main__":
    unittest.main()
