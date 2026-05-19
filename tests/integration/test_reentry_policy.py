"""Re-entry policy tests for ``live_execution._check_reentry_policy``.

Replaces the older binary "any same-day touch → block" gate with a
conditional policy that allows re-bet when the model has genuinely new
conviction after an earlier close.

Each scenario is exercised against a fake ``live_orders`` table populated
with one or more synthetic rows; the policy is called directly and the
returned ``ReentryDecision`` is asserted.

The DB schema mirrors what ``SQLiteStateStore`` actually creates, with
just the columns the policy reads (created_at, status, payload_json).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# Make `kalshi_weather` importable.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kalshi_weather.engines.live_execution import (
    REENTRY_MAX_PER_TICKER_PER_DAY,
    REENTRY_MIN_COOLDOWN_MINUTES,
    REENTRY_P_MODEL_BOOST_REQUIRED,
    _check_reentry_policy,
)


# ────────────────────────────────────────────────────────────────────────────
# Fake store — exposes ._connect() returning a sqlite3.Connection. Mirrors
# the SQLiteStateStore interface that _check_reentry_policy uses.
# ────────────────────────────────────────────────────────────────────────────
class _FakeStore:
    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db)


@pytest.fixture
def fake_store(tmp_path: Path) -> _FakeStore:
    db = tmp_path / "runtime.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE live_orders ("
        "  record_id TEXT PRIMARY KEY,"
        "  market_ticker TEXT NOT NULL,"
        "  status TEXT NOT NULL,"
        "  created_at TEXT NOT NULL,"
        "  payload_json TEXT NOT NULL"
        ")"
    )
    con.commit()
    con.close()
    return _FakeStore(db)


def _insert(
    store: _FakeStore,
    *,
    ticker: str,
    status: str,
    minutes_ago: float,
    payload: dict | None = None,
) -> None:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    con = sqlite3.connect(store._db)
    con.execute(
        "INSERT INTO live_orders VALUES (?, ?, ?, ?, ?)",
        (
            f"rec_{ticker}_{status}_{minutes_ago}",
            ticker,
            status,
            created.isoformat(),
            json.dumps(payload or {}),
        ),
    )
    con.commit()
    con.close()


# Use a ticker name that obviously contains today's UTC date — the policy
# scopes by ``substr(created_at, 1, 10) = today_utc``. We just insert rows
# with created_at = now-N min, so the day prefix matches.
T = "KXHIGHMIA-26MAY18-B86.5"
NEW_P = Decimal("0.90")  # plausible new p_model after big regime shift


# ────────────────────────────────────────────────────────────────────────────
# 1. No same-day rows → allow
# ────────────────────────────────────────────────────────────────────────────
def test_allows_first_touch_today(fake_store: _FakeStore) -> None:
    """No prior same-day rows → policy returns no block reason."""
    decision = _check_reentry_policy(fake_store, T, current_p_model=NEW_P)
    assert decision.block_reason is None
    assert decision.is_reentry is False


# ────────────────────────────────────────────────────────────────────────────
# 2. Position currently held (PLACED with no later CLOSED) → block
# ────────────────────────────────────────────────────────────────────────────
def test_blocks_when_position_currently_held(fake_store: _FakeStore) -> None:
    """A PLACED row without a corresponding later CLOSED row means we
    still hold the position. This is duplicate-bet territory, not re-entry."""
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=120,
            payload={"side": "no"})
    decision = _check_reentry_policy(fake_store, T, current_p_model=NEW_P)
    assert decision.block_reason == "market_already_held_today"
    assert decision.is_reentry is False


# ────────────────────────────────────────────────────────────────────────────
# 3. Closed too recently → cooldown
# ────────────────────────────────────────────────────────────────────────────
def test_blocks_within_cooldown_window(fake_store: _FakeStore) -> None:
    """Closed 30 minutes ago — under the 60-minute cooldown → block.

    Even if signal-boost would otherwise allow, cooldown wins.
    """
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=180,
            payload={"side": "no"})
    _insert(fake_store, ticker=T, status="CLOSED_EXECUTED", minutes_ago=30,
            payload={"exit_metadata": {"p_model_at_close": "0.40"}})
    decision = _check_reentry_policy(fake_store, T, current_p_model=NEW_P)
    assert decision.block_reason is not None
    assert "reentry_cooldown" in decision.block_reason
    assert decision.is_reentry is True


# ────────────────────────────────────────────────────────────────────────────
# 4. Past cooldown, signal weak → block
# ────────────────────────────────────────────────────────────────────────────
def test_blocks_after_cooldown_with_weak_signal(fake_store: _FakeStore) -> None:
    """Closed 90 min ago, new p_model only 0.02 above exit → block."""
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=240,
            payload={"side": "no"})
    _insert(fake_store, ticker=T, status="CLOSED_EXECUTED", minutes_ago=90,
            payload={"exit_metadata": {"p_model_at_close": "0.88"}})
    decision = _check_reentry_policy(fake_store, T, current_p_model=Decimal("0.90"))
    assert decision.block_reason is not None
    assert "reentry_signal_too_weak" in decision.block_reason
    assert decision.is_reentry is True


# ────────────────────────────────────────────────────────────────────────────
# 5. Past cooldown + strong signal → allow
# ────────────────────────────────────────────────────────────────────────────
def test_allows_after_cooldown_with_signal_boost(fake_store: _FakeStore) -> None:
    """Closed 90 min ago, new p_model = exit + 0.15 → allow + is_reentry."""
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=240,
            payload={"side": "no"})
    _insert(fake_store, ticker=T, status="CLOSED_EXECUTED", minutes_ago=90,
            payload={"exit_metadata": {"p_model_at_close": "0.75"}})
    decision = _check_reentry_policy(fake_store, T, current_p_model=Decimal("0.90"))
    assert decision.block_reason is None
    assert decision.is_reentry is True


# ────────────────────────────────────────────────────────────────────────────
# 6. Daily re-entry cap exhausted → block
# ────────────────────────────────────────────────────────────────────────────
def test_blocks_second_reentry_same_day(fake_store: _FakeStore) -> None:
    """Already used the per-ticker re-entry budget for the day → block.

    Offsets kept under 4 hours so the synthesized rows stay within
    today's UTC day even when the test runs close to UTC midnight.
    The original offsets (5h back) caused this test to flake whenever it
    ran between 00:00 and 05:00 UTC because the oldest rows crossed
    yesterday's UTC date and got filtered out by the policy's
    ``substr(created_at,1,10) = today_utc`` clause.
    """
    # If we're in the first 4 hours of a UTC day, the offsets below would
    # still cross midnight. Skip cleanly — production code is correct, the
    # test fixture just can't construct a valid scenario in that window.
    from datetime import datetime, timezone
    utc_hour = datetime.now(timezone.utc).hour
    if utc_hour < 4:
        pytest.skip(
            f"Skipping during UTC {utc_hour}:00–04:00 — synthetic timestamps "
            f"would cross yesterday's UTC date and be filtered by the policy. "
            f"Production semantics are 'same UTC day'; this is a test-fixture limitation."
        )
    # Open
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=210,
            payload={"side": "no"})
    # Closed
    _insert(fake_store, ticker=T, status="CLOSED_EXECUTED", minutes_ago=180,
            payload={"exit_metadata": {"p_model_at_close": "0.5"}})
    # Re-entered (this fills the daily cap)
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=120,
            payload={"side": "no", "is_reentry": True})
    # Closed again — must be > 60 min ago so the cooldown check passes and
    # we actually reach the cap-check that this test is exercising.
    _insert(fake_store, ticker=T, status="CLOSED_EXECUTED", minutes_ago=75,
            payload={"exit_metadata": {"p_model_at_close": "0.60"}})
    decision = _check_reentry_policy(fake_store, T, current_p_model=Decimal("0.90"))
    assert decision.block_reason is not None
    assert "reentry_cap_reached" in decision.block_reason
    assert decision.is_reentry is True


# ────────────────────────────────────────────────────────────────────────────
# 7. Legacy CLOSED row with no exit_metadata → conservative block
# ────────────────────────────────────────────────────────────────────────────
def test_blocks_when_exit_metadata_missing(fake_store: _FakeStore) -> None:
    """Legacy CLOSED row predating this policy (no exit_metadata) → block.

    Conservative default: we don't know what the model thought at close,
    so we don't know if the new signal beats it. Refuse re-entry.
    """
    _insert(fake_store, ticker=T, status="PLACED_EXECUTED", minutes_ago=600,
            payload={"side": "no"})
    _insert(fake_store, ticker=T, status="CLOSED_EXECUTED", minutes_ago=120,
            payload={})  # ← no exit_metadata
    decision = _check_reentry_policy(fake_store, T, current_p_model=Decimal("0.99"))
    assert decision.block_reason == "reentry_blocked_legacy_close_no_metadata"
    assert decision.is_reentry is True


# ────────────────────────────────────────────────────────────────────────────
# 8. DRY_RUN rows are invisible to the policy (existing semantics)
# ────────────────────────────────────────────────────────────────────────────
def test_dry_run_rows_do_not_count(fake_store: _FakeStore) -> None:
    """DRY_RUN_PREVIEW, DRY_RUN, DRY_RUN_CLOSE_PREVIEW are not 'real touches'."""
    for status in ("DRY_RUN_PREVIEW", "DRY_RUN", "DRY_RUN_CLOSE_PREVIEW"):
        _insert(fake_store, ticker=T, status=status, minutes_ago=30,
                payload={"side": "no"})
    decision = _check_reentry_policy(fake_store, T, current_p_model=NEW_P)
    assert decision.block_reason is None
    assert decision.is_reentry is False


# ────────────────────────────────────────────────────────────────────────────
# 9. Constants are sane and consistent
# ────────────────────────────────────────────────────────────────────────────
def test_reentry_constants_are_sane() -> None:
    """Pin the parameters so accidental changes surface in CI."""
    assert REENTRY_MIN_COOLDOWN_MINUTES >= 15, (
        "cooldown < 15 min defeats the purpose — allows hot-loop ping-pong"
    )
    assert REENTRY_MIN_COOLDOWN_MINUTES <= 240, (
        "cooldown > 4 hours is overly restrictive; reconsider before raising"
    )
    assert REENTRY_P_MODEL_BOOST_REQUIRED >= Decimal("0.05"), (
        "boost requirement < 5pp is barely above noise; reconsider"
    )
    assert REENTRY_P_MODEL_BOOST_REQUIRED <= Decimal("0.30"), (
        "boost requirement > 30pp will almost never allow re-entry"
    )
    assert 1 <= REENTRY_MAX_PER_TICKER_PER_DAY <= 3, (
        "daily re-entry cap should be in [1, 3]"
    )
