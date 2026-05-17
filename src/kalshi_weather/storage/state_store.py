from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from kalshi_weather.domain.enums import (
    DecisionType,
    QualificationState,
    ReportStatus,
    RiskState,
    RunMode,
    SettlementValidationStatus,
)
from kalshi_weather.domain.models import (
    CityQualificationState,
    ForecastSnapshot,
    MarketSnapshot,
    ObservationSnapshot,
    OrderbookSnapshot,
    ShadowFill,
    ShadowPosition,
    StrategyDecisionExplanation,
    TradeSnapshot,
)
from kalshi_weather.utils.serde import to_jsonable


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


class SQLiteStateStore:
    def __init__(self, path: Path | str = "data/state/runtime.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    station_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    ingest_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (station_id, event_time)
                );
                CREATE TABLE IF NOT EXISTS forecasts (
                    provider_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    provider_run_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (provider_id, station_id, provider_run_time)
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    market_ticker TEXT NOT NULL,
                    updated_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (market_ticker, updated_time)
                );
                CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                    market_ticker TEXT NOT NULL,
                    as_of_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (market_ticker, as_of_time)
                );
                CREATE TABLE IF NOT EXISTS market_trades (
                    trade_id TEXT PRIMARY KEY,
                    market_ticker TEXT NOT NULL,
                    created_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    city_id TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    as_of_time TEXT NOT NULL,
                    final_decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_positions (
                    city_id TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (city_id, market_ticker)
                );
                CREATE TABLE IF NOT EXISTS shadow_position_history (
                    snapshot_id TEXT PRIMARY KEY,
                    city_id TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_fills (
                    shadow_fill_id TEXT PRIMARY KEY,
                    city_id TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    fill_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlement_validations (
                    validation_id TEXT PRIMARY KEY,
                    city_id TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    matched INTEGER NOT NULL,
                    critical_mismatch INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_orders (
                    record_id TEXT PRIMARY KEY,
                    market_ticker TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qualification_states (
                    city_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kill_switches (
                    scope TEXT NOT NULL,
                    city_id TEXT,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (scope, city_id)
                );
                /* Daily market settlements: actual high/low temp per
                   city+date, joined back to decisions for counterfactual P&L.
                   Populated by tools/fetch_settlements.py from NWS CLI feed. */
                CREATE TABLE IF NOT EXISTS market_settlements (
                    settlement_id TEXT PRIMARY KEY,           -- city_id + local_date
                    city_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,                  -- ISO YYYY-MM-DD
                    daily_high_f REAL,
                    daily_low_f REAL,
                    source TEXT NOT NULL,                      -- "NWS_CLI", "ASOS_CALC", "NCEI"
                    source_payload_id TEXT,
                    fetched_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                /* Every decision recorded as a "recommendation" — even WATCH/NO_TRADE
                   bets. Lets us compute counterfactual P&L on bets we didn't take. */
                CREATE TABLE IF NOT EXISTS market_recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    city_id TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    settlement_variable TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    threshold_f REAL,
                    as_of_time TEXT NOT NULL,
                    recommendation_kind TEXT NOT NULL,   -- "BET" | "WATCH_RECOMMEND" | "SKIP"
                    recommended_side TEXT,                -- "yes" | "no" | None
                    model_p_yes REAL,
                    market_price REAL,
                    raw_edge REAL,
                    executable_ev REAL,
                    provider_spread_f REAL,
                    rejection_reasons TEXT,               -- comma-separated tags
                    counterfactual_resolved INTEGER NOT NULL DEFAULT 0,
                    counterfactual_won INTEGER,           -- nullable until settlement
                    counterfactual_pnl_usd REAL,          -- nullable until settlement
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recommendations_market_date
                    ON market_recommendations(market_ticker, as_of_time);
                CREATE INDEX IF NOT EXISTS idx_recommendations_unresolved
                    ON market_recommendations(counterfactual_resolved, as_of_time);
                CREATE INDEX IF NOT EXISTS idx_settlements_city_date
                    ON market_settlements(city_id, local_date);
                """
            )
            # Schema migration: add signal-timing/context columns to
            # market_recommendations. Safe to run multiple times; ALTER TABLE
            # ADD COLUMN is idempotent if we check for column existence.
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
                ("threshold_gap_f_signed", "REAL"),
                ("orderbook_yes_bid", "REAL"),
                ("orderbook_yes_ask", "REAL"),
                ("orderbook_spread", "REAL"),
                ("tradability_score", "REAL"),
                ("actually_filled", "INTEGER NOT NULL DEFAULT 0"),
                ("fill_blocker_reason", "TEXT"),
                ("settlement_close_time_utc", "TEXT"),
                ("realized_max_high_f", "REAL"),
                ("realized_min_low_f", "REAL"),
            ]
            for col_name, col_def in new_columns:
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE market_recommendations ADD COLUMN {col_name} {col_def}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendations_window_status "
                "ON market_recommendations(window_status, as_of_time)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recommendations_minutes_to_close "
                "ON market_recommendations(minutes_to_settlement_close)"
            )

            # 2026-05-17 migration: shadow_positions primary key was originally
            # `city_id` alone, which caused multi-bracket bets in the same
            # city to OVERWRITE earlier brackets — making the "max 3 markets
            # per city per day" cap effectively a no-op. The CREATE TABLE
            # above already has the new (city_id, market_ticker) compound key
            # for fresh DBs, but existing DBs need a migration.
            pk_info = conn.execute(
                "SELECT name FROM pragma_table_info('shadow_positions') WHERE pk > 0"
            ).fetchall()
            pk_cols = {row[0] for row in pk_info}
            if pk_cols == {"city_id"}:
                # Old schema detected — migrate.
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

    def save_observations(self, records: list[ObservationSnapshot]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO observations (station_id, event_time, ingest_time, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        record.station_id,
                        record.event_time.isoformat(),
                        record.ingest_time.isoformat(),
                        json.dumps(to_jsonable(record), sort_keys=True),
                    )
                    for record in records
                ],
            )

    def get_recent_observations(
        self, station_id: str, limit: int = 4
    ) -> list[ObservationSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM observations
                WHERE station_id = ?
                ORDER BY event_time DESC
                LIMIT ?
                """,
                (station_id, limit),
            ).fetchall()
        return [self._observation_from_json(json.loads(row[0])) for row in rows]

    def save_forecasts(self, records: list[ForecastSnapshot]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO forecasts (provider_id, station_id, provider_run_time, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        record.provider_id,
                        str(record.provider_metadata.get("station_id") or ""),
                        record.provider_run_time.isoformat(),
                        json.dumps(to_jsonable(record), sort_keys=True),
                    )
                    for record in records
                ],
            )

    def get_latest_forecasts(self, station_id: str) -> list[ForecastSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT provider_id, payload_json
                FROM forecasts
                WHERE station_id = ?
                ORDER BY provider_run_time DESC
                """,
                (station_id,),
            ).fetchall()
        seen: set[str] = set()
        results: list[ForecastSnapshot] = []
        for provider_id, payload in rows:
            if provider_id in seen:
                continue
            seen.add(provider_id)
            results.append(self._forecast_from_json(json.loads(payload)))
        return results

    def get_all_forecasts(self, station_id: str) -> list[ForecastSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM forecasts
                WHERE station_id = ?
                ORDER BY provider_run_time ASC
                """,
                (station_id,),
            ).fetchall()
        return [self._forecast_from_json(json.loads(row[0])) for row in rows]

    def save_market_snapshots(self, records: list[MarketSnapshot]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO market_snapshots (market_ticker, updated_time, payload_json)
                VALUES (?, ?, ?)
                """,
                [
                    (
                        record.market_ticker,
                        record.updated_time.isoformat(),
                        json.dumps(to_jsonable(record), sort_keys=True),
                    )
                    for record in records
                ],
            )

    def save_orderbook_snapshot(self, record: OrderbookSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orderbook_snapshots (market_ticker, as_of_time, payload_json)
                VALUES (?, ?, ?)
                """,
                (
                    record.market_ticker,
                    record.as_of_time.isoformat(),
                    json.dumps(to_jsonable(record), sort_keys=True),
                ),
            )

    def get_recent_orderbook_snapshots(
        self, market_ticker: str, limit: int = 10
    ) -> list[OrderbookSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM orderbook_snapshots
                WHERE market_ticker = ?
                ORDER BY as_of_time DESC
                LIMIT ?
                """,
                (market_ticker, limit),
            ).fetchall()
        return [self._orderbook_from_json(json.loads(row[0])) for row in rows]

    def get_all_orderbook_snapshots(self, market_ticker: str) -> list[OrderbookSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM orderbook_snapshots
                WHERE market_ticker = ?
                ORDER BY as_of_time ASC
                """,
                (market_ticker,),
            ).fetchall()
        return [self._orderbook_from_json(json.loads(row[0])) for row in rows]

    def save_trade_snapshots(self, records: list[TradeSnapshot]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO market_trades (trade_id, market_ticker, created_time, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        record.trade_id,
                        record.market_ticker,
                        record.created_time.isoformat(),
                        json.dumps(to_jsonable(record), sort_keys=True),
                    )
                    for record in records
                ],
            )

    def get_recent_trade_snapshots(self, market_ticker: str, limit: int = 50) -> list[TradeSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM market_trades
                WHERE market_ticker = ?
                ORDER BY created_time DESC
                LIMIT ?
                """,
                (market_ticker, limit),
            ).fetchall()
        return [self._trade_from_json(json.loads(row[0])) for row in rows]

    def get_all_trade_snapshots(self, market_ticker: str) -> list[TradeSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM market_trades
                WHERE market_ticker = ?
                ORDER BY created_time ASC
                """,
                (market_ticker,),
            ).fetchall()
        return [self._trade_from_json(json.loads(row[0])) for row in rows]

    def get_all_observations(self, station_id: str) -> list[ObservationSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM observations
                WHERE station_id = ?
                ORDER BY event_time DESC
                """,
                (station_id,),
            ).fetchall()
        return [self._observation_from_json(json.loads(row[0])) for row in rows]

    def save_decision(self, explanation: StrategyDecisionExplanation) -> None:
        payload = explanation.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO decisions (
                    decision_id, city_id, market_ticker, as_of_time, final_decision, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    explanation.decision_id,
                    explanation.city_id,
                    explanation.market_ticker,
                    explanation.as_of_time.isoformat(),
                    explanation.final_decision.value,
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def save_shadow_fill(self, city_id: str, fill: ShadowFill) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO shadow_fills (
                    shadow_fill_id, city_id, market_ticker, fill_time, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    fill.shadow_fill_id,
                    city_id,
                    fill.market_ticker,
                    fill.fill_time.isoformat(),
                    json.dumps(to_jsonable(fill), sort_keys=True),
                ),
            )

    def delete_shadow_fill(self, shadow_fill_id: str) -> None:
        """Roll back a shadow_fill row — used when live execution gets
        blocked AFTER the shadow was recorded, so DB doesn't claim a
        position we don't actually hold."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM shadow_fills WHERE shadow_fill_id = ?",
                (shadow_fill_id,),
            )

    def delete_shadow_position(self, city_id: str, market_ticker: str) -> None:
        """Roll back a shadow_position row — paired with delete_shadow_fill."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM shadow_positions WHERE city_id = ? AND market_ticker = ?",
                (city_id, market_ticker),
            )

    def list_decision_payloads(self, city_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM decisions"
        params: tuple[Any, ...] = ()
        if city_id:
            query += " WHERE city_id = ?"
            params = (city_id,)
        query += " ORDER BY as_of_time DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_shadow_fill_payloads(self, city_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM shadow_fills"
        params: tuple[Any, ...] = ()
        if city_id:
            query += " WHERE city_id = ?"
            params = (city_id,)
        query += " ORDER BY fill_time DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_shadow_fills(
        self,
        city_id: str | None = None,
        market_ticker: str | None = None,
    ) -> list[ShadowFill]:
        query = "SELECT payload_json FROM shadow_fills"
        clauses: list[str] = []
        params: list[Any] = []
        if city_id:
            clauses.append("city_id = ?")
            params.append(city_id)
        if market_ticker:
            clauses.append("market_ticker = ?")
            params.append(market_ticker)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY fill_time DESC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._shadow_fill_from_json(json.loads(row[0])) for row in rows]

    def get_shadow_position(self, city_id: str) -> ShadowPosition | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM shadow_positions
                WHERE city_id = ?
                """,
                (city_id,),
            ).fetchone()
        if row is None:
            return None
        return self._shadow_position_from_json(json.loads(row[0]))

    def save_shadow_position(self, position: ShadowPosition) -> None:
        recorded_at = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(to_jsonable(position), sort_keys=True)
        history_payload = dict(to_jsonable(position))
        history_payload["recorded_at"] = recorded_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO shadow_positions (city_id, market_ticker, lifecycle_status, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    position.city_id,
                    position.market_ticker,
                    position.lifecycle_status,
                    payload_json,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO shadow_position_history (
                    snapshot_id, city_id, market_ticker, lifecycle_status, recorded_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{position.city_id}:{position.market_ticker}:{recorded_at}",
                    position.city_id,
                    position.market_ticker,
                    position.lifecycle_status,
                    recorded_at,
                    json.dumps(history_payload, sort_keys=True),
                ),
            )

    def list_shadow_position_payloads(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM shadow_positions
                ORDER BY city_id ASC
                """
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_shadow_positions(self) -> list[ShadowPosition]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM shadow_positions
                ORDER BY city_id ASC
                """
            ).fetchall()
        return [self._shadow_position_from_json(json.loads(row[0])) for row in rows]

    def list_shadow_position_history_payloads(self, city_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM shadow_position_history"
        params: tuple[Any, ...] = ()
        if city_id:
            query += " WHERE city_id = ?"
            params = (city_id,)
        query += " ORDER BY recorded_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_settlement_validation_record(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO settlement_validations (
                    validation_id, city_id, market_ticker, local_date, matched, critical_mismatch, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["validation_id"]),
                    str(payload["city_id"]),
                    str(payload["market_ticker"]),
                    str(payload["local_date"]),
                    1 if bool(payload.get("matched")) else 0,
                    1 if bool(payload.get("critical_mismatch")) else 0,
                    json.dumps(to_jsonable(payload), sort_keys=True),
                ),
            )

    def list_settlement_validation_payloads(self, city_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM settlement_validations"
        params: tuple[Any, ...] = ()
        if city_id:
            query += " WHERE city_id = ?"
            params = (city_id,)
        query += " ORDER BY local_date DESC, market_ticker ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_settlement_validation_payload(
        self,
        *,
        city_id: str,
        market_ticker: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM settlement_validations
                WHERE city_id = ? AND market_ticker = ?
                """,
                (city_id, market_ticker),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def clear_settlement_validation_records(self, city_id: str | None = None) -> None:
        query = "DELETE FROM settlement_validations"
        params: tuple[Any, ...] = ()
        if city_id:
            query += " WHERE city_id = ?"
            params = (city_id,)
        with self._connect() as conn:
            conn.execute(query, params)

    def save_live_order_record(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO live_orders (record_id, market_ticker, created_at, status, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(payload["record_id"]),
                    str(payload["market_ticker"]),
                    str(payload["created_at"]),
                    str(payload["status"]),
                    json.dumps(to_jsonable(payload), sort_keys=True),
                ),
            )

    def list_live_order_payloads(self, market_ticker: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM live_orders"
        params: tuple[Any, ...] = ()
        if market_ticker:
            query += " WHERE market_ticker = ?"
            params = (market_ticker,)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_qualification_state(self, city_id: str) -> CityQualificationState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM qualification_states WHERE city_id = ?
                """,
                (city_id,),
            ).fetchone()
        if row is None:
            return None
        return self._qualification_from_json(json.loads(row[0]))

    def save_qualification_state(self, state: CityQualificationState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO qualification_states (city_id, state, payload_json)
                VALUES (?, ?, ?)
                """,
                (
                    state.city_id,
                    state.state.value,
                    json.dumps(to_jsonable(state), sort_keys=True),
                ),
            )

    def get_kill_switch(self, scope: str, city_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM kill_switches WHERE scope = ? AND city_id IS ?
                """,
                (scope, city_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_kill_switch(self, scope: str, payload: dict[str, Any], city_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO kill_switches (scope, city_id, state, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    scope,
                    city_id,
                    str(payload.get("state") or ""),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def ensure_default_qualification(
        self,
        city_id: str,
        state: QualificationState = QualificationState.SHADOW_ONLY,
    ) -> None:
        existing = self.get_qualification_state(city_id)
        if existing is not None:
            return
        now = datetime.now(timezone.utc)
        self.save_qualification_state(
            CityQualificationState(
                city_id=city_id,
                state=state,
                effective_from=now,
                effective_to=None,
                settlement_validation_score=Decimal("0"),
                calibration_score=Decimal("0"),
                nowcast_score=Decimal("0"),
                path_score=Decimal("0"),
                market_depth_score=Decimal("0"),
                slippage_score=Decimal("0"),
                shadow_ev_score=Decimal("0"),
                drawdown_score=Decimal("0"),
                promotion_reasons=("bootstrap_default",),
                demotion_reasons=(),
            )
        )

    def _observation_from_json(self, payload: dict[str, Any]) -> ObservationSnapshot:
        return ObservationSnapshot(
            station_id=payload["station_id"],
            event_time=_parse_datetime(payload["event_time"]),
            ingest_time=_parse_datetime(payload["ingest_time"]),
            temperature_f=_parse_decimal(payload["temperature_f"]),
            dewpoint_f=_parse_decimal(payload["dewpoint_f"]),
            wind_dir_deg=payload["wind_dir_deg"],
            wind_speed_kt=_parse_decimal(payload["wind_speed_kt"]),
            sky_cover_code=payload["sky_cover_code"],
            ceiling_ft=payload["ceiling_ft"],
            visibility_mi=_parse_decimal(payload["visibility_mi"]),
            weather_codes=tuple(payload["weather_codes"]),
            quality_flags=tuple(payload["quality_flags"]),
            source_payload_id=payload["source_payload_id"],
        )

    def _forecast_from_json(self, payload: dict[str, Any]) -> ForecastSnapshot:
        return ForecastSnapshot(
            provider_id=payload["provider_id"],
            provider_run_time=_parse_datetime(payload["provider_run_time"]),
            ingest_time=_parse_datetime(payload["ingest_time"]),
            valid_for_times=tuple(_parse_datetime(item) for item in payload["valid_for_times"]),
            hourly_temp_path_f=tuple(_parse_decimal(item) for item in payload["hourly_temp_path_f"]),
            cloud_cover_path_pct=tuple(_parse_decimal(item) for item in payload["cloud_cover_path_pct"]),
            wind_path=tuple(_parse_decimal(item) for item in payload["wind_path"]),
            precipitation_path=tuple(_parse_decimal(item) for item in payload["precipitation_path"]),
            provider_metadata=payload["provider_metadata"],
            source_payload_id=payload["source_payload_id"],
        )

    def _orderbook_from_json(self, payload: dict[str, Any]) -> OrderbookSnapshot:
        return OrderbookSnapshot(
            market_ticker=payload["market_ticker"],
            as_of_time=_parse_datetime(payload["as_of_time"]),
            seq=int(payload["seq"]),
            yes_bids_ladder=tuple(
                (_parse_decimal(price), _parse_decimal(size))
                for price, size in payload["yes_bids_ladder"]
            ),
            no_bids_ladder=tuple(
                (_parse_decimal(price), _parse_decimal(size))
                for price, size in payload["no_bids_ladder"]
            ),
            implied_yes_asks_ladder=tuple(
                (_parse_decimal(price), _parse_decimal(size))
                for price, size in payload["implied_yes_asks_ladder"]
            ),
            implied_no_asks_ladder=tuple(
                (_parse_decimal(price), _parse_decimal(size))
                for price, size in payload["implied_no_asks_ladder"]
            ),
            checksum_status=payload["checksum_status"],
            source_refs=tuple(payload["source_refs"]),
        )

    def _shadow_position_from_json(self, payload: dict[str, Any]) -> ShadowPosition:
        return ShadowPosition(
            city_id=payload["city_id"],
            market_ticker=payload["market_ticker"],
            side=str(payload.get("side") or "unknown"),
            open_quantity_fp=_parse_decimal(payload["open_quantity_fp"]),
            avg_cost_dollars=_parse_decimal(payload["avg_cost_dollars"]),
            cumulative_fees_dollars=_parse_decimal(payload["cumulative_fees_dollars"]),
            mark_pnl_dollars=_parse_decimal(payload["mark_pnl_dollars"]),
            settled_pnl_dollars=_parse_decimal(payload["settled_pnl_dollars"]),
            lifecycle_status=payload["lifecycle_status"],
        )

    def _trade_from_json(self, payload: dict[str, Any]) -> TradeSnapshot:
        return TradeSnapshot(
            trade_id=payload["trade_id"],
            market_ticker=payload["market_ticker"],
            created_time=_parse_datetime(payload["created_time"]),
            count_fp=_parse_decimal(payload["count_fp"]),
            yes_price_dollars=_parse_decimal(payload["yes_price_dollars"]),
            no_price_dollars=_parse_decimal(payload["no_price_dollars"]),
            taker_side=payload["taker_side"],
            source_payload_id=payload["source_payload_id"],
        )

    def _shadow_fill_from_json(self, payload: dict[str, Any]) -> ShadowFill:
        return ShadowFill(
            shadow_fill_id=payload["shadow_fill_id"],
            decision_id=payload["decision_id"],
            market_ticker=payload["market_ticker"],
            side=payload["side"],
            quantity_fp=_parse_decimal(payload["quantity_fp"]),
            modeled_fill_price_dollars=_parse_decimal(payload["modeled_fill_price_dollars"]),
            fill_scenario=payload["fill_scenario"],
            fill_confidence=_parse_decimal(payload["fill_confidence"]),
            modeled_fee_dollars=_parse_decimal(payload["modeled_fee_dollars"]),
            modeled_slippage_dollars=_parse_decimal(payload["modeled_slippage_dollars"]),
            modeled_adverse_selection_dollars=_parse_decimal(payload["modeled_adverse_selection_dollars"]),
            fill_time=_parse_datetime(payload["fill_time"]),
            reconciliation_status=payload["reconciliation_status"],
            predicted_executable_ev_per_contract=_parse_decimal(payload.get("predicted_executable_ev_per_contract")),
            predicted_executable_ev_total=_parse_decimal(payload.get("predicted_executable_ev_total")),
            model_confidence=_parse_decimal(payload.get("model_confidence")),
            execution_confidence=_parse_decimal(payload.get("execution_confidence")),
            governance_confidence=_parse_decimal(payload.get("governance_confidence")),
            overall_trade_confidence=_parse_decimal(payload.get("overall_trade_confidence")),
            confidence_reasons=tuple(payload.get("confidence_reasons") or ()),
        )

    def _qualification_from_json(self, payload: dict[str, Any]) -> CityQualificationState:
        return CityQualificationState(
            city_id=payload["city_id"],
            state=QualificationState(payload["state"]),
            effective_from=_parse_datetime(payload["effective_from"]),
            effective_to=_parse_datetime(payload["effective_to"]),
            settlement_validation_score=_parse_decimal(payload["settlement_validation_score"]),
            calibration_score=_parse_decimal(payload["calibration_score"]),
            nowcast_score=_parse_decimal(payload["nowcast_score"]),
            path_score=_parse_decimal(payload["path_score"]),
            market_depth_score=_parse_decimal(payload["market_depth_score"]),
            slippage_score=_parse_decimal(payload["slippage_score"]),
            shadow_ev_score=_parse_decimal(payload["shadow_ev_score"]),
            drawdown_score=_parse_decimal(payload["drawdown_score"]),
            promotion_reasons=tuple(payload["promotion_reasons"]),
            demotion_reasons=tuple(payload["demotion_reasons"]),
        )

    # ── Market settlements (actual daily high/low per city) ─────────────────

    def save_market_settlement(
        self,
        *,
        city_id: str,
        station_id: str,
        local_date: str,
        daily_high_f: float | None,
        daily_low_f: float | None,
        source: str,
        source_payload_id: str | None = None,
        fetched_at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        from json import dumps
        payload = {
            "city_id": city_id,
            "station_id": station_id,
            "local_date": local_date,
            "daily_high_f": daily_high_f,
            "daily_low_f": daily_low_f,
            "source": source,
            "source_payload_id": source_payload_id,
            **(extra or {}),
        }
        settlement_id = f"{city_id}:{local_date}"
        fetched_iso = (fetched_at or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO market_settlements
                   (settlement_id, city_id, station_id, local_date,
                    daily_high_f, daily_low_f, source, source_payload_id,
                    fetched_at, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(settlement_id) DO UPDATE SET
                     daily_high_f=excluded.daily_high_f,
                     daily_low_f=excluded.daily_low_f,
                     source=excluded.source,
                     source_payload_id=excluded.source_payload_id,
                     fetched_at=excluded.fetched_at,
                     payload_json=excluded.payload_json
                """,
                (
                    settlement_id, city_id, station_id, local_date,
                    daily_high_f, daily_low_f, source, source_payload_id,
                    fetched_iso, dumps(payload),
                ),
            )

    def get_market_settlement(self, city_id: str, local_date: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM market_settlements WHERE settlement_id = ?",
                (f"{city_id}:{local_date}",),
            ).fetchone()
        if not row:
            return None
        from json import loads
        return loads(row[0])

    def list_market_settlements(self, city_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload_json FROM market_settlements"
        params: tuple[Any, ...] = ()
        if city_id:
            sql += " WHERE city_id = ?"
            params = (city_id,)
        sql += " ORDER BY local_date DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        from json import loads
        return [loads(r[0]) for r in rows]

    # ── Market recommendations (every decision → counterfactual P&L) ────────

    def save_market_recommendation(self, record: dict[str, Any]) -> None:
        """Insert or update a recommendation row.

        Caller must supply at minimum: recommendation_id, decision_id, city_id,
        market_ticker, settlement_variable, operator, threshold_f, as_of_time,
        recommendation_kind. Optional indexed fields populate explicit columns
        for downstream queryability.
        """
        from json import dumps
        rejection_csv = ",".join(record.get("rejection_reasons") or [])
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO market_recommendations
                   (recommendation_id, decision_id, city_id, market_ticker,
                    settlement_variable, operator, threshold_f, as_of_time,
                    recommendation_kind, recommended_side, model_p_yes,
                    market_price, raw_edge, executable_ev, provider_spread_f,
                    rejection_reasons, counterfactual_resolved,
                    counterfactual_won, counterfactual_pnl_usd, payload_json,
                    local_time_of_day_hour, minutes_to_settlement_close,
                    window_status, current_temp_f, high_so_far_f,
                    threshold_gap_f_signed, orderbook_yes_bid, orderbook_yes_ask,
                    orderbook_spread, tradability_score, actually_filled,
                    fill_blocker_reason, settlement_close_time_utc,
                    realized_max_high_f, realized_min_low_f)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?,
                           ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(recommendation_id) DO UPDATE SET
                     recommendation_kind=excluded.recommendation_kind,
                     recommended_side=excluded.recommended_side,
                     model_p_yes=excluded.model_p_yes,
                     market_price=excluded.market_price,
                     raw_edge=excluded.raw_edge,
                     executable_ev=excluded.executable_ev,
                     provider_spread_f=excluded.provider_spread_f,
                     rejection_reasons=excluded.rejection_reasons,
                     payload_json=excluded.payload_json,
                     local_time_of_day_hour=excluded.local_time_of_day_hour,
                     minutes_to_settlement_close=excluded.minutes_to_settlement_close,
                     window_status=excluded.window_status,
                     current_temp_f=excluded.current_temp_f,
                     high_so_far_f=excluded.high_so_far_f,
                     threshold_gap_f_signed=excluded.threshold_gap_f_signed,
                     orderbook_yes_bid=excluded.orderbook_yes_bid,
                     orderbook_yes_ask=excluded.orderbook_yes_ask,
                     orderbook_spread=excluded.orderbook_spread,
                     tradability_score=excluded.tradability_score,
                     actually_filled=excluded.actually_filled,
                     fill_blocker_reason=excluded.fill_blocker_reason,
                     settlement_close_time_utc=excluded.settlement_close_time_utc
                """,
                (
                    record["recommendation_id"],
                    record["decision_id"],
                    record["city_id"],
                    record["market_ticker"],
                    record.get("settlement_variable", ""),
                    record.get("operator", ""),
                    record.get("threshold_f"),
                    record["as_of_time"],
                    record["recommendation_kind"],
                    record.get("recommended_side"),
                    record.get("model_p_yes"),
                    record.get("market_price"),
                    record.get("raw_edge"),
                    record.get("executable_ev"),
                    record.get("provider_spread_f"),
                    rejection_csv,
                    dumps(record.get("payload") or {}),
                    record.get("local_time_of_day_hour"),
                    record.get("minutes_to_settlement_close"),
                    record.get("window_status"),
                    record.get("current_temp_f"),
                    record.get("high_so_far_f"),
                    record.get("threshold_gap_f_signed"),
                    record.get("orderbook_yes_bid"),
                    record.get("orderbook_yes_ask"),
                    record.get("orderbook_spread"),
                    record.get("tradability_score"),
                    1 if record.get("actually_filled") else 0,
                    record.get("fill_blocker_reason"),
                    record.get("settlement_close_time_utc"),
                    record.get("realized_max_high_f"),
                    record.get("realized_min_low_f"),
                ),
            )

    def mark_recommendation_filled(self, recommendation_id: str) -> None:
        """Update the row to record that the bot actually placed/filled this bet."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE market_recommendations "
                "SET actually_filled=1, fill_blocker_reason=NULL "
                "WHERE recommendation_id=?",
                (recommendation_id,),
            )

    def mark_recommendation_blocked(
        self,
        recommendation_id: str,
        blocker_reason: str,
    ) -> None:
        """Record why the bot didn't fill (no orderbook, exec failed, capital cap, etc.)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE market_recommendations "
                "SET actually_filled=0, fill_blocker_reason=? "
                "WHERE recommendation_id=?",
                (blocker_reason, recommendation_id),
            )

    def list_recommendations(
        self,
        *,
        unresolved_only: bool = False,
        market_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = ("SELECT recommendation_id, decision_id, city_id, market_ticker, "
               "settlement_variable, operator, threshold_f, as_of_time, "
               "recommendation_kind, recommended_side, model_p_yes, market_price, "
               "raw_edge, executable_ev, provider_spread_f, rejection_reasons, "
               "counterfactual_resolved, counterfactual_won, counterfactual_pnl_usd, "
               "payload_json FROM market_recommendations")
        clauses = []
        params: list[Any] = []
        if unresolved_only:
            clauses.append("counterfactual_resolved = 0")
        if market_ticker:
            clauses.append("market_ticker = ?")
            params.append(market_ticker)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY as_of_time DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        from json import loads
        cols = ["recommendation_id", "decision_id", "city_id", "market_ticker",
                "settlement_variable", "operator", "threshold_f", "as_of_time",
                "recommendation_kind", "recommended_side", "model_p_yes",
                "market_price", "raw_edge", "executable_ev", "provider_spread_f",
                "rejection_reasons", "counterfactual_resolved",
                "counterfactual_won", "counterfactual_pnl_usd", "payload_json"]
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(zip(cols, r))
            d["rejection_reasons"] = [s for s in (d.get("rejection_reasons") or "").split(",") if s]
            d["payload"] = loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def update_recommendation_outcome(
        self,
        *,
        recommendation_id: str,
        counterfactual_won: bool,
        counterfactual_pnl_usd: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE market_recommendations
                   SET counterfactual_resolved = 1,
                       counterfactual_won = ?,
                       counterfactual_pnl_usd = ?
                   WHERE recommendation_id = ?""",
                (1 if counterfactual_won else 0, counterfactual_pnl_usd, recommendation_id),
            )
