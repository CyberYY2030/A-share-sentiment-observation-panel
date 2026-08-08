from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .capabilities import SCREENING_DEFINITION_VERSION


_BATCH_COLUMNS = {
    "batch_id",
    "trade_date",
    "definition_version",
    "mode",
    "observed_at",
    "price_as_of",
    "input_fingerprint",
    "status",
    "error_msg",
    "created_at",
    "completed_at",
}
_RUN_COLUMNS = {"batch_id", "mode", "input_fingerprint"}
_STATE_COLUMNS = {
    "state_id",
    "batch_id",
    "definition_version",
    "trade_date",
    "sec_code",
    "state",
    "trend_profile",
    "as_of",
    "created_at",
}


@dataclass(frozen=True)
class BatchSchemaState:
    code: str
    error_msg: str | None = None

    @property
    def ready(self) -> bool:
        return self.code == "ready"


@dataclass(frozen=True)
class LatestCompleteBatch:
    code: str
    batch_id: int | None = None
    error_msg: str | None = None


def selection_batch_schema_state(conn: sqlite3.Connection) -> BatchSchemaState:
    """Classify the explicit R3 schema without mutating the connected database."""
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table'
                  AND name IN ('selection_batches', 'strategy_runs', 'pullback_state_history')
                """
            )
        }
    except sqlite3.DatabaseError as exc:
        return BatchSchemaState("schema_error", f"sqlite_master: {type(exc).__name__}: {exc}")

    if "selection_batches" not in tables:
        return BatchSchemaState("migration_required", "selection_batches table is absent")
    if "strategy_runs" not in tables:
        return BatchSchemaState("schema_invalid", "strategy_runs table is absent")
    try:
        batch_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(selection_batches)")}
        run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(strategy_runs)")}
    except sqlite3.DatabaseError as exc:
        return BatchSchemaState("schema_error", f"table_info: {type(exc).__name__}: {exc}")

    missing_batch = sorted(_BATCH_COLUMNS - batch_columns)
    missing_run = sorted(_RUN_COLUMNS - run_columns)
    if missing_batch or missing_run:
        parts = []
        if missing_batch:
            parts.append("selection_batches missing " + ",".join(missing_batch))
        if missing_run:
            parts.append("strategy_runs missing " + ",".join(missing_run))
        return BatchSchemaState("schema_invalid", "; ".join(parts))
    if "pullback_state_history" not in tables:
        return BatchSchemaState("migration_required", "pullback_state_history table is absent")
    try:
        state_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(pullback_state_history)")}
    except sqlite3.DatabaseError as exc:
        return BatchSchemaState("schema_error", f"state table_info: {type(exc).__name__}: {exc}")
    missing_state = sorted(_STATE_COLUMNS - state_columns)
    if missing_state:
        return BatchSchemaState(
            "schema_invalid",
            "pullback_state_history missing " + ",".join(missing_state),
        )
    return BatchSchemaState("ready")


def selection_batch_schema_ready(conn: sqlite3.Connection) -> bool:
    return selection_batch_schema_state(conn).ready


def latest_complete_batch(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    definition_version: str = SCREENING_DEFINITION_VERSION,
) -> LatestCompleteBatch:
    """Return exactly the newest current-version complete batch for one close date."""
    schema = selection_batch_schema_state(conn)
    if not schema.ready:
        return LatestCompleteBatch(schema.code, error_msg=schema.error_msg)
    try:
        row = conn.execute(
            """
            SELECT batch_id
            FROM selection_batches
            WHERE trade_date=? AND definition_version=? AND mode='close_final' AND status='complete'
            ORDER BY completed_at DESC, batch_id DESC
            LIMIT 1
            """,
            (str(trade_date), str(definition_version)),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        return LatestCompleteBatch("schema_error", error_msg=f"latest complete batch: {type(exc).__name__}: {exc}")
    if row is None:
        return LatestCompleteBatch("unfinalized", error_msg="no complete close_final batch")
    return LatestCompleteBatch("complete", batch_id=int(row[0]))
