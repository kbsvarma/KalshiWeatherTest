from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from kalshi_weather.tools.prepare_live_plan import (
    _edge_from_stored_decision,
    _safe_prepare_payload,
    _select_latest_taker_decision,
)


class PrepareLivePlanTest(unittest.TestCase):
    def test_select_latest_taker_decision_ignores_stale_green_ticket(self) -> None:
        stale = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 6, 5, 0, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR06-T54",
        }
        latest_watch = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 6, 6, 40, tzinfo=timezone.utc).isoformat(),
            "final_decision": "WATCH",
            "market_ticker": "KXHIGHNY-26APR06-T61",
        }
        latest_no_trade = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 6, 6, 40, tzinfo=timezone.utc).isoformat(),
            "final_decision": "NO_TRADE",
            "market_ticker": "KXHIGHNY-26APR06-T54",
        }
        selected = _select_latest_taker_decision([stale, latest_watch, latest_no_trade])
        self.assertIsNone(selected)

    def test_select_latest_taker_decision_uses_latest_cycle_window(self) -> None:
        latest_watch = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 16, 40, 42, tzinfo=timezone.utc).isoformat(),
            "final_decision": "WATCH",
            "market_ticker": "KXHIGHNY-26APR09-T53",
        }
        taker_from_same_cycle = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 16, 40, 40, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR10-T61",
        }
        stale_taker = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 16, 20, 0, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR09-T53",
        }

        selected = _select_latest_taker_decision([stale_taker, latest_watch, taker_from_same_cycle])

        self.assertEqual(selected, taker_from_same_cycle)

    def test_select_latest_taker_decision_falls_back_to_non_preferred_city(self) -> None:
        latest_nyc_watch = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 16, 40, 42, tzinfo=timezone.utc).isoformat(),
            "final_decision": "WATCH",
            "market_ticker": "KXHIGHNY-26APR10-T68",
        }
        latest_aus_taker = {
            "city_id": "aus",
            "as_of_time": datetime(2026, 4, 9, 16, 40, 41, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHAUS-26APR10-T77",
        }

        selected = _select_latest_taker_decision([latest_nyc_watch, latest_aus_taker])

        self.assertEqual(selected, latest_aus_taker)

    def test_select_latest_taker_decision_prefers_best_edge_within_latest_cycle(self) -> None:
        earlier_lower_ev = {
            "city_id": "bos",
            "as_of_time": datetime(2026, 4, 11, 3, 28, 30, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHTBOS-26APR11-T57",
            "edge_summary": {"selected_executable_ev": "0.22"},
        }
        later_lower_priority = {
            "city_id": "lax",
            "as_of_time": datetime(2026, 4, 11, 3, 28, 40, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHLAX-26APR11-T71",
            "edge_summary": {"selected_executable_ev": "0.11"},
        }

        selected = _select_latest_taker_decision([earlier_lower_ev, later_lower_priority], preferred_city_id="nyc")

        self.assertEqual(selected, earlier_lower_ev)

    def test_select_latest_taker_decision_prefers_latest_city_cycle(self) -> None:
        latest_nyc_taker = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 6, 6, 40, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR06-T54",
        }
        older_lax_taker = {
            "city_id": "lax",
            "as_of_time": datetime(2026, 4, 6, 6, 20, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHLAX-26APR06-T70",
        }
        selected = _select_latest_taker_decision([older_lax_taker, latest_nyc_taker])
        self.assertEqual(selected, latest_nyc_taker)

    def test_select_latest_taker_decision_defaults_to_next_available_day(self) -> None:
        same_day_taker = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 21, 0, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR09-T60",
        }
        next_day_taker = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 21, 1, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR10-T61",
        }

        selected = _select_latest_taker_decision(
            [same_day_taker, next_day_taker],
            market_day_mode="next_available",
            timezone_by_city={"nyc": "America/New_York"},
        )

        self.assertEqual(selected, next_day_taker)

    def test_select_latest_taker_decision_skips_open_city_by_default(self) -> None:
        nyc_taker = {
            "city_id": "nyc",
            "as_of_time": datetime(2026, 4, 9, 21, 1, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHNY-26APR10-T61",
        }
        aus_taker = {
            "city_id": "aus",
            "as_of_time": datetime(2026, 4, 9, 21, 0, tzinfo=timezone.utc).isoformat(),
            "final_decision": "TAKER_ALLOWED",
            "market_ticker": "KXHIGHAUS-26APR10-T77",
        }

        selected = _select_latest_taker_decision(
            [nyc_taker, aus_taker],
            market_day_mode="next_available",
            timezone_by_city={
                "nyc": "America/New_York",
                "aus": "America/Chicago",
            },
            open_city_ids={"nyc"},
            exclude_open_cities=True,
        )

        self.assertEqual(selected, aus_taker)

    def test_edge_from_stored_decision_reconstructs_decimals_safely(self) -> None:
        latest = {
            "market_ticker": "KXHIGHNY-26APR06-T54",
            "edge_summary": {
                "selected_side": "no",
                "selected_p_model": "0.42",
                "selected_raw_edge": 0,
                "selected_friction_to_edge_ratio": "0.0",
                "selected_uncertainty_haircut": "0.15",
                "selected_executable_ev": 0,
            },
        }
        edge = _edge_from_stored_decision(
            latest,
            executable_price=Decimal("0.82"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.00"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.02"),
            portfolio_haircut=Decimal("0"),
        )
        self.assertEqual(edge.quantity_fp, Decimal("1"))
        self.assertEqual(edge.p_model, Decimal("0.42"))
        self.assertEqual(edge.raw_edge, Decimal("0"))
        self.assertEqual(edge.executable_ev_per_contract, Decimal("0"))

    def test_safe_prepare_payload_uses_quantity_fp_when_adapter_payload_build_fails(self) -> None:
        class BrokenAdapter:
            def prepare_order_payload(self, plan):  # noqa: ANN001
                raise RuntimeError("boom")

        plan = SimpleNamespace(
            market_ticker="KXHIGHNY-26APR06-T54",
            side="yes",
            quantity_fp=Decimal("1"),
        )
        payload = _safe_prepare_payload(BrokenAdapter(), plan)
        self.assertEqual(payload["market_ticker"], "KXHIGHNY-26APR06-T54")
        self.assertEqual(payload["side"], "yes")
        self.assertEqual(payload["quantity"], "1")
        self.assertIn("payload_prepare_failed", str(payload["error"]))


if __name__ == "__main__":
    unittest.main()
