from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.enums import SettlementValidationStatus
from kalshi_weather.domain.models import CityProfile, ForecastSnapshot, SettlementRule
from kalshi_weather.engines.forecast import _threshold_skew_sigmas, build_forecast_distribution


class ForecastEngineTest(unittest.TestCase):
    def test_threshold_skew_direction_flips_when_forecast_is_below_threshold(self) -> None:
        sigma_down_hot, sigma_up_hot = _threshold_skew_sigmas(2.0, 1.0)
        sigma_down_cool, sigma_up_cool = _threshold_skew_sigmas(2.0, -1.0)
        self.assertGreater(sigma_down_hot, sigma_up_hot)
        self.assertGreater(sigma_up_cool, sigma_down_cool)

    def test_unsupported_provider_is_explicitly_marked_and_downweighted(self) -> None:
        as_of = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        settlement_rule = SettlementRule(
            settlement_rule_id="rule-1",
            market_ticker="M1",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("75"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli",
            local_standard_window_start=datetime(2026, 4, 6, 5, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 7, 4, 59, tzinfo=timezone.utc),
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        snapshots = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=as_of,
                ingest_time=as_of,
                valid_for_times=(as_of, as_of.replace(hour=13)),
                hourly_temp_path_f=(Decimal("72"), Decimal("75")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("25")),
                wind_path=(Decimal("8"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="nws-1",
            ),
            ForecastSnapshot(
                provider_id="HRRR",
                provider_run_time=as_of,
                ingest_time=as_of,
                valid_for_times=(as_of, as_of.replace(hour=13)),
                hourly_temp_path_f=(Decimal("73"), Decimal("76")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("25")),
                wind_path=(Decimal("8"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="hrrr-1",
            ),
        ]
        result = build_forecast_distribution(
            snapshots=snapshots,
            settlement_rule=settlement_rule,
            city_profile=city,
            as_of_time=as_of,
        )
        self.assertEqual(result.provider_support_status["NWS"], "mvp_supported")
        self.assertEqual(result.provider_support_status["HRRR"], "unsupported_fallback")
        self.assertLess(result.provider_weights["HRRR"], result.provider_weights["NWS"])
        self.assertGreater(result.provider_spread_f, Decimal("0"))

    def test_empirical_bias_adjustment_is_applied_to_provider_maximum(self) -> None:
        as_of = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        settlement_rule = SettlementRule(
            settlement_rule_id="rule-1",
            market_ticker="M1",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("75"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli",
            local_standard_window_start=datetime(2026, 4, 6, 5, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 7, 4, 59, tzinfo=timezone.utc),
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        snapshot = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=as_of,
            ingest_time=as_of,
            valid_for_times=(as_of, as_of.replace(hour=13)),
            hourly_temp_path_f=(Decimal("72"), Decimal("75")),
            cloud_cover_path_pct=(Decimal("20"), Decimal("25")),
            wind_path=(Decimal("8"), Decimal("9")),
            precipitation_path=(Decimal("0"), Decimal("0")),
            provider_metadata={"station_id": "nyc-central-park"},
            source_payload_id="nws-1",
        )
        unbiased = build_forecast_distribution(
            snapshots=[snapshot],
            settlement_rule=settlement_rule,
            city_profile=city,
            as_of_time=as_of,
        )
        biased = build_forecast_distribution(
            snapshots=[snapshot],
            settlement_rule=settlement_rule,
            city_profile=city,
            as_of_time=as_of,
            provider_bias_adjustments={"NWS": Decimal("1.0")},
        )
        self.assertLess(biased.provider_maxima_f["NWS"], unbiased.provider_maxima_f["NWS"])
        self.assertEqual(biased.provider_bias_adjustments_f["NWS"], Decimal("1.0"))

    def test_support_buffer_expands_with_city_volatility(self) -> None:
        as_of = datetime(2026, 4, 6, 0, tzinfo=timezone.utc)
        settlement_rule = SettlementRule(
            settlement_rule_id="rule-1",
            market_ticker="M1",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("75"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli",
            local_standard_window_start=datetime(2026, 4, 6, 5, tzinfo=timezone.utc),
            local_standard_window_end=datetime(2026, 4, 7, 4, 59, tzinfo=timezone.utc),
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
        )
        city = CityProfile(
            city_id="den",
            display_name="Denver",
            station_id="denver",
            region_cluster="front_range",
            marine_sensitive_flag=False,
            onshore_wind_sectors=(),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("0"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        snapshot = ForecastSnapshot(
            provider_id="HRRR",
            provider_run_time=as_of,
            ingest_time=as_of,
            valid_for_times=(as_of, as_of.replace(hour=13)),
            hourly_temp_path_f=(Decimal("72"), Decimal("75")),
            cloud_cover_path_pct=(Decimal("20"), Decimal("25")),
            wind_path=(Decimal("8"), Decimal("9")),
            precipitation_path=(Decimal("0"), Decimal("0")),
            provider_metadata={"station_id": "denver"},
            source_payload_id="hrrr-1",
        )
        result = build_forecast_distribution(
            snapshots=[snapshot],
            settlement_rule=settlement_rule,
            city_profile=city,
            as_of_time=as_of,
        )
        self.assertLessEqual(min(result.distribution.support_temps_f), 65)
        self.assertGreaterEqual(max(result.distribution.support_temps_f), 85)


if __name__ == "__main__":
    unittest.main()
