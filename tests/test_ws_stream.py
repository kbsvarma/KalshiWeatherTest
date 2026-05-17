from __future__ import annotations

from decimal import Decimal
import unittest

from kalshi_weather.clients import SubscriptionSpec, build_subscribe_command
from kalshi_weather.market import (
    OrderbookSequenceGapError,
    apply_orderbook_delta,
    orderbook_snapshot_from_stream_state,
    orderbook_state_from_snapshot,
    trade_snapshot_from_ws_message,
)


class WebSocketStreamTest(unittest.TestCase):
    def test_build_subscribe_command(self) -> None:
        payload = build_subscribe_command(
            SubscriptionSpec(channels=("orderbook_delta",), market_tickers=("M1", "M2")),
            request_id=7,
        )
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["params"]["channels"], ["orderbook_delta"])
        self.assertEqual(payload["params"]["market_tickers"], ["M1", "M2"])

    def test_orderbook_snapshot_and_delta(self) -> None:
        snapshot_payload = {
            "type": "orderbook_snapshot",
            "sid": 11,
            "msg": {
                "market_ticker": "M1",
                "market_id": "123",
                "seq": 5,
                "yes_dollars": [["0.20", "100"], ["0.25", "50"]],
                "no_dollars": [["0.70", "20"]],
            },
        }
        state = orderbook_state_from_snapshot(snapshot_payload)
        delta_payload = {
            "type": "orderbook_delta",
            "sid": 11,
            "msg": {
                "market_ticker": "M1",
                "seq": 6,
                "price_dollars": "0.25",
                "delta_fp": "-10",
                "side": "yes",
                "ts": 1710000000,
            },
        }
        updated = apply_orderbook_delta(state, delta_payload)
        orderbook = orderbook_snapshot_from_stream_state(updated, source_ref="raw1")
        self.assertEqual(orderbook.seq, 6)
        self.assertEqual(orderbook.yes_bids_ladder[-1], (Decimal("0.25"), Decimal("40")))
        self.assertEqual(orderbook.implied_yes_asks_ladder[0][0], Decimal("0.30"))

    def test_orderbook_gap_raises(self) -> None:
        state = orderbook_state_from_snapshot(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "msg": {
                    "market_ticker": "M1",
                    "seq": 4,
                    "yes_dollars": [["0.20", "100"]],
                    "no_dollars": [["0.70", "20"]],
                },
            }
        )
        with self.assertRaises(OrderbookSequenceGapError):
            apply_orderbook_delta(
                state,
                {
                    "type": "orderbook_delta",
                    "sid": 1,
                    "msg": {
                        "market_ticker": "M1",
                        "seq": 7,
                        "price_dollars": "0.20",
                        "delta_fp": "5",
                        "side": "yes",
                    },
                },
            )

    def test_trade_message_parsing(self) -> None:
        trade = trade_snapshot_from_ws_message(
            {
                "type": "trade",
                "sid": 11,
                "msg": {
                    "trade_id": "t1",
                    "market_ticker": "M1",
                    "yes_price_dollars": "0.360",
                    "no_price_dollars": "0.640",
                    "count_fp": "136.00",
                    "taker_side": "no",
                    "ts": 1669149841,
                },
            },
            source_payload_id="raw2",
        )
        self.assertEqual(trade.market_ticker, "M1")
        self.assertEqual(trade.taker_side, "no")
        self.assertEqual(trade.count_fp, Decimal("136.00"))


if __name__ == "__main__":
    unittest.main()
