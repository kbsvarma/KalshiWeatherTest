"""Status-filter conventions for ``live_orders`` queries — catch
inconsistent filters across scripts and modules.

The ``live_orders`` table contains rows with these statuses (today):

  PLACED_EXECUTED   — real fill, currently open
  PLACED_RESTING    — real fill, sitting in the orderbook  (uncommon)
  PLACED_CANCELED   — real fill, then exchange-side canceled
  CLOSED_EXECUTED   — a SEPARATE row written when we sell to close a position
  CLOSED_CANCELED   — close-action that got canceled
  BLOCKED           — gate rejected, never sent to exchange
  DRY_RUN           — preview mode, never sent
  DRY_RUN_PREVIEW   — older naming, same semantics as DRY_RUN
  ERROR             — exchange returned an error

Audit 2026-05-18 found three different filter conventions in production
code, all for the same logical concept ("real placed orders today"):

  * ``status='PLACED_EXECUTED'``  → too narrow, misses other PLACED_* states
  * ``status LIKE 'PLACED_%'``    → correct for "placed (any state)"
  * (no filter at all)            → counts BLOCKED/DRY_RUN as live, wrong

Plus a missed subtlety: when the bot CLOSES a position via a sell-to-close
order, it INSERTS a new ``CLOSED_*`` row rather than UPDATING the original.
So ``status='PLACED_EXECUTED'`` returns markets that have been closed too,
unless the query also excludes them via ``NOT IN (CLOSED_*)``.

These tests enforce the convention, end-to-end, against a synthetic DB
with each status type represented.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_live_orders_db(tmp_path: Path) -> Path:
    """A SQLite DB matching ``live_orders``'s schema, populated with one row
    of each status type.

    Row count by status:
      PLACED_EXECUTED  : 3   (one of which is later closed)
      PLACED_RESTING   : 1
      CLOSED_EXECUTED  : 1   (the closing leg of one of the placed rows)
      BLOCKED          : 1
      DRY_RUN          : 1

    Truth table:
      "real placed orders today"           = 4 (PLACED_* x 3, RESTING x 1)
      "currently open (not yet closed)"    = 3 (PLACED minus one closed)
      "money deployed today (any state)"   = 5 (PLACED + CLOSED legs)
    """
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
    today = "2026-05-18"
    rows = [
        ("p1", "KXHIGHATL-26MAY18-B86.5",  "PLACED_EXECUTED", f"{today}T10:00:00", "{}"),
        ("p2", "KXHIGHMIA-26MAY18-B88.5",  "PLACED_EXECUTED", f"{today}T11:00:00", "{}"),
        ("p3", "KXHIGHHOU-26MAY18-B90.5",  "PLACED_EXECUTED", f"{today}T12:00:00", "{}"),
        ("p4", "KXHIGHNYC-26MAY18-B82.5",  "PLACED_RESTING",  f"{today}T13:00:00", "{}"),
        # p2 was closed at 11:30
        ("c1", "KXHIGHMIA-26MAY18-B88.5",  "CLOSED_EXECUTED", f"{today}T11:30:00", "{}"),
        ("b1", "KXHIGHDAL-26MAY18-B85.5",  "BLOCKED",         f"{today}T10:15:00", "{}"),
        ("d1", "KXHIGHSAT-26MAY18-T91",    "DRY_RUN",         f"{today}T09:00:00", "{}"),
    ]
    con.executemany(
        "INSERT INTO live_orders VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return db


# ────────────────────────────────────────────────────────────────────────────
# Concept: "real placed orders today" — bug 8
# ────────────────────────────────────────────────────────────────────────────
def test_real_placed_count_excludes_blocked_and_dry_run(fake_live_orders_db: Path) -> None:
    """``status LIKE 'PLACED_%'`` is the canonical filter for "real placed".

    Catches: ``write_cycle_report.sh`` (bug 8) used no status filter on its
    ``live_in_window`` count, inflating the daily report by including
    BLOCKED and DRY_RUN rows.
    """
    con = sqlite3.connect(fake_live_orders_db)
    placed = con.execute(
        "SELECT COUNT(*) FROM live_orders WHERE status LIKE 'PLACED_%'"
    ).fetchone()[0]
    all_rows = con.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0]
    con.close()
    assert placed == 4, f"expected 4 real-placed rows, got {placed}"
    # If filter is missing, count would be 7 (incl BLOCKED + DRY_RUN + CLOSED).
    assert all_rows == 7
    assert placed != all_rows, "no-filter would inflate the count"


def test_write_cycle_report_filters_status() -> None:
    """The shell script must include ``AND status LIKE 'PLACED_%'`` on the
    ``live_in_window`` SQL.
    """
    text = (REPO_ROOT / "scripts" / "write_cycle_report.sh").read_text()
    # The line must include both the window check and the status filter.
    assert "live_in_window" in text
    # Locate the live_in_window assignment and check it has the filter.
    for line in text.splitlines():
        if "live_in_window=" in line and "SELECT count(*)" in line:
            assert "status LIKE 'PLACED_%'" in line, (
                "write_cycle_report.sh's live_in_window SQL must filter "
                "status LIKE 'PLACED_%' to exclude BLOCKED/DRY_RUN/ERROR rows."
            )
            break
    else:
        pytest.fail("could not find live_in_window SQL assignment in write_cycle_report.sh")


# ────────────────────────────────────────────────────────────────────────────
# Concept: "currently open (placed but not yet closed)" — bug 9
# ────────────────────────────────────────────────────────────────────────────
def test_currently_open_excludes_already_closed_markets(fake_live_orders_db: Path) -> None:
    """When a market has BOTH a PLACED_* and a CLOSED_* row, it's NOT open.

    Catches: dashboard.py (bug 9) used ``status='PLACED_EXECUTED'`` which
    over-counted markets that had been closed via a subsequent sell.
    The fix adds ``AND market_ticker NOT IN (SELECT market_ticker FROM
    live_orders WHERE status LIKE 'CLOSED_%')``.
    """
    con = sqlite3.connect(fake_live_orders_db)
    naive = con.execute(
        "SELECT COUNT(*) FROM live_orders WHERE status='PLACED_EXECUTED'"
    ).fetchone()[0]
    correct = con.execute(
        "SELECT COUNT(*) FROM live_orders "
        "WHERE status='PLACED_EXECUTED' "
        "AND market_ticker NOT IN ("
        "  SELECT market_ticker FROM live_orders WHERE status LIKE 'CLOSED_%'"
        ")"
    ).fetchone()[0]
    con.close()
    assert naive == 3, f"naive query returns 3 (one is already closed), got {naive}"
    assert correct == 2, f"closed-aware query returns 2 (the truly open), got {correct}"
    assert naive > correct, "filter must exclude markets with CLOSED_* legs"


def test_dashboard_open_position_queries_use_closed_exclusion() -> None:
    """All three dashboard.py queries displaying "currently open" must use
    the ``NOT IN (SELECT ... CLOSED_*)`` exclusion.

    Heuristic: look at executable code only (strip ``# comment`` portions),
    and for each line that filters ``status='PLACED_EXECUTED'`` in a SQL
    string, the next 10 lines must contain the closed-exclusion subquery.
    Comment lines that document the bug shouldn't trip the check.
    """
    text = (REPO_ROOT / "scripts" / "dashboard.py").read_text()
    expected_clause = "SELECT market_ticker FROM live_orders WHERE status LIKE 'CLOSED_%'"
    lines = text.splitlines()

    def _is_code(idx: int) -> bool:
        # The pattern lives inside a SQL string ``"... status='PLACED_EXECUTED' ..."``
        # so the surrounding char must be a ``"``. If the line is purely a
        # comment (starts with optional whitespace + ``#``) or contains the
        # phrase outside quotes (like in a docstring describing the bug),
        # skip it.
        line = lines[idx]
        stripped = line.lstrip()
        if stripped.startswith("#"):
            return False
        # Comment-citation in a Python comment: line has both `#` and the
        # pattern, and the pattern appears AFTER the `#`.
        if "#" in line and line.index("#") < line.find("status='PLACED_EXECUTED'"):
            return False
        # The literal must appear inside a quoted string to be executable SQL.
        return '"' in line or "'" in line

    found_executable = False
    for i, line in enumerate(lines):
        if "status='PLACED_EXECUTED'" not in line:
            continue
        if not _is_code(i):
            continue
        found_executable = True
        window = "\n".join(lines[i : i + 10])
        assert expected_clause in window, (
            f"dashboard.py line {i+1} filters status='PLACED_EXECUTED' "
            f"in executable SQL but doesn't exclude markets with CLOSED_* "
            f"legs in the next 10 lines. This over-counts closed positions."
        )
    assert found_executable, (
        "expected at least one executable status='PLACED_EXECUTED' query in dashboard.py"
    )


# ────────────────────────────────────────────────────────────────────────────
# Cross-consumer consistency — bug 10
# ────────────────────────────────────────────────────────────────────────────
def test_render_report_uses_status_filter_for_placed_counts() -> None:
    """render_report.sh's "placed today" counts must use LIKE 'PLACED_%'."""
    text = (REPO_ROOT / "scripts" / "render_report.sh").read_text()
    # All COUNT(*) FROM live_orders queries that represent "real placed"
    # must include the filter.
    assert "status LIKE 'PLACED_%'" in text, (
        "render_report.sh must filter status LIKE 'PLACED_%' on placed-order counts."
    )
