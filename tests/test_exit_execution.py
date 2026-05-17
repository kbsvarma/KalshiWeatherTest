"""Integration tests for the close-on-EXIT execution path.

The decision engine emits EXIT signals for owned markets when conditions
deteriorate. _maybe_close_if_underwater (in tools/run_city_cycle.py) is
the consumer. These tests verify it:

  - Closes when underwater (top bid < entry price)
  - Holds when in profit (top bid >= entry price)
  - Skips when no live order exists for the market
  - Doesn't double-close if a CLOSED_ row already exists
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch


class _MockOrderbook:
    """Minimal stand-in for OrderbookSnapshot with the fields the close
    helper queries."""

    def __init__(
        self,
        *,
        yes_bids: list[tuple[Decimal, int]] | None = None,
        no_bids: list[tuple[Decimal, int]] | None = None,
    ) -> None:
        self.yes_bids_ladder = yes_bids or []
        self.no_bids_ladder = no_bids or []


def _setup_store_with_live_order(
    tmp_path: str,
    *,
    ticker: str,
    side: str,
    entry_cents: int,
    status: str = "PLACED_EXECUTED",
):
    """Spin up a SQLiteStateStore at tmp_path and seed one live order row."""
    from kalshi_weather.storage.state_store import SQLiteStateStore
    store = SQLiteStateStore(f"{tmp_path}/state.sqlite3")
    payload = {
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "type": "limit",
        ("yes_price" if side == "yes" else "no_price"): entry_cents,
        "count": 1,
        "time_in_force": "immediate_or_cancel",
    }
    record = {
        "record_id": "test_record_1",
        "market_ticker": ticker,
        "created_at": "2026-05-17T03:00:00+00:00",
        "status": status,
        **payload,
    }
    store.save_live_order_record(record)
    # Also create a shadow_position so the rollback step has something to delete
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO shadow_positions "
            "(city_id, market_ticker, lifecycle_status, payload_json) "
            "VALUES (?,?,?,?)",
            ("nola", ticker, "OPEN", json.dumps({})),
        )
    return store


class ExitExecutionTest(unittest.TestCase):
    """Verify _maybe_close_if_underwater behavior on various inputs."""

    def test_holds_when_in_profit(self) -> None:
        """Position bought at 0.62, current bid 0.80 → in profit → hold."""
        os.environ["LIVE_ORDERS_ENABLED"] = "0"  # ensure no real orders
        from kalshi_weather.tools.run_city_cycle import _maybe_close_if_underwater
        with tempfile.TemporaryDirectory() as td:
            store = _setup_store_with_live_order(
                td, ticker="KXHIGHTNOLA-26MAY17-B82.5",
                side="no", entry_cents=62,
            )
            ob = _MockOrderbook(no_bids=[(Decimal("0.80"), 100)])
            _maybe_close_if_underwater(
                state_store=store,
                market_ticker="KXHIGHTNOLA-26MAY17-B82.5",
                orderbook=ob,
                city_id="nola",
            )
            # Should have NOT inserted any CLOSED_* row
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT count(*) FROM live_orders WHERE status LIKE 'CLOSED_%'"
                ).fetchone()
            self.assertEqual(rows[0], 0)

    def test_closes_when_underwater(self) -> None:
        """Position bought at 0.62, current bid 0.40 → underwater → close."""
        os.environ["LIVE_ORDERS_ENABLED"] = "1"
        os.environ["LIVE_ORDERS_DRY_RUN"] = "1"  # dry-run path, no real API
        from kalshi_weather.tools.run_city_cycle import _maybe_close_if_underwater
        with tempfile.TemporaryDirectory() as td:
            store = _setup_store_with_live_order(
                td, ticker="KXHIGHTNOLA-26MAY17-B82.5",
                side="no", entry_cents=62,
            )
            ob = _MockOrderbook(no_bids=[(Decimal("0.40"), 100)])
            _maybe_close_if_underwater(
                state_store=store,
                market_ticker="KXHIGHTNOLA-26MAY17-B82.5",
                orderbook=ob,
                city_id="nola",
            )
            # Dry-run path writes a DRY_RUN_CLOSE_PREVIEW row
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT count(*) FROM live_orders "
                    "WHERE status='DRY_RUN_CLOSE_PREVIEW'"
                ).fetchone()
            self.assertEqual(rows[0], 1)

    def test_skips_when_no_position_exists(self) -> None:
        """No live order for this ticker → nothing to close."""
        os.environ["LIVE_ORDERS_ENABLED"] = "1"
        os.environ["LIVE_ORDERS_DRY_RUN"] = "1"
        from kalshi_weather.tools.run_city_cycle import _maybe_close_if_underwater
        from kalshi_weather.storage.state_store import SQLiteStateStore
        with tempfile.TemporaryDirectory() as td:
            store = SQLiteStateStore(f"{td}/state.sqlite3")
            ob = _MockOrderbook(no_bids=[(Decimal("0.40"), 100)])
            _maybe_close_if_underwater(
                state_store=store,
                market_ticker="KXHIGHTNOLA-26MAY17-B82.5",
                orderbook=ob,
                city_id="nola",
            )
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT count(*) FROM live_orders"
                ).fetchone()
            self.assertEqual(rows[0], 0)

    def test_does_not_double_close(self) -> None:
        """If a CLOSED_* row exists for this ticker today, skip."""
        os.environ["LIVE_ORDERS_ENABLED"] = "1"
        os.environ["LIVE_ORDERS_DRY_RUN"] = "1"
        from kalshi_weather.tools.run_city_cycle import _maybe_close_if_underwater
        with tempfile.TemporaryDirectory() as td:
            store = _setup_store_with_live_order(
                td, ticker="KXHIGHTNOLA-26MAY17-B82.5",
                side="no", entry_cents=62,
            )
            # Seed an existing CLOSED row
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO live_orders (record_id, market_ticker, created_at, status, payload_json) "
                    "VALUES (?,?,?,?,?)",
                    ("close_existing", "KXHIGHTNOLA-26MAY17-B82.5",
                     "2026-05-17T05:00:00+00:00", "CLOSED_EXECUTED",
                     json.dumps({"close_intent": True})),
                )
            ob = _MockOrderbook(no_bids=[(Decimal("0.40"), 100)])
            _maybe_close_if_underwater(
                state_store=store,
                market_ticker="KXHIGHTNOLA-26MAY17-B82.5",
                orderbook=ob,
                city_id="nola",
            )
            # Should NOT have added another close — count stays at 1
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT count(*) FROM live_orders "
                    "WHERE status LIKE 'CLOSED_%' "
                    "OR status='DRY_RUN_CLOSE_PREVIEW'"
                ).fetchone()
            self.assertEqual(rows[0], 1)


if __name__ == "__main__":
    unittest.main()
