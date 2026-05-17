from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.models import CurrentStateEstimate, PathProgressState
from kalshi_weather.engines.regime import build_regime_assessment


class RegimeAssessmentTest(unittest.TestCase):
    def test_convective_shock_haircut_scales_with_shock_risk(self) -> None:
        as_of_time = datetime(2026, 4, 6, 18, tzinfo=timezone.utc)
        path_state = PathProgressState(
            as_of_time=as_of_time,
            current_high_so_far_f=Decimal("72"),
            current_temp_f=Decimal("72"),
            threshold_gap_f=Decimal("2"),
            remaining_effective_window_minutes=150,
            estimated_intraday_slope_f_per_hr=Decimal("0.5"),
            solar_insolation_vector={"daylight_weight": Decimal("1"), "minutes_to_peak": Decimal("30")},
            thermal_ceiling_estimate_f=Decimal("76"),
            residual_gain_mean_f=Decimal("2"),
            residual_gain_p80_f=Decimal("3"),
            reachability_score=Decimal("0.6"),
            late_day_decay_factor=Decimal("0.85"),
            path_uncertainty_addon=Decimal("0.10"),
            threshold_already_crossed_flag=False,
        )
        low_shock = build_regime_assessment(
            city_id="mia",
            current_state=CurrentStateEstimate(
                as_of_time=as_of_time,
                station_id="s1",
                current_temp_est_f=Decimal("72"),
                current_temp_sigma_f=Decimal("1"),
                near_term_slope_f_per_hr=Decimal("0"),
                observation_lag_minutes=5,
                expected_observation_cadence_minutes=15,
                observation_excess_lag_minutes=0,
                observation_lag_penalty=Decimal("0"),
                shock_risk_score=Decimal("0.55"),
                cloud_cover_shock_adjustment_f=Decimal("-1"),
                storm_shock_adjustment_f=Decimal("-1"),
                marine_intrusion_adjustment_f=Decimal("0"),
                wind_shift_adjustment_f=Decimal("0"),
                discontinuity_suspected=True,
                confidence_downgrade=Decimal("0.1"),
                provenance_refs=(),
            ),
            path_state=path_state,
            as_of_time=as_of_time,
        )
        high_shock = build_regime_assessment(
            city_id="mia",
            current_state=CurrentStateEstimate(
                as_of_time=as_of_time,
                station_id="s1",
                current_temp_est_f=Decimal("72"),
                current_temp_sigma_f=Decimal("1"),
                near_term_slope_f_per_hr=Decimal("0"),
                observation_lag_minutes=5,
                expected_observation_cadence_minutes=15,
                observation_excess_lag_minutes=0,
                observation_lag_penalty=Decimal("0"),
                shock_risk_score=Decimal("0.95"),
                cloud_cover_shock_adjustment_f=Decimal("-2"),
                storm_shock_adjustment_f=Decimal("-2"),
                marine_intrusion_adjustment_f=Decimal("0"),
                wind_shift_adjustment_f=Decimal("0"),
                discontinuity_suspected=True,
                confidence_downgrade=Decimal("0.2"),
                provenance_refs=(),
            ),
            path_state=path_state,
            as_of_time=as_of_time,
        )
        self.assertEqual(low_shock.active_regime, "CONVECTIVE_SHOCK")
        self.assertGreater(high_shock.haircut_value, low_shock.haircut_value)

    def test_late_day_decay_gradates_before_hard_block(self) -> None:
        as_of_time = datetime(2026, 4, 6, 18, tzinfo=timezone.utc)
        current_state = CurrentStateEstimate(
            as_of_time=as_of_time,
            station_id="s1",
            current_temp_est_f=Decimal("70"),
            current_temp_sigma_f=Decimal("1"),
            near_term_slope_f_per_hr=Decimal("0.8"),
            observation_lag_minutes=5,
            expected_observation_cadence_minutes=60,
            observation_excess_lag_minutes=0,
            observation_lag_penalty=Decimal("0"),
            shock_risk_score=Decimal("0.10"),
            cloud_cover_shock_adjustment_f=Decimal("0"),
            storm_shock_adjustment_f=Decimal("0"),
            marine_intrusion_adjustment_f=Decimal("0"),
            wind_shift_adjustment_f=Decimal("0"),
            discontinuity_suspected=False,
            confidence_downgrade=Decimal("0"),
            provenance_refs=(),
        )
        pre_decay = build_regime_assessment(
            city_id="nyc",
            current_state=current_state,
            path_state=PathProgressState(
                as_of_time=as_of_time,
                current_high_so_far_f=Decimal("70"),
                current_temp_f=Decimal("70"),
                threshold_gap_f=Decimal("2"),
                remaining_effective_window_minutes=110,
                estimated_intraday_slope_f_per_hr=Decimal("1"),
                solar_insolation_vector={"daylight_weight": Decimal("1"), "minutes_to_peak": Decimal("0")},
                thermal_ceiling_estimate_f=Decimal("73"),
                residual_gain_mean_f=Decimal("2"),
                residual_gain_p80_f=Decimal("3"),
                reachability_score=Decimal("0.60"),
                late_day_decay_factor=Decimal("0.85"),
                path_uncertainty_addon=Decimal("0.10"),
                threshold_already_crossed_flag=False,
            ),
            as_of_time=as_of_time,
        )
        soft = build_regime_assessment(
            city_id="nyc",
            current_state=current_state,
            path_state=PathProgressState(
                as_of_time=as_of_time,
                current_high_so_far_f=Decimal("70"),
                current_temp_f=Decimal("70"),
                threshold_gap_f=Decimal("2"),
                remaining_effective_window_minutes=80,
                estimated_intraday_slope_f_per_hr=Decimal("1"),
                solar_insolation_vector={"daylight_weight": Decimal("1"), "minutes_to_peak": Decimal("0")},
                thermal_ceiling_estimate_f=Decimal("73"),
                residual_gain_mean_f=Decimal("2"),
                residual_gain_p80_f=Decimal("3"),
                reachability_score=Decimal("0.80"),
                late_day_decay_factor=Decimal("0.60"),
                path_uncertainty_addon=Decimal("0.15"),
                threshold_already_crossed_flag=False,
            ),
            as_of_time=as_of_time,
        )
        hard = build_regime_assessment(
            city_id="nyc",
            current_state=current_state,
            path_state=PathProgressState(
                as_of_time=as_of_time,
                current_high_so_far_f=Decimal("70"),
                current_temp_f=Decimal("70"),
                threshold_gap_f=Decimal("2"),
                remaining_effective_window_minutes=50,
                estimated_intraday_slope_f_per_hr=Decimal("1"),
                solar_insolation_vector={"daylight_weight": Decimal("1"), "minutes_to_peak": Decimal("0")},
                thermal_ceiling_estimate_f=Decimal("73"),
                residual_gain_mean_f=Decimal("2"),
                residual_gain_p80_f=Decimal("3"),
                reachability_score=Decimal("0.80"),
                late_day_decay_factor=Decimal("0.35"),
                path_uncertainty_addon=Decimal("0.20"),
                threshold_already_crossed_flag=False,
            ),
            as_of_time=as_of_time,
        )
        self.assertEqual(pre_decay.active_regime, "CLEAR_STABLE_HEATING")
        self.assertEqual(soft.active_regime, "LATE_DAY_DECAY")
        self.assertEqual(soft.haircut_value, Decimal("0.20"))
        self.assertFalse(soft.block_flag)
        self.assertTrue(hard.block_flag)


if __name__ == "__main__":
    unittest.main()
