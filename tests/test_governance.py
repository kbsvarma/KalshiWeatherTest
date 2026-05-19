from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.analytics.live_gating import NYC_MVP_LIVE_PROFILE
from kalshi_weather.domain.enums import QualificationState
from kalshi_weather.domain.models import CityQualificationState
from kalshi_weather.governance.qualification import apply_qualification_update, recommend_qualification_update
from kalshi_weather.engines.risk import build_risk_decision
from kalshi_weather.engines.ev import DecisionThresholds
from kalshi_weather.domain.models import CurrentStateEstimate, TradabilityAssessment


class QualificationGovernanceTest(unittest.TestCase):
    def test_recommend_shadow_only_with_small_sample(self) -> None:
        current_state = CityQualificationState(
            city_id="nyc",
            state=QualificationState.SHADOW_ONLY,
            effective_from=datetime.now(timezone.utc),
            effective_to=None,
            settlement_validation_score=Decimal("1"),
            calibration_score=Decimal("0"),
            nowcast_score=Decimal("0"),
            path_score=Decimal("0"),
            market_depth_score=Decimal("0"),
            slippage_score=Decimal("0"),
            shadow_ev_score=Decimal("0"),
            drawdown_score=Decimal("0"),
            promotion_reasons=(),
            demotion_reasons=(),
        )
        update = recommend_qualification_update(
            current_state=current_state,
            shadow_report={"shadow_fill_count": 1, "taker_allowed_count": 1, "lower_80_confidence_executable_ev": 0.1},
            drift_report={
                "average_observation_lag_minutes": 10,
                "average_observation_excess_lag_minutes": 0,
                "p95_observation_excess_lag_minutes": 0,
            },
            settled_validation_count=50,
        )
        self.assertEqual(update.next_state, QualificationState.SHADOW_ONLY)
        updated_state = apply_qualification_update(current_state, update)
        self.assertEqual(updated_state.state, QualificationState.SHADOW_ONLY)
        self.assertGreaterEqual(updated_state.settlement_validation_score, Decimal("0"))

    def test_recommend_observe_only_on_settlement_gap(self) -> None:
        current_state = CityQualificationState(
            city_id="nyc",
            state=QualificationState.SHADOW_ONLY,
            effective_from=datetime.now(timezone.utc),
            effective_to=None,
            settlement_validation_score=Decimal("1"),
            calibration_score=Decimal("1"),
            nowcast_score=Decimal("1"),
            path_score=Decimal("1"),
            market_depth_score=Decimal("1"),
            slippage_score=Decimal("1"),
            shadow_ev_score=Decimal("1"),
            drawdown_score=Decimal("1"),
            promotion_reasons=(),
            demotion_reasons=(),
        )
        # Updated 2026-05-17: settlement-validation gap now only triggers
        # OBSERVE_ONLY when the daily-high forecast signal is ALSO weak
        # (best_provider_mae_f > 5.0). Adding a weak settlement-error
        # summary so this test still exercises the demotion path.
        update = recommend_qualification_update(
            current_state=current_state,
            shadow_report={"shadow_fill_count": 25, "taker_allowed_count": 25, "lower_80_confidence_executable_ev": 0.2},
            drift_report={
                "average_observation_lag_minutes": 10,
                "average_observation_excess_lag_minutes": 0,
                "p95_observation_excess_lag_minutes": 0,
            },
            settled_validation_count=50,
            settlement_summary={
                "eligible_for_shadow_only": False,
                "resolved_validation_count": 10,
                "matched_market_count": 10,
                "supportable_market_count": 20,
                "unresolved_entry_count": 10,
                "critical_mismatch_count": 0,
            },
            settlement_error_summary={
                "sample_count": 50,
                "best_provider_mae_f": 6.5,  # > 5.0 threshold → weak
            },
        )
        self.assertEqual(update.next_state, QualificationState.OBSERVE_ONLY)
        self.assertIn("settlement_validation_incomplete_and_forecast_weak", update.reasons)

    def test_good_forecast_keeps_qualified_despite_validation_gap(self) -> None:
        """Regression test for the 2026-05-17 fix: cities with no settlement
        corpus entries (because they were added after the corpus was built)
        should NOT be demoted to OBSERVE_ONLY if their actual provider
        forecast errors are good."""
        current_state = CityQualificationState(
            city_id="atl",
            state=QualificationState.SHADOW_ONLY,
            effective_from=datetime.now(timezone.utc),
            effective_to=None,
            settlement_validation_score=Decimal("0"),
            calibration_score=Decimal("0.7"),
            nowcast_score=Decimal("0.8"),
            path_score=Decimal("0.6"),
            market_depth_score=Decimal("0.5"),
            slippage_score=Decimal("0.5"),
            shadow_ev_score=Decimal("0.1"),
            drawdown_score=Decimal("0"),
            promotion_reasons=(),
            demotion_reasons=(),
        )
        update = recommend_qualification_update(
            current_state=current_state,
            shadow_report={"shadow_fill_count": 5, "taker_allowed_count": 5, "lower_80_confidence_executable_ev": 0.1},
            drift_report={
                "average_observation_lag_minutes": 5,
                "average_observation_excess_lag_minutes": 0,
                "p95_observation_excess_lag_minutes": 0,
            },
            settled_validation_count=0,  # NO corpus entries
            settlement_summary={"eligible_for_shadow_only": False},
            settlement_error_summary={
                "sample_count": 50,
                "best_provider_mae_f": 2.5,  # under 5.0 → forecast is good
            },
        )
        # Must NOT demote to OBSERVE_ONLY just because validation count is 0
        self.assertEqual(update.next_state, QualificationState.SHADOW_ONLY)

    def test_risk_decision_blocks_observe_only_city(self) -> None:
        risk = build_risk_decision(
            qualification_state=QualificationState.OBSERVE_ONLY,
            current_state=CurrentStateEstimate(
                as_of_time=datetime.now(timezone.utc),
                station_id="s1",
                current_temp_est_f=Decimal("60"),
                current_temp_sigma_f=Decimal("1"),
                near_term_slope_f_per_hr=Decimal("0"),
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
            ),
            tradability=TradabilityAssessment(
                market_ticker="m1",
                side="yes",
                tradability_score=Decimal("1"),
                thin_book_flag=False,
                stale_book_flag=False,
                friction_overload_flag=False,
                allowed_taker_flag=True,
                allowed_maker_flag=False,
                block_reasons=(),
            ),
            thresholds=DecisionThresholds(),
            raw_edge=Decimal("0.1"),
            executable_ev=Decimal("0.1"),
            friction_to_edge_ratio=Decimal("0.2"),
        )
        self.assertFalse(risk.qualification_ok)
        self.assertIn("qualification_block", risk.veto_reasons)

    def test_risk_decision_marks_exposure_not_ok_when_portfolio_limit_is_hit(self) -> None:
        risk = build_risk_decision(
            qualification_state=QualificationState.SHADOW_ONLY,
            current_state=CurrentStateEstimate(
                as_of_time=datetime.now(timezone.utc),
                station_id="s1",
                current_temp_est_f=Decimal("60"),
                current_temp_sigma_f=Decimal("1"),
                near_term_slope_f_per_hr=Decimal("0"),
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
            ),
            tradability=TradabilityAssessment(
                market_ticker="m1",
                side="yes",
                tradability_score=Decimal("1"),
                thin_book_flag=False,
                stale_book_flag=False,
                friction_overload_flag=False,
                allowed_taker_flag=True,
                allowed_maker_flag=False,
                block_reasons=(),
            ),
            thresholds=DecisionThresholds(),
            raw_edge=Decimal("0.1"),
            executable_ev=Decimal("0.1"),
            friction_to_edge_ratio=Decimal("0.2"),
            # Use 55.0 to exceed PORTFOLIO_RISK_LIMIT=50.0 (raised
            # 2026-05-18 from 5.0 — the previous 5.0 cap was hard-vetoing
            # every new market while only $1.06 of the $15 daily cap had
            # been spent. See engines/risk.py for full cap history.)
            portfolio_risk_units_value=Decimal("55.0"),
        )
        self.assertFalse(risk.exposure_ok)
        self.assertFalse(risk.correlation_ok)
        self.assertIn("portfolio_risk_limit", risk.veto_reasons)

    def test_nyc_mvp_profile_can_promote_live_pilot(self) -> None:
        current_state = CityQualificationState(
            city_id="nyc",
            state=QualificationState.SHADOW_ONLY,
            effective_from=datetime.now(timezone.utc),
            effective_to=None,
            settlement_validation_score=Decimal("0"),
            calibration_score=Decimal("0"),
            nowcast_score=Decimal("0"),
            path_score=Decimal("0"),
            market_depth_score=Decimal("0"),
            slippage_score=Decimal("0"),
            shadow_ev_score=Decimal("0"),
            drawdown_score=Decimal("0"),
            promotion_reasons=(),
            demotion_reasons=(),
        )
        decision_payloads = [
            {
                "city_id": "nyc",
                "schema_version": "1.0.0",
                "final_decision": "TAKER_ALLOWED" if index < 15 else "WATCH",
                "data_freshness": {
                    "observation_lag_minutes": 8,
                    "observation_excess_lag_minutes": 0,
                },
                "path_state": {
                    "p_yes": "0.60",
                    "threshold_gap_f": "1",
                    "reachability_score": "0.8",
                },
                "microstructure_summary": {
                    "max_taker_tradability_score": "0.8",
                    "max_quote_stability_score": "0.7",
                    "mean_ghost_liquidity_ratio": "0.2",
                },
                "risk_summary": {
                    "taker_candidate_count": 2,
                },
                "edge_summary": {
                    "selected_slippage_cost": "0.01",
                    "selected_adverse_selection_penalty": "0.01",
                    "selected_executable_ev": "0.08",
                },
            }
            for index in range(75)
        ]
        update = recommend_qualification_update(
            current_state=current_state,
            shadow_report={
                "decision_count": 75,
                "shadow_fill_count": 0,
                "taker_allowed_count": 15,
                "lower_80_confidence_executable_ev": 0.08,
                "settled_position_count": 25,
                "settled_win_rate": 0.72,
                "total_settled_pnl_dollars": 1.25,
            },
            drift_report={
                "average_observation_lag_minutes": 10,
                "average_observation_excess_lag_minutes": 0,
                "p95_observation_excess_lag_minutes": 0,
                "schema_versions": {"1.0.0": 75},
            },
            settled_validation_count=50,
            city_id="nyc",
            profile=NYC_MVP_LIVE_PROFILE,
            decision_payloads=decision_payloads,
            settlement_summary={
                "resolved_validation_count": 50,
                "matched_market_count": 50,
                "critical_mismatch_count": 0,
                "revision_resolved_count": 50,
                "unresolved_entry_count": 0,
            },
            nowcast_report={
                "sample_sufficient": True,
                "beats_baselines": True,
                "sample_count": 217,
                "required_sample": 24,
                "model_mae_f": 1.0,
                "best_available_baseline_mae_f": 1.05,
                "shock_buckets": {"low": {"count": 10}, "medium": {"count": 1}, "high": {"count": 0}},
            },
            calibration_report={
                "global_sample_count": 10,
                "global_unique_run_count": 2,
                "global_mean_abs_error_f": 0.8,
                "provider_reports": {
                    "NWS": {"mean_bias_f": 0.1},
                    "NWS_GRID": {"mean_bias_f": 0.2},
                },
            },
        )
        self.assertEqual(update.next_state, QualificationState.LIVE_PILOT)

    def test_non_nyc_near_baseline_nowcast_can_enter_shadow_only(self) -> None:
        current_state = CityQualificationState(
            city_id="chi",
            state=QualificationState.OBSERVE_ONLY,
            effective_from=datetime.now(timezone.utc),
            effective_to=None,
            settlement_validation_score=Decimal("0"),
            calibration_score=Decimal("0"),
            nowcast_score=Decimal("0"),
            path_score=Decimal("0"),
            market_depth_score=Decimal("0"),
            slippage_score=Decimal("0"),
            shadow_ev_score=Decimal("0"),
            drawdown_score=Decimal("0"),
            promotion_reasons=(),
            demotion_reasons=(),
        )
        update = recommend_qualification_update(
            current_state=current_state,
            shadow_report={
                "decision_count": 0,
                "shadow_fill_count": 0,
                "taker_allowed_count": 0,
                "lower_80_confidence_executable_ev": None,
                "settled_position_count": 0,
            },
            drift_report={
                "average_observation_lag_minutes": 18,
                "average_observation_excess_lag_minutes": 4,
                "p95_observation_excess_lag_minutes": 8,
                "schema_versions": {},
            },
            settled_validation_count=100,
            city_id="chi",
            settlement_summary={
                "eligible_for_shadow_only": True,
                "resolved_validation_count": 100,
                "matched_market_count": 100,
                "critical_mismatch_count": 0,
                "unresolved_entry_count": 0,
                "supportable_market_count": 100,
            },
            nowcast_report={
                "sample_sufficient": True,
                "beats_baselines": False,
                "redesign_required": False,
                "model_mae_f": 0.2745,
                "best_available_baseline_mae_f": 0.2694,
                "sample_count": 632,
                "required_sample": 24,
                "shock_buckets": {"low": {"count": 629}, "medium": {"count": 3}, "high": {"count": 0}},
            },
            calibration_report={
                "sample_sufficient": True,
                "global_sample_count": 536,
                "global_mean_abs_error_f": 1.48,
                "provider_reports": {
                    "NWS": {"mean_bias_f": 0.2},
                    "NWS_GRID": {"mean_bias_f": 0.3},
                },
            },
        )
        self.assertEqual(update.next_state, QualificationState.SHADOW_ONLY)
        self.assertIn("nowcast_near_baseline_shadow_probe", update.reasons)


if __name__ == "__main__":
    unittest.main()
