from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.enums import DecisionType, RunMode
from kalshi_weather.domain.models import EdgeEstimate, StrategyDecisionExplanation
from kalshi_weather.live import LiveAdapterError, ThinLiveAdapter, build_execution_plan


class LiveAdapterTest(unittest.TestCase):
    def test_prepare_dry_run_payload(self) -> None:
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
            microstructure_summary={},
            edge_summary={},
            regime_summary={},
            risk_summary={},
            final_decision=DecisionType.TAKER_ALLOWED,
            explanation_codes=(),
            provenance_refs=(),
            module_versions={},
        )
        edge = EdgeEstimate(
            market_ticker="M1",
            side="no",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.7"),
            p_market_exec=Decimal("0.2"),
            raw_edge=Decimal("0.5"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.01"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.03"),
            friction_to_edge_ratio=Decimal("0.06"),
            uncertainty_haircut=Decimal("0.2"),
            regime_haircut=Decimal("0"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.4"),
            executable_ev_per_contract=Decimal("0.37"),
            executable_ev_total=Decimal("0.37"),
        )
        plan = build_execution_plan(explanation, edge)
        adapter = ThinLiveAdapter()
        payload = adapter.submit_plan(plan, {"all_passed": True}, dry_run=True)
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertEqual(payload["payload"]["ticker"], "M1")
        self.assertEqual(payload["payload"]["action"], "buy")
        self.assertEqual(payload["payload"]["time_in_force"], "immediate_or_cancel")

    def test_live_gate_block(self) -> None:
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
            microstructure_summary={},
            edge_summary={},
            regime_summary={},
            risk_summary={},
            final_decision=DecisionType.TAKER_ALLOWED,
            explanation_codes=(),
            provenance_refs=(),
            module_versions={},
        )
        edge = EdgeEstimate(
            market_ticker="M1",
            side="yes",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.7"),
            p_market_exec=Decimal("0.2"),
            raw_edge=Decimal("0.5"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.01"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.03"),
            friction_to_edge_ratio=Decimal("0.06"),
            uncertainty_haircut=Decimal("0.2"),
            regime_haircut=Decimal("0"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.4"),
            executable_ev_per_contract=Decimal("0.37"),
            executable_ev_total=Decimal("0.37"),
        )
        plan = build_execution_plan(explanation, edge)
        with self.assertRaises(LiveAdapterError):
            ThinLiveAdapter().submit_plan(plan, {"all_passed": False}, dry_run=True)

    def test_real_submit_requires_live_trade_mode(self) -> None:
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
            microstructure_summary={},
            edge_summary={},
            regime_summary={},
            risk_summary={},
            final_decision=DecisionType.TAKER_ALLOWED,
            explanation_codes=(),
            provenance_refs=(),
            module_versions={},
        )
        edge = EdgeEstimate(
            market_ticker="M1",
            side="yes",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.7"),
            p_market_exec=Decimal("0.2"),
            raw_edge=Decimal("0.5"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.01"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.03"),
            friction_to_edge_ratio=Decimal("0.06"),
            uncertainty_haircut=Decimal("0.2"),
            regime_haircut=Decimal("0"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.4"),
            executable_ev_per_contract=Decimal("0.37"),
            executable_ev_total=Decimal("0.37"),
        )
        plan = build_execution_plan(explanation, edge)
        with self.assertRaises(LiveAdapterError):
            ThinLiveAdapter().submit_plan(plan, {"all_passed": True}, dry_run=False)

    def test_stale_decision_is_blocked(self) -> None:
        explanation = StrategyDecisionExplanation(
            schema_version="1.0.0",
            decision_id="d1",
            run_mode=RunMode.SHADOW,
            as_of_time=datetime.now(timezone.utc).replace(microsecond=0),
            market_ticker="M1",
            city_id="nyc",
            station_id="s1",
            settlement_rule_id="r1",
            data_freshness={},
            current_state={},
            forecast_summary={},
            path_state={},
            microstructure_summary={},
            edge_summary={},
            regime_summary={},
            risk_summary={},
            final_decision=DecisionType.TAKER_ALLOWED,
            explanation_codes=(),
            provenance_refs=(),
            module_versions={},
        )
        edge = EdgeEstimate(
            market_ticker="M1",
            side="yes",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.7"),
            p_market_exec=Decimal("0.2"),
            raw_edge=Decimal("0.5"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.01"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.03"),
            friction_to_edge_ratio=Decimal("0.06"),
            uncertainty_haircut=Decimal("0.2"),
            regime_haircut=Decimal("0"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.4"),
            executable_ev_per_contract=Decimal("0.37"),
            executable_ev_total=Decimal("0.37"),
        )
        plan = build_execution_plan(explanation, edge)
        stale_plan = replace(plan, decision_utc=datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc))
        with self.assertRaises(LiveAdapterError) as exc:
            ThinLiveAdapter().submit_plan(stale_plan, {"all_passed": True}, dry_run=True)
        self.assertIn("stale_decision_abort", str(exc.exception))
        self.assertIn("decision_age_seconds=", str(exc.exception))

    def test_maker_only_is_rejected_for_live_submission(self) -> None:
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
            microstructure_summary={},
            edge_summary={},
            regime_summary={},
            risk_summary={},
            final_decision=DecisionType.MAKER_ONLY,
            explanation_codes=(),
            provenance_refs=(),
            module_versions={},
        )
        edge = EdgeEstimate(
            market_ticker="M1",
            side="yes",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.7"),
            p_market_exec=Decimal("0.2"),
            raw_edge=Decimal("0.5"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.01"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.03"),
            friction_to_edge_ratio=Decimal("0.06"),
            uncertainty_haircut=Decimal("0.2"),
            regime_haircut=Decimal("0"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.4"),
            executable_ev_per_contract=Decimal("0.37"),
            executable_ev_total=Decimal("0.37"),
        )
        plan = build_execution_plan(explanation, edge)
        with self.assertRaises(LiveAdapterError):
            ThinLiveAdapter().submit_plan(plan, {"all_passed": True}, dry_run=True)


if __name__ == "__main__":
    unittest.main()
