"""Schema migration framework.

The original `_initialize` method in state_store.py mixed table creation
with one-off schema fixes via inline `if pk_cols == {'city_id'}:` checks.
That works but: (1) hard to reason about ordering, (2) no audit trail of
what's been applied, (3) easy to forget to drop a migration after it
becomes obsolete.

This module replaces that with a numbered-migration registry. Each
migration is a forward-only operation with a stable id. We record applied
ids in `schema_migrations` and skip already-applied ones on each startup.

Migrations themselves stay as plain SQL strings (or callables for
complex cases) so the simplest possible operational model.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward-only schema change."""

    migration_id: str  # Globally unique. Format: YYYYMMDD_NN_short_description
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "migration_id TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL, "
        "description TEXT NOT NULL"
        ")"
    )


def _applied_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT migration_id FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def run_pending(conn: sqlite3.Connection, migrations: list[Migration]) -> list[str]:
    """Apply any not-yet-applied migrations in order.

    Returns the list of newly-applied migration ids. Idempotent; safe to
    call on every startup.
    """
    from datetime import datetime, timezone
    _ensure_migrations_table(conn)
    already = _applied_ids(conn)
    newly_applied: list[str] = []
    for m in migrations:
        if m.migration_id in already:
            continue
        m.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, applied_at, description) "
            "VALUES (?,?,?)",
            (m.migration_id, datetime.now(timezone.utc).isoformat(), m.description),
        )
        newly_applied.append(m.migration_id)
    return newly_applied


# ───────────────────────────────────────────────────────────────────────
# Migration registry — append-only, never remove or reorder.
# ───────────────────────────────────────────────────────────────────────


def _m_20260517_01_shadow_positions_pk(conn: sqlite3.Connection) -> None:
    """shadow_positions PK was city_id alone; change to (city_id, market_ticker)
    so multi-bracket-per-city positions can coexist."""
    pk_info = conn.execute(
        "SELECT name FROM pragma_table_info('shadow_positions') WHERE pk > 0"
    ).fetchall()
    pk_cols = {row[0] for row in pk_info}
    if pk_cols == {"city_id"}:
        conn.executescript(
            """
            CREATE TABLE shadow_positions_new (
                city_id TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (city_id, market_ticker)
            );
            INSERT OR IGNORE INTO shadow_positions_new
                SELECT city_id, market_ticker, lifecycle_status, payload_json
                FROM shadow_positions;
            DROP TABLE shadow_positions;
            ALTER TABLE shadow_positions_new RENAME TO shadow_positions;
            """
        )


def _m_20260517_02_recommendations_extra_columns(conn: sqlite3.Connection) -> None:
    """Add signal-timing/context columns to market_recommendations."""
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(market_recommendations)")
    }
    new_columns: list[tuple[str, str]] = [
        ("local_time_of_day_hour", "INTEGER"),
        ("minutes_to_settlement_close", "INTEGER"),
        ("window_status", "TEXT"),
        ("current_temp_f", "REAL"),
        ("high_so_far_f", "REAL"),
        ("orderbook_yes_bid", "REAL"),
        ("orderbook_yes_ask", "REAL"),
        ("orderbook_no_bid", "REAL"),
        ("orderbook_no_ask", "REAL"),
        ("tradability_score", "REAL"),
        ("actually_filled", "INTEGER"),
        ("fill_blocker_reason", "TEXT"),
    ]
    for col_name, col_def in new_columns:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE market_recommendations ADD COLUMN {col_name} {col_def}"
            )


MIGRATIONS: list[Migration] = [
    Migration(
        migration_id="20260517_01_shadow_positions_pk",
        description="shadow_positions PK city_id → (city_id, market_ticker)",
        apply=_m_20260517_01_shadow_positions_pk,
    ),
    Migration(
        migration_id="20260517_02_recommendations_extra_columns",
        description="Add signal-timing columns to market_recommendations",
        apply=_m_20260517_02_recommendations_extra_columns,
    ),
]
