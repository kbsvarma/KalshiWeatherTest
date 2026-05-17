from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from kalshi_weather.analytics import build_nowcast_validation_report
from kalshi_weather.domain.models import CityProfile, ForecastSnapshot, ObservationSnapshot
from kalshi_weather.engines.nowcast import build_current_state_estimate


class NowcastValidationTest(unittest.TestCase):
    def test_short_cadence_damps_projection(self) -> None:
        base_time = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        observations = [
            ObservationSnapshot(
                station_id="s1",
                event_time=base_time - timedelta(minutes=offset),
                ingest_time=base_time - timedelta(minutes=offset - 1),
                temperature_f=Decimal(str(temp)),
                dewpoint_f=None,
                wind_dir_deg=180,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="CLR",
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id=f"o{offset}",
            )
            for offset, temp in ((0, 70.0), (15, 69.0), (30, 68.0), (45, 67.0))
        ]
        forecast = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=base_time - timedelta(hours=1),
            ingest_time=base_time - timedelta(hours=1),
            valid_for_times=tuple(base_time + timedelta(hours=index) for index in range(3)),
            hourly_temp_path_f=(Decimal("70"), Decimal("74"), Decimal("76")),
            cloud_cover_path_pct=(Decimal("10"),) * 3,
            wind_path=(Decimal("8"),) * 3,
            precipitation_path=(Decimal("0"),) * 3,
            provider_metadata={"station_id": "s1"},
            source_payload_id="f1",
        )
        city_profile = CityProfile(
            city_id="chi",
            display_name="Chicago",
            station_id="s1",
            region_cluster="midwest",
            marine_sensitive_flag=False,
            onshore_wind_sectors=(),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("0"),
            wind_shift_cap_f=Decimal("0"),
            calibration_buckets_version="seed_v1",
        )
        estimate = build_current_state_estimate(
            observations=observations,
            forecast=forecast,
            city_profile=city_profile,
            as_of_time=base_time + timedelta(minutes=3),
        )
        self.assertLess(abs(float(estimate.current_temp_est_f) - 70.0), 0.3)

    def test_build_nowcast_validation_report(self) -> None:
        base_time = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        observations = [
            ObservationSnapshot(
                station_id="s1",
                event_time=base_time + timedelta(hours=offset),
                ingest_time=base_time + timedelta(hours=offset, minutes=2),
                temperature_f=Decimal(str(temp)),
                dewpoint_f=None,
                wind_dir_deg=180,
                wind_speed_kt=Decimal("5"),
                sky_cover_code="CLR",
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id=f"o{offset}",
            )
            for offset, temp in enumerate((60, 62, 64, 66, 68))
        ]
        forecast = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=base_time,
            ingest_time=base_time,
            valid_for_times=tuple(base_time + timedelta(hours=index) for index in range(6)),
            hourly_temp_path_f=tuple(Decimal(str(value)) for value in (60, 62, 64, 66, 68, 69)),
            cloud_cover_path_pct=(Decimal("10"),) * 6,
            wind_path=(Decimal("5"),) * 6,
            precipitation_path=(Decimal("0"),) * 6,
            provider_metadata={"station_id": "s1"},
            source_payload_id="f1",
        )
        city_profile = CityProfile(
            city_id="nyc",
            display_name="NYC",
            station_id="s1",
            region_cluster="northeast",
            marine_sensitive_flag=False,
            onshore_wind_sectors=(),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("0"),
            wind_shift_cap_f=Decimal("0"),
            calibration_buckets_version="seed_v1",
        )
        report = build_nowcast_validation_report(observations, [forecast], city_profile)
        self.assertEqual(report["sample_count"], 3)
        self.assertIn("beats_baselines", report)

    def test_validation_does_not_require_future_forecast(self) -> None:
        base_time = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        observations = [
            ObservationSnapshot(
                station_id="s1",
                event_time=base_time + timedelta(hours=offset),
                ingest_time=base_time + timedelta(hours=offset, minutes=2),
                temperature_f=Decimal(str(temp)),
                dewpoint_f=None,
                wind_dir_deg=180,
                wind_speed_kt=Decimal("5"),
                sky_cover_code="CLR",
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id=f"o{offset}",
            )
            for offset, temp in enumerate((60, 62, 64, 66, 68))
        ]
        future_forecast = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=base_time + timedelta(days=1),
            ingest_time=base_time + timedelta(days=1),
            valid_for_times=tuple(base_time + timedelta(hours=index) for index in range(6)),
            hourly_temp_path_f=tuple(Decimal(str(value)) for value in (60, 62, 64, 66, 68, 69)),
            cloud_cover_path_pct=(Decimal("10"),) * 6,
            wind_path=(Decimal("5"),) * 6,
            precipitation_path=(Decimal("0"),) * 6,
            provider_metadata={"station_id": "s1"},
            source_payload_id="f_future",
        )
        city_profile = CityProfile(
            city_id="nyc",
            display_name="NYC",
            station_id="s1",
            region_cluster="northeast",
            marine_sensitive_flag=False,
            onshore_wind_sectors=(),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("0"),
            wind_shift_cap_f=Decimal("0"),
            calibration_buckets_version="seed_v1",
        )
        report = build_nowcast_validation_report(observations, [future_forecast], city_profile)
        self.assertEqual(report["sample_count"], 3)

    def test_short_cadence_outlier_does_not_use_pure_carry_forward(self) -> None:
        base_time = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        observations = [
            ObservationSnapshot(
                station_id="s1",
                event_time=base_time - timedelta(minutes=offset),
                ingest_time=base_time - timedelta(minutes=offset - 1),
                temperature_f=Decimal(str(temp)),
                dewpoint_f=None,
                wind_dir_deg=180,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="CLR",
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id=f"o{offset}",
            )
            for offset, temp in ((0, 85.0), (15, 70.0), (30, 69.0), (45, 68.0))
        ]
        forecast = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=base_time - timedelta(hours=1),
            ingest_time=base_time - timedelta(hours=1),
            valid_for_times=tuple(base_time + timedelta(hours=index) for index in range(3)),
            hourly_temp_path_f=(Decimal("70"), Decimal("71"), Decimal("72")),
            cloud_cover_path_pct=(Decimal("10"),) * 3,
            wind_path=(Decimal("8"),) * 3,
            precipitation_path=(Decimal("0"),) * 3,
            provider_metadata={"station_id": "s1"},
            source_payload_id="f1",
        )
        city_profile = CityProfile(
            city_id="chi",
            display_name="Chicago",
            station_id="s1",
            region_cluster="midwest",
            marine_sensitive_flag=False,
            onshore_wind_sectors=(),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("0"),
            wind_shift_cap_f=Decimal("0"),
            calibration_buckets_version="seed_v1",
        )
        estimate = build_current_state_estimate(
            observations=observations,
            forecast=forecast,
            city_profile=city_profile,
            as_of_time=base_time + timedelta(minutes=3),
        )
        self.assertLess(estimate.current_temp_est_f, Decimal("80"))
        self.assertTrue(estimate.discontinuity_suspected)

    def test_station_timezone_uses_local_heating_window(self) -> None:
        as_of_time = datetime(2026, 4, 6, 18, 0, tzinfo=timezone.utc)
        observations = [
            ObservationSnapshot(
                station_id="s1",
                event_time=as_of_time - timedelta(minutes=15 * idx),
                ingest_time=as_of_time - timedelta(minutes=(15 * idx) - 1),
                temperature_f=Decimal(str(temp)),
                dewpoint_f=None,
                wind_dir_deg=180,
                wind_speed_kt=Decimal("7"),
                sky_cover_code="CLR",
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id=f"tz{idx}",
            )
            for idx, temp in enumerate((70, 69, 68, 67))
        ]
        forecast = ForecastSnapshot(
            provider_id="NWS",
            provider_run_time=as_of_time - timedelta(hours=1),
            ingest_time=as_of_time - timedelta(hours=1),
            valid_for_times=tuple(as_of_time + timedelta(hours=index) for index in range(3)),
            hourly_temp_path_f=(Decimal("70"), Decimal("73"), Decimal("75")),
            cloud_cover_path_pct=(Decimal("10"),) * 3,
            wind_path=(Decimal("7"),) * 3,
            precipitation_path=(Decimal("0"),) * 3,
            provider_metadata={"station_id": "s1"},
            source_payload_id="f-tz",
        )
        city_profile = CityProfile(
            city_id="nyc",
            display_name="NYC",
            station_id="s1",
            region_cluster="northeast",
            marine_sensitive_flag=False,
            onshore_wind_sectors=(),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("0"),
            wind_shift_cap_f=Decimal("0"),
            calibration_buckets_version="seed_v1",
        )

        estimate = build_current_state_estimate(
            observations=observations,
            forecast=forecast,
            city_profile=city_profile,
            as_of_time=as_of_time,
            station_timezone="America/New_York",
        )

        self.assertGreater(estimate.near_term_slope_f_per_hr, Decimal("0.50"))


if __name__ == "__main__":
    unittest.main()
