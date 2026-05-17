from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import tempfile
import unittest

from kalshi_weather.domain.enums import DecisionType, RunMode
from kalshi_weather.domain.models import EdgeEstimate, StrategyDecisionExplanation
from kalshi_weather.engines.shadow import apply_shadow_decision
from kalshi_weather.storage import SQLiteStateStore


class ShadowExecutionTest(unittest.TestCase):
    def test_apply_shadow_decision_uses_partial_fill_ratio(self) -> None:
        explanation = StrategyDecisionExplanation(
            schema_version="1.0.0",
            decision_id="d1",
            run_mode=RunMode.SHADOW,
            as_of_time=datetime.now(timezone.utc),
            market_ticker="M1",
            city_id="nyc",
            station_id="s1",
            settlement_rule_id="r1",
            data_freshness={},
            current_state={},
            forecast_summary={},
            path_state={},
            microstructure_summary={
                "selected_execution_style": "taker",
                "selected_taker_tradability_score": "0.60",
                "selected_ghost_liquidity_ratio": "0.80",
                "selected_depth_consumed_levels": 3,
                "selected_maker_fill_probability": "0.10",
                "selected_quote_stability_score": "0.40",
            },
            edge_summary={},
            regime_summary={},
            risk_summary={},
            final_decision=DecisionType.TAKER_ALLOWED,
            explanation_codes=(),
            provenance_refs=(),
            module_versions={},
        )
        # Updated 2026-05-17: p_model bumped 0.65 → 0.78 to pass the
        # volume-grinder favorite floor (0.70) added since this test was
        # originally written. The test's intent (verify partial-fill ratio)
        # is preserved; only the gate-blocking p_model changed.
        edge = EdgeEstimate(
            market_ticker="M1",
            side="yes",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.78"),
            p_market_exec=Decimal("0.40"),
            raw_edge=Decimal("0.38"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.02"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.04"),
            friction_to_edge_ratio=Decimal("0.10"),
            uncertainty_haircut=Decimal("0.20"),
            regime_haircut=Decimal("0.10"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.31"),
            executable_ev_per_contract=Decimal("0.27"),
            executable_ev_total=Decimal("0.27"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteStateStore(f"{tempdir}/state.sqlite3")
            result = apply_shadow_decision(store, "nyc", explanation, edge)
        self.assertIsNotNone(result.fill)
        self.assertIsNotNone(result.position)
        self.assertLess(result.fill.quantity_fp, Decimal("1"))
        self.assertEqual(result.fill.fill_scenario, "base")
        self.assertEqual(result.position.open_quantity_fp, result.fill.quantity_fp)


if __name__ == "__main__":
    unittest.main()
