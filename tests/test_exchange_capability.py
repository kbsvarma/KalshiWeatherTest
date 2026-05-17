from __future__ import annotations

import unittest

from kalshi_weather.checks.exchange_capability import (
    orderbook_doc_has_required_tokens,
    parse_fee_change_count,
    parse_series_metadata,
)


ORDERBOOK_DOC_SAMPLE = """
orderbook_snapshot
"seq"
"market_ticker"
"orderbook_delta"
"price_dollars"
"delta_fp"
"side"
"ts"
"""


class ExchangeCapabilityTest(unittest.TestCase):
    def test_orderbook_doc_token_check(self) -> None:
        self.assertTrue(orderbook_doc_has_required_tokens(ORDERBOOK_DOC_SAMPLE))
        self.assertFalse(orderbook_doc_has_required_tokens("orderbook_snapshot only"))

    def test_parse_series_metadata(self) -> None:
        fee_type, fee_multiplier = parse_series_metadata(
            {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
        )
        self.assertEqual(fee_type, "quadratic")
        self.assertEqual(fee_multiplier, 1)

    def test_parse_fee_change_count(self) -> None:
        self.assertEqual(parse_fee_change_count({"series_fee_change_arr": []}), 0)
        self.assertEqual(parse_fee_change_count({"series_fee_change_arr": [{}, {}]}), 2)


if __name__ == "__main__":
    unittest.main()
