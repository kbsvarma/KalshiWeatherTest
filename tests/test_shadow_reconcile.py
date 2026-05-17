from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import tempfile
import unittest

from kalshi_weather.domain.models import ShadowFill, ShadowPosition
from kalshi_weather.engines import reconcile_shadow_position
from kalshi_weather.storage import SQLiteStateStore


class ShadowReconcileTest(unittest.TestCase):
    def test_reconcile_shadow_position(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteStateStore(f"{tempdir}/state.sqlite3")
            position = ShadowPosition(
                city_id="nyc",
                market_ticker="M1",
                side="yes",
                open_quantity_fp=Decimal("1"),
                avg_cost_dollars=Decimal("0.40"),
                cumulative_fees_dollars=Decimal("0.01"),
                mark_pnl_dollars=Decimal("0"),
                settled_pnl_dollars=Decimal("0"),
                lifecycle_status="OPEN",
            )
            fill = ShadowFill(
                shadow_fill_id="f1",
                decision_id="d1",
                market_ticker="M1",
                side="yes",
                quantity_fp=Decimal("1"),
                modeled_fill_price_dollars=Decimal("0.40"),
                fill_scenario="base",
                fill_confidence=Decimal("0.5"),
                modeled_fee_dollars=Decimal("0.01"),
                modeled_slippage_dollars=Decimal("0.01"),
                modeled_adverse_selection_dollars=Decimal("0.01"),
                fill_time=datetime.now(timezone.utc),
                reconciliation_status="OPEN",
            )
            store.save_shadow_position(position)
            store.save_shadow_fill("nyc", fill)
            result = reconcile_shadow_position(
                store,
                city_id="nyc",
                market_payload={"ticker": "M1", "status": "finalized", "result": "yes"},
            )
            self.assertTrue(result.settled)
            updated = store.get_shadow_position("nyc")
            self.assertEqual(updated.lifecycle_status, "CLOSED")

    def test_reconcile_shadow_position_waits_for_revision_resolved_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteStateStore(f"{tempdir}/state.sqlite3")
            position = ShadowPosition(
                city_id="nyc",
                market_ticker="M1",
                side="yes",
                open_quantity_fp=Decimal("1"),
                avg_cost_dollars=Decimal("0.40"),
                cumulative_fees_dollars=Decimal("0.01"),
                mark_pnl_dollars=Decimal("0"),
                settled_pnl_dollars=Decimal("0"),
                lifecycle_status="OPEN",
            )
            fill = ShadowFill(
                shadow_fill_id="f1",
                decision_id="d1",
                market_ticker="M1",
                side="yes",
                quantity_fp=Decimal("1"),
                modeled_fill_price_dollars=Decimal("0.40"),
                fill_scenario="base",
                fill_confidence=Decimal("0.5"),
                modeled_fee_dollars=Decimal("0.01"),
                modeled_slippage_dollars=Decimal("0.01"),
                modeled_adverse_selection_dollars=Decimal("0.01"),
                fill_time=datetime.now(timezone.utc),
                reconciliation_status="OPEN",
            )
            store.save_shadow_position(position)
            store.save_shadow_fill("nyc", fill)
            store.save_settlement_validation_record(
                {
                    "validation_id": "nyc:M1",
                    "city_id": "nyc",
                    "market_ticker": "M1",
                    "local_date": "2026-04-05",
                    "matched": True,
                    "critical_mismatch": False,
                    "report_status": "CANDIDATE_FINAL",
                    "report_version": 2,
                    "revision_resolved": False,
                    "revision_resolution_reason": "awaiting later revisions",
                    "notes": ["awaiting_revision_resolution"],
                }
            )
            result = reconcile_shadow_position(
                store,
                city_id="nyc",
                market_payload={"ticker": "M1", "status": "finalized", "result": "yes"},
            )
            self.assertFalse(result.settled)
            self.assertIn("settlement_revision_unresolved", result.notes)

    def test_reconcile_shadow_position_can_settle_from_validation_without_market_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = SQLiteStateStore(f"{tempdir}/state.sqlite3")
            position = ShadowPosition(
                city_id="nyc",
                market_ticker="M1",
                side="no",
                open_quantity_fp=Decimal("1"),
                avg_cost_dollars=Decimal("0.82"),
                cumulative_fees_dollars=Decimal("0.01"),
                mark_pnl_dollars=Decimal("0"),
                settled_pnl_dollars=Decimal("0"),
                lifecycle_status="OPEN",
            )
            fill = ShadowFill(
                shadow_fill_id="f1",
                decision_id="d1",
                market_ticker="M1",
                side="no",
                quantity_fp=Decimal("1"),
                modeled_fill_price_dollars=Decimal("0.82"),
                fill_scenario="base",
                fill_confidence=Decimal("0.7"),
                modeled_fee_dollars=Decimal("0.01"),
                modeled_slippage_dollars=Decimal("0.01"),
                modeled_adverse_selection_dollars=Decimal("0.01"),
                fill_time=datetime.now(timezone.utc),
                reconciliation_status="OPEN",
                predicted_executable_ev_per_contract=Decimal("0.03"),
                predicted_executable_ev_total=Decimal("0.03"),
                model_confidence=Decimal("0.6"),
                execution_confidence=Decimal("0.7"),
                governance_confidence=Decimal("0.8"),
                overall_trade_confidence=Decimal("0.68"),
                confidence_reasons=("edge_survives_friction",),
            )
            store.save_shadow_position(position)
            store.save_shadow_fill("nyc", fill)
            store.save_settlement_validation_record(
                {
                    "validation_id": "nyc:M1",
                    "city_id": "nyc",
                    "market_ticker": "M1",
                    "local_date": "2026-04-05",
                    "matched": True,
                    "critical_mismatch": False,
                    "report_status": "FINAL",
                    "report_version": 3,
                    "revision_resolved": True,
                    "actual_result": "no",
                    "revision_resolution_reason": "later_revision_window_cleared",
                }
            )
            result = reconcile_shadow_position(
                store,
                city_id="nyc",
                market_payload=None,
            )
            self.assertTrue(result.settled)
            self.assertIn("settled_from_settlement_validation", result.notes)
            settled_fill = store.list_shadow_fills(city_id="nyc", market_ticker="M1")[0]
            self.assertEqual(settled_fill.reconciliation_status, "SETTLED")
            self.assertEqual(settled_fill.overall_trade_confidence, Decimal("0.68"))
            self.assertEqual(settled_fill.confidence_reasons, ("edge_survives_friction",))
            self.assertEqual(settled_fill.predicted_executable_ev_total, Decimal("0.03"))


if __name__ == "__main__":
    unittest.main()
