from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from kalshi_weather.domain.enums import SettlementValidationStatus
from kalshi_weather.domain.models import (
    CityProfile,
    CurrentStateEstimate,
    ForecastDistribution,
    ObservationSnapshot,
    SettlementRule,
)
from kalshi_weather.engines.path import apply_path_adjustment


class PathEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of_time = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        self.city = CityProfile(
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
        self.rule = SettlementRule(
            settlement_rule_id="rule1",
            market_ticker="M1",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("75"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli:nyc",
            local_standard_window_start=self.as_of_time,
            local_standard_window_end=self.as_of_time,
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
            ambiguity_flags=(),
        )

    def test_fractional_current_high_removes_lower_integer_support(self) -> None:
        distribution = ForecastDistribution(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            support_temps_f=(72, 73, 74),
            pmf=(Decimal("0.2"), Decimal("0.3"), Decimal("0.5")),
            provider_weights={"NWS": Decimal("1")},
            base_entropy=Decimal("0.5"),
            sigma_equivalent_f=Decimal("1.0"),
            calibration_version="v1",
            conditioned_flag=False,
        )
        current_state = CurrentStateEstimate(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            current_temp_est_f=Decimal("72.8"),
            current_temp_sigma_f=Decimal("1.0"),
            near_term_slope_f_per_hr=Decimal("0.5"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=self.as_of_time,
                ingest_time=self.as_of_time,
                temperature_f=Decimal("72.8"),
                dewpoint_f=None,
                wind_dir_deg=None,
                wind_speed_kt=None,
                sky_cover_code=None,
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id="obs1",
            )
        ]
        result = apply_path_adjustment(
            distribution=distribution,
            current_state=current_state,
            observations=observations,
            settlement_rule=self.rule,
            city_profile=self.city,
            as_of_time=self.as_of_time,
        )
        self.assertEqual(result.conditioned_distribution.pmf[0], Decimal("0"))

    def test_exact_integer_current_high_keeps_matching_integer_support(self) -> None:
        distribution = ForecastDistribution(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            support_temps_f=(72, 73),
            pmf=(Decimal("0.4"), Decimal("0.6")),
            provider_weights={"NWS": Decimal("1")},
            base_entropy=Decimal("0.5"),
            sigma_equivalent_f=Decimal("1.0"),
            calibration_version="v1",
            conditioned_flag=False,
        )
        current_state = CurrentStateEstimate(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            current_temp_est_f=Decimal("72.0"),
            current_temp_sigma_f=Decimal("1.0"),
            near_term_slope_f_per_hr=Decimal("0.5"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=self.as_of_time,
                ingest_time=self.as_of_time,
                temperature_f=Decimal("72.0"),
                dewpoint_f=None,
                wind_dir_deg=None,
                wind_speed_kt=None,
                sky_cover_code=None,
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id="obs1",
            )
        ]
        result = apply_path_adjustment(
            distribution=distribution,
            current_state=current_state,
            observations=observations,
            settlement_rule=self.rule,
            city_profile=self.city,
            as_of_time=self.as_of_time,
        )
        self.assertGreater(result.conditioned_distribution.pmf[0], Decimal("0"))

    def test_residual_gain_uses_conditional_tail_not_support_max(self) -> None:
        distribution = ForecastDistribution(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            support_temps_f=(70, 71, 72, 73, 80),
            pmf=(
                Decimal("0.40"),
                Decimal("0.30"),
                Decimal("0.20"),
                Decimal("0.05"),
                Decimal("0.05"),
            ),
            provider_weights={"NWS": Decimal("1")},
            base_entropy=Decimal("0.5"),
            sigma_equivalent_f=Decimal("1.0"),
            calibration_version="v1",
            conditioned_flag=False,
        )
        current_state = CurrentStateEstimate(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            current_temp_est_f=Decimal("72"),
            current_temp_sigma_f=Decimal("1.0"),
            near_term_slope_f_per_hr=Decimal("0.5"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=self.as_of_time,
                ingest_time=self.as_of_time,
                temperature_f=Decimal("72"),
                dewpoint_f=None,
                wind_dir_deg=None,
                wind_speed_kt=None,
                sky_cover_code=None,
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id="obs1",
            )
        ]
        result = apply_path_adjustment(
            distribution=distribution,
            current_state=current_state,
            observations=observations,
            settlement_rule=self.rule,
            city_profile=self.city,
            as_of_time=self.as_of_time,
        )
        self.assertEqual(result.path_state.residual_gain_mean_f, Decimal("4.5"))
        self.assertEqual(result.path_state.residual_gain_p80_f, Decimal("8"))

    def test_fractional_threshold_does_not_truncate_support_cutoff(self) -> None:
        fractional_rule = SettlementRule(
            settlement_rule_id="rule-fractional",
            market_ticker="M2",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("72.5"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli:nyc",
            local_standard_window_start=self.as_of_time,
            local_standard_window_end=self.as_of_time,
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
            ambiguity_flags=(),
        )
        distribution = ForecastDistribution(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            support_temps_f=(72, 73),
            pmf=(Decimal("0.5"), Decimal("0.5")),
            provider_weights={"NWS": Decimal("1")},
            base_entropy=Decimal("0.5"),
            sigma_equivalent_f=Decimal("1.0"),
            calibration_version="v1",
            conditioned_flag=False,
        )
        current_state = CurrentStateEstimate(
            as_of_time=self.as_of_time,
            station_id="nyc-central-park",
            current_temp_est_f=Decimal("71.0"),
            current_temp_sigma_f=Decimal("1.0"),
            near_term_slope_f_per_hr=Decimal("0.5"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=self.as_of_time,
                ingest_time=self.as_of_time,
                temperature_f=Decimal("71.0"),
                dewpoint_f=None,
                wind_dir_deg=None,
                wind_speed_kt=None,
                sky_cover_code=None,
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id="obs1",
            )
        ]

        result = apply_path_adjustment(
            distribution=distribution,
            current_state=current_state,
            observations=observations,
            settlement_rule=fractional_rule,
            city_profile=self.city,
            as_of_time=self.as_of_time,
        )

        self.assertGreater(result.conditioned_distribution.pmf[0], Decimal("0.5"))

    def test_remaining_window_uses_local_timezone_not_utc(self) -> None:
        as_of_time = datetime(2026, 4, 6, 18, 0, tzinfo=timezone.utc)
        local_start = datetime(2026, 4, 6, 1, 0, tzinfo=ZoneInfo("America/New_York"))
        local_end = datetime(2026, 4, 7, 0, 59, tzinfo=ZoneInfo("America/New_York"))
        rule = SettlementRule(
            settlement_rule_id="rule-local",
            market_ticker="M3",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("75"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli:nyc",
            local_standard_window_start=local_start,
            local_standard_window_end=local_end,
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
            ambiguity_flags=(),
        )
        distribution = ForecastDistribution(
            as_of_time=as_of_time,
            station_id="nyc-central-park",
            support_temps_f=(72, 73, 74, 75, 76),
            pmf=(Decimal("0.1"), Decimal("0.2"), Decimal("0.2"), Decimal("0.2"), Decimal("0.3")),
            provider_weights={"NWS": Decimal("1")},
            base_entropy=Decimal("0.5"),
            sigma_equivalent_f=Decimal("1.0"),
            calibration_version="v1",
            conditioned_flag=False,
        )
        current_state = CurrentStateEstimate(
            as_of_time=as_of_time,
            station_id="nyc-central-park",
            current_temp_est_f=Decimal("73"),
            current_temp_sigma_f=Decimal("1.0"),
            near_term_slope_f_per_hr=Decimal("1.0"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=as_of_time,
                ingest_time=as_of_time,
                temperature_f=Decimal("73"),
                dewpoint_f=None,
                wind_dir_deg=None,
                wind_speed_kt=None,
                sky_cover_code=None,
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id="obs-local",
            )
        ]

        result = apply_path_adjustment(
            distribution=distribution,
            current_state=current_state,
            observations=observations,
            settlement_rule=rule,
            city_profile=self.city,
            as_of_time=as_of_time,
        )

        self.assertEqual(result.path_state.remaining_effective_window_minutes, 180)

    def test_tomorrow_market_is_not_treated_as_late_day_today(self) -> None:
        as_of_time = datetime(2026, 4, 9, 0, 35, tzinfo=timezone.utc)
        local_start = datetime(2026, 4, 9, 1, 0, tzinfo=ZoneInfo("America/New_York"))
        local_end = datetime(2026, 4, 10, 0, 59, tzinfo=ZoneInfo("America/New_York"))
        rule = SettlementRule(
            settlement_rule_id="rule-tomorrow",
            market_ticker="KXHIGHNY-26APR09-T60",
            settlement_variable="daily_high_temp_f",
            operator=">",
            threshold_f=Decimal("60"),
            inclusive_flag=False,
            station_id="nyc-central-park",
            source_kind="nws_cli",
            source_locator="cli:nyc",
            local_standard_window_start=local_start,
            local_standard_window_end=local_end,
            parser_version="v1",
            validation_status=SettlementValidationStatus.VALIDATED,
            ambiguity_flags=(),
        )
        distribution = ForecastDistribution(
            as_of_time=as_of_time,
            station_id="nyc-central-park",
            support_temps_f=(50, 55, 60, 65, 70),
            pmf=(Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.25"), Decimal("0.15")),
            provider_weights={"NWS": Decimal("1")},
            base_entropy=Decimal("0.5"),
            sigma_equivalent_f=Decimal("3.0"),
            calibration_version="v1",
            conditioned_flag=False,
        )
        current_state = CurrentStateEstimate(
            as_of_time=as_of_time,
            station_id="nyc-central-park",
            current_temp_est_f=Decimal("72"),
            current_temp_sigma_f=Decimal("1.0"),
            near_term_slope_f_per_hr=Decimal("-1.0"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=as_of_time,
                ingest_time=as_of_time,
                temperature_f=Decimal("72"),
                dewpoint_f=None,
                wind_dir_deg=None,
                wind_speed_kt=None,
                sky_cover_code=None,
                ceiling_ft=None,
                visibility_mi=None,
                weather_codes=(),
                quality_flags=(),
                source_payload_id="obs-prior-evening",
            )
        ]

        result = apply_path_adjustment(
            distribution=distribution,
            current_state=current_state,
            observations=observations,
            settlement_rule=rule,
            city_profile=self.city,
            as_of_time=as_of_time,
        )

        self.assertEqual(result.path_state.remaining_effective_window_minutes, 1225)
        self.assertFalse(result.path_state.threshold_already_crossed_flag)
        self.assertEqual(result.path_state.late_day_decay_factor, Decimal("1"))
        self.assertEqual(result.conditioned_distribution.pmf, distribution.pmf)


if __name__ == "__main__":
    unittest.main()
