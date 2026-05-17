from __future__ import annotations

import unittest

from kalshi_weather.analytics import (
    build_city_daily_selection,
    build_opportunity_board,
    parse_market_date,
    select_latest_market_decisions,
    select_scannable_market_decisions,
)


class OpportunityBoardTest(unittest.TestCase):
    def test_build_city_daily_selection_prefers_high_win_probability_candidate(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR10-T61",
                "as_of_time": "2026-04-09T21:01:00+00:00",
                "market_date": "2026-04-10",
                "final_decision": "TAKER_ALLOWED",
                "regime_summary": {"haircut_value": "0"},
                "risk_summary": {"hard_halt_flag": False},
                "microstructure_summary": {"selected_taker_tradability_score": "0.90"},
                "edge_summary": {
                    "selected_side": "yes",
                    "selected_p_model": "0.28",
                    "selected_p_market_exec": "0.05",
                    "selected_executable_ev": "0.06",
                    "selected_raw_edge": "0.23",
                    "selected_total_friction": "0.01",
                    "selected_uncertainty_haircut": "0.20",
                    "selected_portfolio_haircut": "0",
                    "confidence_summary": {
                        "overall_trade_confidence": "0.70",
                    },
                    "best_taker_candidate": {
                        "side": "yes",
                        "p_model": "0.28",
                        "p_market_exec": "0.05",
                        "executable_ev": "0.06",
                        "raw_edge": "0.23",
                        "total_friction": "0.01",
                        "tradability_score": "0.90",
                        "uncertainty_haircut": "0.20",
                        "portfolio_haircut": "0",
                    },
                },
            },
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR10-T54",
                "as_of_time": "2026-04-09T21:02:00+00:00",
                "market_date": "2026-04-10",
                "final_decision": "TAKER_ALLOWED",
                "regime_summary": {"haircut_value": "0"},
                "risk_summary": {"hard_halt_flag": False},
                "microstructure_summary": {"selected_taker_tradability_score": "0.88"},
                "edge_summary": {
                    "selected_side": "no",
                    "selected_p_model": "0.86",
                    "selected_p_market_exec": "0.65",
                    "selected_executable_ev": "0.05",
                    "selected_raw_edge": "0.21",
                    "selected_total_friction": "0.02",
                    "selected_uncertainty_haircut": "0.08",
                    "selected_portfolio_haircut": "0",
                    "confidence_summary": {
                        "overall_trade_confidence": "0.66",
                    },
                    "best_taker_candidate": {
                        "side": "no",
                        "p_model": "0.86",
                        "p_market_exec": "0.65",
                        "executable_ev": "0.05",
                        "raw_edge": "0.21",
                        "total_friction": "0.02",
                        "tradability_score": "0.88",
                        "uncertainty_haircut": "0.08",
                        "portfolio_haircut": "0",
                    },
                },
            },
        ]
        selection = build_city_daily_selection(
            decisions,
            market_day_mode="next_available",
            timezone_by_city={"nyc": "America/New_York"},
        )
        self.assertEqual(selection["result"], "BET")
        self.assertEqual(selection["best_candidate"]["market_ticker"], "KXHIGHNY-26APR10-T54")
        self.assertEqual(selection["best_candidate"]["selected_side"], "no")

    def test_build_city_daily_selection_uses_best_blocked_candidate_for_skip_reason(self) -> None:
        decisions = [
            {
                "city_id": "mia",
                "market_ticker": "KXHIGHMIA-26APR10-T83",
                "as_of_time": "2026-04-09T21:02:00+00:00",
                "market_date": "2026-04-10",
                "final_decision": "WATCH",
                "explanation_codes": ["watch_raw_edge_present"],
                "regime_summary": {"haircut_value": "0"},
                "risk_summary": {"hard_halt_flag": False},
                "microstructure_summary": {"selected_taker_tradability_score": "0.82"},
                "edge_summary": {
                    "confidence_summary": {
                        "overall_trade_confidence": "0.62",
                    },
                    "best_taker_candidate": {
                        "side": "yes",
                        "p_model": "0.145",
                        "p_market_exec": "0.05",
                        "executable_ev": "0.02",
                        "raw_edge": "0.095",
                        "total_friction": "0.04",
                        "tradability_score": "0.82",
                        "uncertainty_haircut": "0.22",
                        "portfolio_haircut": "0",
                        "block_reasons": ["qualification_block"],
                    },
                },
            },
        ]
        selection = build_city_daily_selection(
            decisions,
            market_day_mode="next_available",
            timezone_by_city={"mia": "America/New_York"},
        )
        self.assertEqual(selection["result"], "SKIP")
        self.assertEqual(selection["best_candidate"]["market_ticker"], "KXHIGHMIA-26APR10-T83")
        self.assertIn("decision_not_taker_allowed", selection["rejection_reasons"])

    def test_build_city_daily_selection_accepts_strong_cheap_longshot(self) -> None:
        decisions = [
            {
                "city_id": "den",
                "market_ticker": "KXHIGHDEN-26APR10-T72",
                "as_of_time": "2026-04-09T21:02:00+00:00",
                "market_date": "2026-04-10",
                "final_decision": "TAKER_ALLOWED",
                "regime_summary": {"haircut_value": "0"},
                "risk_summary": {"hard_halt_flag": False},
                "microstructure_summary": {"selected_taker_tradability_score": "0.88"},
                "edge_summary": {
                    "selected_side": "yes",
                    "selected_p_model": "0.325",
                    "selected_p_market_exec": "0.05",
                    "selected_executable_ev": "0.134",
                    "selected_raw_edge": "0.275",
                    "selected_total_friction": "0.053",
                    "selected_uncertainty_haircut": "0.20",
                    "selected_portfolio_haircut": "0",
                    "confidence_summary": {
                        "overall_trade_confidence": "0.54",
                    },
                    "best_taker_candidate": {
                        "side": "yes",
                        "p_model": "0.325",
                        "p_market_exec": "0.05",
                        "executable_ev": "0.134",
                        "raw_edge": "0.275",
                        "total_friction": "0.053",
                        "tradability_score": "0.88",
                        "uncertainty_haircut": "0.20",
                        "portfolio_haircut": "0",
                    },
                },
            },
        ]
        selection = build_city_daily_selection(
            decisions,
            market_day_mode="next_available",
            timezone_by_city={"den": "America/Denver"},
        )
        self.assertEqual(selection["result"], "BET")
        self.assertEqual(selection["best_candidate"]["market_ticker"], "KXHIGHDEN-26APR10-T72")

    def test_build_city_daily_selection_rejects_watch_even_with_positive_math(self) -> None:
        decisions = [
            {
                "city_id": "bos",
                "market_ticker": "KXHIGHTBOS-26APR10-T49",
                "as_of_time": "2026-04-09T21:02:00+00:00",
                "market_date": "2026-04-10",
                "final_decision": "WATCH",
                "regime_summary": {"haircut_value": "0"},
                "risk_summary": {"hard_halt_flag": False},
                "microstructure_summary": {"selected_taker_tradability_score": "0.82"},
                "edge_summary": {
                    "selected_side": "yes",
                    "selected_p_model": "0.35",
                    "selected_p_market_exec": "0.10",
                    "selected_executable_ev": "0.09",
                    "selected_raw_edge": "0.25",
                    "selected_total_friction": "0.05",
                    "selected_uncertainty_haircut": "0.20",
                    "selected_portfolio_haircut": "0",
                    "confidence_summary": {
                        "overall_trade_confidence": "0.62",
                    },
                    "best_taker_candidate": {
                        "side": "yes",
                        "p_model": "0.35",
                        "p_market_exec": "0.10",
                        "executable_ev": "0.09",
                        "raw_edge": "0.25",
                        "total_friction": "0.05",
                        "tradability_score": "0.82",
                        "uncertainty_haircut": "0.20",
                        "portfolio_haircut": "0",
                    },
                },
            },
        ]
        selection = build_city_daily_selection(
            decisions,
            market_day_mode="next_available",
            timezone_by_city={"bos": "America/New_York"},
        )
        self.assertEqual(selection["result"], "SKIP")
        self.assertIn("decision_not_taker_allowed", selection["rejection_reasons"])

    def test_parse_market_date_extracts_threshold_market_day(self) -> None:
        self.assertEqual(str(parse_market_date("KXHIGHNY-26APR10-T61")), "2026-04-10")

    def test_select_latest_market_decisions_keeps_latest_per_city_market(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "M1",
                "as_of_time": "2026-04-06T12:00:00+00:00",
                "final_decision": "WATCH",
            },
            {
                "city_id": "nyc",
                "market_ticker": "M1",
                "as_of_time": "2026-04-06T12:05:00+00:00",
                "final_decision": "TAKER_ALLOWED",
            },
            {
                "city_id": "chi",
                "market_ticker": "M1",
                "as_of_time": "2026-04-06T12:03:00+00:00",
                "final_decision": "NO_TRADE",
            },
        ]
        latest = select_latest_market_decisions(decisions)
        self.assertEqual(len(latest), 2)
        nyc = next(item for item in latest if item["city_id"] == "nyc")
        self.assertEqual(nyc["final_decision"], "TAKER_ALLOWED")

    def test_build_opportunity_board_ranks_tradeable_high_edge_first(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "M1",
                "as_of_time": "2026-04-06T12:05:00+00:00",
                "final_decision": "WATCH",
                "microstructure_summary": {
                    "selected_taker_tradability_score": "0.55",
                },
                "edge_summary": {
                    "selected_side": "yes",
                    "selected_executable_ev": "0.02",
                    "selected_raw_edge": "0.04",
                    "confidence_summary": {
                        "overall_trade_confidence": "0.62",
                    },
                },
            },
            {
                "city_id": "chi",
                "market_ticker": "M2",
                "as_of_time": "2026-04-06T12:06:00+00:00",
                "final_decision": "TAKER_ALLOWED",
                "microstructure_summary": {
                    "selected_taker_tradability_score": "0.63",
                },
                "edge_summary": {
                    "selected_side": "no",
                    "selected_executable_ev": "0.05",
                    "selected_raw_edge": "0.08",
                    "confidence_summary": {
                        "overall_trade_confidence": "0.71",
                    },
                },
            },
        ]
        board = build_opportunity_board(
            decisions,
            qualification_states={"nyc": "SHADOW_ONLY", "chi": "OBSERVE_ONLY"},
        )
        self.assertEqual(board["items"][0]["city_id"], "chi")
        self.assertEqual(board["items"][0]["final_decision"], "TAKER_ALLOWED")
        self.assertEqual(board["items"][0]["qualification_state"], "OBSERVE_ONLY")

    def test_build_opportunity_board_uses_latest_city_cycle_window(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "OLD1",
                "as_of_time": "2026-04-06T10:00:00+00:00",
                "final_decision": "WATCH",
                "microstructure_summary": {},
                "edge_summary": {},
            },
            {
                "city_id": "nyc",
                "market_ticker": "NEW1",
                "as_of_time": "2026-04-06T12:00:00+00:00",
                "final_decision": "WATCH",
                "microstructure_summary": {},
                "edge_summary": {},
            },
            {
                "city_id": "nyc",
                "market_ticker": "NEW2",
                "as_of_time": "2026-04-06T12:04:00+00:00",
                "final_decision": "NO_TRADE",
                "microstructure_summary": {},
                "edge_summary": {},
            },
        ]
        board = build_opportunity_board(decisions, cycle_window_minutes=10)
        tickers = [item["market_ticker"] for item in board["items"]]
        self.assertEqual(tickers, ["NEW1", "NEW2"])

    def test_build_opportunity_board_flags_cross_threshold_inconsistency(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR06-T70",
                "as_of_time": "2026-04-06T12:05:00+00:00",
                "final_decision": "WATCH",
                "path_state": {"p_yes": "0.58"},
                "microstructure_summary": {"best_yes_ask": "0.50"},
                "edge_summary": {"confidence_summary": {}},
            },
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR06-T75",
                "as_of_time": "2026-04-06T12:06:00+00:00",
                "final_decision": "WATCH",
                "path_state": {"p_yes": "0.64"},
                "microstructure_summary": {"best_yes_ask": "0.57"},
                "edge_summary": {"confidence_summary": {}},
            },
        ]
        board = build_opportunity_board(decisions, cycle_window_minutes=10)
        self.assertEqual(board["cross_threshold_model_inconsistency_count"], 2)
        self.assertEqual(board["cross_threshold_market_inconsistency_count"], 2)
        self.assertTrue(board["items"][0]["cross_threshold_model_inconsistency"])
        self.assertTrue(board["items"][0]["cross_threshold_market_inconsistency"])

    def test_select_scannable_market_decisions_filters_to_next_available_day(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR09-T60",
                "as_of_time": "2026-04-09T21:00:00+00:00",
                "final_decision": "TAKER_ALLOWED",
            },
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR10-T61",
                "as_of_time": "2026-04-09T21:01:00+00:00",
                "final_decision": "WATCH",
            },
        ]
        latest = select_scannable_market_decisions(
            decisions,
            market_day_mode="next_available",
            timezone_by_city={"nyc": "America/New_York"},
        )
        self.assertEqual([item["market_ticker"] for item in latest], ["KXHIGHNY-26APR10-T61"])

    def test_select_scannable_market_decisions_filters_to_same_day(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR09-T60",
                "as_of_time": "2026-04-09T21:00:00+00:00",
                "final_decision": "TAKER_ALLOWED",
            },
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR10-T61",
                "as_of_time": "2026-04-09T21:01:00+00:00",
                "final_decision": "WATCH",
            },
        ]
        latest = select_scannable_market_decisions(
            decisions,
            market_day_mode="open_day",
            timezone_by_city={"nyc": "America/New_York"},
        )
        self.assertEqual([item["market_ticker"] for item in latest], ["KXHIGHNY-26APR09-T60"])

    def test_build_opportunity_board_can_exclude_open_cities(self) -> None:
        decisions = [
            {
                "city_id": "nyc",
                "market_ticker": "KXHIGHNY-26APR10-T61",
                "as_of_time": "2026-04-09T21:05:00+00:00",
                "final_decision": "TAKER_ALLOWED",
                "microstructure_summary": {"selected_taker_tradability_score": "0.70"},
                "edge_summary": {
                    "selected_side": "yes",
                    "selected_executable_ev": "0.06",
                    "selected_raw_edge": "0.08",
                    "confidence_summary": {"overall_trade_confidence": "0.70"},
                },
            },
            {
                "city_id": "phl",
                "market_ticker": "KXHIGHPHIL-26APR10-T68",
                "as_of_time": "2026-04-09T21:04:00+00:00",
                "final_decision": "WATCH",
                "microstructure_summary": {"selected_taker_tradability_score": "0.55"},
                "edge_summary": {
                    "selected_side": "yes",
                    "selected_executable_ev": "0.01",
                    "selected_raw_edge": "0.05",
                    "confidence_summary": {"overall_trade_confidence": "0.40"},
                },
            },
        ]
        board = build_opportunity_board(
            decisions,
            market_day_mode="next_available",
            timezone_by_city={
                "nyc": "America/New_York",
                "phl": "America/New_York",
            },
            open_city_ids={"nyc"},
            exclude_open_cities=True,
        )
        self.assertEqual([item["city_id"] for item in board["items"]], ["phl"])
        self.assertEqual(board["excluded_open_city_count"], 1)


if __name__ == "__main__":
    unittest.main()
