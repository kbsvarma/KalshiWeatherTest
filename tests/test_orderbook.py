from __future__ import annotations

from decimal import Decimal
import unittest

from kalshi_weather.market.orderbook import derive_implied_ask_ladders, executable_wap_for_buy


class OrderbookMathTest(unittest.TestCase):
    def test_implied_asks_and_wap(self) -> None:
        yes_bids = ((Decimal("0.20"), Decimal("10")),)
        no_bids = (
            (Decimal("0.97"), Decimal("1")),
            (Decimal("0.95"), Decimal("2")),
        )
        implied_yes, _ = derive_implied_ask_ladders(yes_bids, no_bids)
        self.assertEqual(implied_yes[0][0], Decimal("0.03"))
        wap, levels = executable_wap_for_buy(implied_yes, Decimal("2"))
        self.assertEqual(levels, 2)
        self.assertIsNotNone(wap)
        self.assertGreater(wap, Decimal("0.03"))


if __name__ == "__main__":
    unittest.main()
