from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from kalshi_weather.domain.models import OrderbookSnapshot, TradeSnapshot
from kalshi_weather.engines.microstructure import assess_taker_side


class MicrostructureTest(unittest.TestCase):
    def test_assess_taker_side_uses_recent_trades(self) -> None:
        now = datetime.now(timezone.utc)
        snapshot = OrderbookSnapshot(
            market_ticker="M1",
            as_of_time=now,
            seq=1,
            yes_bids_ladder=((Decimal("0.45"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.55"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.45"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.55"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )
        older = OrderbookSnapshot(
            market_ticker="M1",
            as_of_time=now - timedelta(seconds=30),
            seq=0,
            yes_bids_ladder=((Decimal("0.45"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.55"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.45"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.55"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob0",),
        )
        trades = [
            TradeSnapshot(
                trade_id="t1",
                market_ticker="M1",
                created_time=now - timedelta(seconds=20),
                count_fp=Decimal("2"),
                yes_price_dollars=Decimal("0.46"),
                no_price_dollars=Decimal("0.54"),
                taker_side="no",
                source_payload_id="tr1",
            ),
            TradeSnapshot(
                trade_id="t2",
                market_ticker="M1",
                created_time=now - timedelta(seconds=10),
                count_fp=Decimal("3"),
                yes_price_dollars=Decimal("0.45"),
                no_price_dollars=Decimal("0.55"),
                taker_side="no",
                source_payload_id="tr2",
            ),
        ]
        micro, tradability = assess_taker_side(
            snapshot=snapshot,
            recent_history=[snapshot, older],
            recent_trades=trades,
            market_close_time=now + timedelta(hours=1),
            side="yes",
            quantity=Decimal("1"),
        )
        self.assertGreater(micro.maker_fill_probability, Decimal("0"))
        self.assertTrue(tradability.allowed_maker_flag)

    def test_time_to_close_modulates_taker_adverse_selection(self) -> None:
        now = datetime.now(timezone.utc)
        snapshot = OrderbookSnapshot(
            market_ticker="M1",
            as_of_time=now,
            seq=1,
            yes_bids_ladder=((Decimal("0.45"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.55"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.45"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.55"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )
        trades = [
            TradeSnapshot(
                trade_id="t1",
                market_ticker="M1",
                created_time=now - timedelta(seconds=20),
                count_fp=Decimal("2"),
                yes_price_dollars=Decimal("0.46"),
                no_price_dollars=Decimal("0.54"),
                taker_side="yes",
                source_payload_id="tr1",
            )
        ]
        far_micro, _ = assess_taker_side(
            snapshot=snapshot,
            recent_history=[snapshot],
            recent_trades=trades,
            market_close_time=now + timedelta(hours=6),
            side="yes",
            quantity=Decimal("1"),
        )
        near_micro, near_tradability = assess_taker_side(
            snapshot=snapshot,
            recent_history=[snapshot],
            recent_trades=trades,
            market_close_time=now + timedelta(minutes=10),
            side="yes",
            quantity=Decimal("1"),
        )
        self.assertGreater(far_micro.taker_adverse_selection_penalty, near_micro.taker_adverse_selection_penalty)
        self.assertLess(near_tradability.tradability_score, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
