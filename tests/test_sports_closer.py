from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from kalshi_weather.analytics.sports_closer import (
    SportsCloserConfig,
    evaluate_event,
    evaluate_market_candidate,
    filter_markets_for_target_date,
    parse_market_date_from_ticker,
)


def _candle(ts: int, *, yes_ask_close: str, yes_bid_close: str) -> dict[str, object]:
    return {
        "end_period_ts": ts,
        "yes_ask": {"close_dollars": yes_ask_close},
        "yes_bid": {"close_dollars": yes_bid_close},
    }


class SportsCloserTest(unittest.TestCase):
    def test_parse_market_date_from_ticker(self) -> None:
        self.assertEqual(parse_market_date_from_ticker("KXNHLGAME-26APR11VANSJ-VAN"), date(2026, 4, 11))

    def test_filter_markets_for_target_date(self) -> None:
        markets = [
            {"ticker": "KXNHLGAME-26APR11VANSJ-VAN"},
            {"ticker": "KXNHLGAME-26APR12VANANA-VAN"},
        ]
        filtered = filter_markets_for_target_date(markets, target_date=date(2026, 4, 11))
        self.assertEqual([item["ticker"] for item in filtered], ["KXNHLGAME-26APR11VANSJ-VAN"])

    def test_evaluate_market_candidate_accepts_yes_entry(self) -> None:
        now = datetime(2026, 4, 11, 16, 0, tzinfo=timezone.utc)
        market = {
            "ticker": "KXNHLGAME-26APR11VANSJ-VAN",
            "title": "Vancouver at San Jose Winner?",
            "yes_ask_dollars": "0.84",
            "yes_bid_dollars": "0.77",
            "no_ask_dollars": "0.23",
            "no_bid_dollars": "0.16",
            "expected_expiration_time": (now + timedelta(minutes=90)).isoformat(),
        }
        candles = [
            _candle(int((now - timedelta(minutes=4)).timestamp()), yes_ask_close="0.81", yes_bid_close="0.18"),
            _candle(int((now - timedelta(minutes=3)).timestamp()), yes_ask_close="0.82", yes_bid_close="0.17"),
            _candle(int((now - timedelta(minutes=2)).timestamp()), yes_ask_close="0.84", yes_bid_close="0.16"),
        ]
        result = evaluate_market_candidate(
            market,
            side="yes",
            candles=candles,
            now=now,
            config=SportsCloserConfig(),
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["skip_reason"], None)
        self.assertEqual(result["side"], "yes")

    def test_evaluate_market_candidate_accepts_no_entry(self) -> None:
        now = datetime(2026, 4, 11, 16, 0, tzinfo=timezone.utc)
        market = {
            "ticker": "KXNHLGAME-26APR11VANSJ-SJ",
            "title": "Vancouver at San Jose Winner?",
            "yes_ask_dollars": "0.16",
            "yes_bid_dollars": "0.12",
            "no_ask_dollars": "0.88",
            "no_bid_dollars": "0.83",
            "expected_expiration_time": (now + timedelta(minutes=75)).isoformat(),
        }
        candles = [
            _candle(int((now - timedelta(minutes=4)).timestamp()), yes_ask_close="0.20", yes_bid_close="0.17"),
            _candle(int((now - timedelta(minutes=3)).timestamp()), yes_ask_close="0.19", yes_bid_close="0.16"),
            _candle(int((now - timedelta(minutes=2)).timestamp()), yes_ask_close="0.18", yes_bid_close="0.15"),
        ]
        result = evaluate_market_candidate(
            market,
            side="no",
            candles=candles,
            now=now,
            config=SportsCloserConfig(),
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["skip_reason"], None)
        self.assertEqual(result["side"], "no")
        self.assertEqual(result["recent_closes"], [Decimal("0.83"), Decimal("0.84"), Decimal("0.85")])

    def test_evaluate_event_prefers_ready_candidate(self) -> None:
        now = datetime(2026, 4, 11, 16, 0, tzinfo=timezone.utc)
        markets = [
            {
                "ticker": "KXNHLGAME-26APR11VANSJ-VAN",
                "event_ticker": "KXNHLGAME-26APR11VANSJ",
                "title": "Vancouver at San Jose Winner?",
                "yes_ask_dollars": "0.84",
                "yes_bid_dollars": "0.77",
                "no_ask_dollars": "0.23",
                "no_bid_dollars": "0.16",
                "expected_expiration_time": (now + timedelta(minutes=90)).isoformat(),
            },
            {
                "ticker": "KXNHLGAME-26APR11VANSJ-SJ",
                "event_ticker": "KXNHLGAME-26APR11VANSJ",
                "title": "Vancouver at San Jose Winner?",
                "yes_ask_dollars": "0.16",
                "yes_bid_dollars": "0.12",
                "no_ask_dollars": "0.88",
                "no_bid_dollars": "0.83",
                "expected_expiration_time": (now + timedelta(minutes=90)).isoformat(),
            },
        ]
        candles_by_market = {
            "KXNHLGAME-26APR11VANSJ-VAN": [
                _candle(int((now - timedelta(minutes=4)).timestamp()), yes_ask_close="0.81", yes_bid_close="0.18"),
                _candle(int((now - timedelta(minutes=3)).timestamp()), yes_ask_close="0.82", yes_bid_close="0.17"),
                _candle(int((now - timedelta(minutes=2)).timestamp()), yes_ask_close="0.84", yes_bid_close="0.16"),
            ],
            "KXNHLGAME-26APR11VANSJ-SJ": [
                _candle(int((now - timedelta(minutes=4)).timestamp()), yes_ask_close="0.20", yes_bid_close="0.17"),
                _candle(int((now - timedelta(minutes=3)).timestamp()), yes_ask_close="0.19", yes_bid_close="0.16"),
                _candle(int((now - timedelta(minutes=2)).timestamp()), yes_ask_close="0.18", yes_bid_close="0.15"),
            ],
        }
        result = evaluate_event(
            "KXNHLGAME-26APR11VANSJ",
            markets,
            candles_by_market=candles_by_market,
            now=now,
            config=SportsCloserConfig(),
        )
        self.assertEqual(result["decision"], "BET")
        self.assertEqual(result["selected_candidate"]["market_ticker"], "KXNHLGAME-26APR11VANSJ-SJ")
        self.assertEqual(result["selected_candidate"]["side"], "no")
        self.assertTrue(result["decision_notes"])
        self.assertEqual(result["best_ready_candidate"]["market_ticker"], "KXNHLGAME-26APR11VANSJ-SJ")

    def test_evaluate_market_candidate_records_skip_notes(self) -> None:
        now = datetime(2026, 4, 11, 13, 0, tzinfo=timezone.utc)
        market = {
            "ticker": "KXNHLGAME-26APR11VANSJ-VAN",
            "title": "Vancouver at San Jose Winner?",
            "yes_ask_dollars": "0.84",
            "yes_bid_dollars": "0.77",
            "no_ask_dollars": "0.23",
            "no_bid_dollars": "0.16",
            "expected_expiration_time": (now + timedelta(minutes=300)).isoformat(),
        }
        result = evaluate_market_candidate(
            market,
            side="yes",
            candles=[],
            now=now,
            config=SportsCloserConfig(),
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["skip_reason"], "too_early_for_closer_entry")
        self.assertTrue(any("too far away" in note for note in result["notes"]))

    def test_evaluate_event_skip_carries_best_blocked_details(self) -> None:
        now = datetime(2026, 4, 11, 13, 0, tzinfo=timezone.utc)
        markets = [
            {
                "ticker": "KXNHLGAME-26APR11VANSJ-VAN",
                "event_ticker": "KXNHLGAME-26APR11VANSJ",
                "title": "Vancouver at San Jose Winner?",
                "yes_ask_dollars": "0.84",
                "yes_bid_dollars": "0.77",
                "no_ask_dollars": "0.23",
                "no_bid_dollars": "0.16",
                "expected_expiration_time": (now + timedelta(minutes=300)).isoformat(),
            },
            {
                "ticker": "KXNHLGAME-26APR11VANSJ-SJ",
                "event_ticker": "KXNHLGAME-26APR11VANSJ",
                "title": "Vancouver at San Jose Winner?",
                "yes_ask_dollars": "0.16",
                "yes_bid_dollars": "0.12",
                "no_ask_dollars": "0.88",
                "no_bid_dollars": "0.83",
                "expected_expiration_time": (now + timedelta(minutes=300)).isoformat(),
            },
        ]
        result = evaluate_event(
            "KXNHLGAME-26APR11VANSJ",
            markets,
            candles_by_market={},
            now=now,
            config=SportsCloserConfig(),
        )
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["skip_reason"], "too_early_for_closer_entry")
        self.assertEqual(result["best_blocked_candidate"]["market_ticker"], "KXNHLGAME-26APR11VANSJ-SJ")
        self.assertTrue(any("dominant blocker" in note for note in result["decision_notes"]))


if __name__ == "__main__":
    unittest.main()
