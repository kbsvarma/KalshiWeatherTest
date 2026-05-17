from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.models import EdgeEstimate, MicrostructureAssessment, PathProgressState, RegimeAssessment
from kalshi_weather.engines.ev import _fee_cost, compute_edge_estimate


class EdgeEstimateTest(unittest.TestCase):
    def test_edge_estimate_includes_round_trip_fee_and_portfolio_haircut(self) -> None:
        micro = MicrostructureAssessment(
            market_ticker="M1",
            side="yes",
            quantity_fp=Decimal("1"),
            top_of_book_price=Decimal("0.60"),
            executable_wap_price=Decimal("0.60"),
            depth_consumed_levels=1,
            slippage_cost=Decimal("0"),
            top_of_book_durability_score=Decimal("0.8"),
            quote_stability_score=Decimal("0.8"),
            ghost_liquidity_ratio=Decimal("0.1"),
            maker_fill_probability=Decimal("0.2"),
            maker_adverse_selection_penalty=Decimal("0.02"),
            taker_adverse_selection_penalty=Decimal("0.01"),
            time_to_close_bucket="1h_to_4h",
        )
        path_state = PathProgressState(
            as_of_time=datetime(2026, 4, 6, 12, tzinfo=timezone.utc),
            current_high_so_far_f=Decimal("70"),
            current_temp_f=Decimal("70"),
            threshold_gap_f=Decimal("2"),
            remaining_effective_window_minutes=180,
            estimated_intraday_slope_f_per_hr=Decimal("1"),
            solar_insolation_vector={"daylight_weight": Decimal("1"), "minutes_to_peak": Decimal("60")},
            thermal_ceiling_estimate_f=Decimal("75"),
            residual_gain_mean_f=Decimal("2"),
            residual_gain_p80_f=Decimal("3"),
            reachability_score=Decimal("0.7"),
            late_day_decay_factor=Decimal("1"),
            path_uncertainty_addon=Decimal("0.05"),
            threshold_already_crossed_flag=False,
        )
        regime = RegimeAssessment(
            city_id="nyc",
            as_of_time=datetime(2026, 4, 6, 12, tzinfo=timezone.utc),
            active_regime="CLEAR_STABLE_HEATING",
            regime_scores={"CLEAR_STABLE_HEATING": Decimal("1")},
            feature_values={},
            block_flag=False,
            haircut_value=Decimal("0"),
            sizing_multiplier=Decimal("1"),
            explanation_codes=("clear_stable_heating",),
        )

        edge = compute_edge_estimate(
            market_ticker="M1",
            side="yes",
            quantity=Decimal("1"),
            p_yes=Decimal("0.70"),
            executable_price=Decimal("0.60"),
            micro=micro,
            path_state=path_state,
            regime=regime,
            portfolio_risk_units_value=Decimal("1.2"),
        )

        expected_fee = _fee_cost(Decimal("0.60"), Decimal("1"), 1) + _fee_cost(Decimal("0.70"), Decimal("1"), 1)
        self.assertEqual(edge.fee_cost, expected_fee)
        self.assertGreater(edge.portfolio_haircut, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
