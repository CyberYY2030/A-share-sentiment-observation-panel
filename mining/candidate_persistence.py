from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from .capabilities import (
    CAPABILITY_TOP_N,
    SCREENING_DEFINITION_VERSION,
    formal_definition,
    formal_definitions,
    formal_strategy_ids,
    shortlist_capability_rows,
)
from .db import connect, now_str
from .scanners import Candidate
from .scanners.base_breakout import evaluate_base_breakout
from .scanners.counter_trend_rs import evaluate_counter_trend_rs
from .scanners.launch_burst import evaluate_compression_launch
from .scanners.momentum_breakout import evaluate_momentum_anomaly
from .scanners.second_launch import select_candidates_from_pullback_support
from .scanners.strong_trend import evaluate_strong_trend
from .selection_batches import selection_batch_schema_ready, selection_batch_schema_state
from .watchlist import PULLBACK_STATE_HISTORY_TABLE, PULLBACK_SUPPORT_STATES, build_pullback_support


@dataclass(frozen=True)
class CapabilityResult:
    strategy_id: str
    candidates: tuple[Candidate, ...]
    universe_size: int
    status: str = "ok"
    error_msg: str | None = None
    state_rows: tuple[PullbackStateRow, ...] = ()


@dataclass(frozen=True)
class PullbackStateRow:
    sec_code: str
    state: str
    trend_profile: str | None
    pullback_pct: float | None = None
    reclaim_ma10: bool | None = None
    activity_expand: bool | None = None


@dataclass(frozen=True)
class BatchPersistenceResult:
    batch_id: int | None
    status: str
    reused: bool
    error_msg: str | None = None


def migrate_selection_batch_schema(conn: sqlite3.Connection) -> None:
    """Explicit, idempotent migration for a single authorized database connection."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS selection_batches (
              batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
              trade_date TEXT NOT NULL,
              definition_version TEXT NOT NULL,
              mode TEXT NOT NULL,
              observed_at TEXT,
              price_as_of TEXT,
              input_fingerprint TEXT NOT NULL,
              status TEXT NOT NULL,
              error_msg TEXT,
              created_at TEXT NOT NULL,
              completed_at TEXT
            )
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(strategy_runs)")}
        for column, definition in (
            ("batch_id", "INTEGER"),
            ("mode", "TEXT"),
            ("input_fingerprint", "TEXT"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE strategy_runs ADD COLUMN {column} {definition}")
        _migrate_pullback_state_history(conn)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_selection_batches_lookup
            ON selection_batches(trade_date, definition_version, mode, status, completed_at, batch_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runs_batch
            ON strategy_runs(batch_id, strategy_id, version, trade_date)
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _create_pullback_state_history(conn: sqlite3.Connection, table_name: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {table_name} (
          state_id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER,
          definition_version TEXT,
          trade_date TEXT NOT NULL,
          sec_code TEXT NOT NULL,
          state TEXT NOT NULL,
          trend_profile TEXT,
          as_of TEXT,
          created_at TEXT,
          CHECK (
            (batch_id IS NULL AND definition_version IS NULL)
            OR (batch_id IS NOT NULL AND definition_version IS NOT NULL)
          ),
          UNIQUE (batch_id, sec_code),
          FOREIGN KEY (batch_id) REFERENCES selection_batches(batch_id)
        )
        """
    )


def _migrate_pullback_state_history(conn: sqlite3.Connection) -> None:
    """Create the R4.2 batch-owned state table while retaining legacy rows as ineligible audit data."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (PULLBACK_STATE_HISTORY_TABLE,),
    ).fetchone()
    required = {
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
    if exists is None:
        _create_pullback_state_history(conn, PULLBACK_STATE_HISTORY_TABLE)
    else:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({PULLBACK_STATE_HISTORY_TABLE})")}
        if not required.issubset(columns):
            legacy_columns = {"trade_date", "sec_code", "state", "trend_profile", "as_of", "created_at"}
            if not legacy_columns.issubset(columns):
                raise sqlite3.DatabaseError("pullback_state_history has an unsupported legacy schema")
            replacement = f"{PULLBACK_STATE_HISTORY_TABLE}_r42"
            _create_pullback_state_history(conn, replacement)
            conn.execute(
                f"""
                INSERT INTO {replacement} (
                  batch_id, definition_version, trade_date, sec_code, state,
                  trend_profile, as_of, created_at
                )
                SELECT NULL, NULL, trade_date, sec_code, state,
                       trend_profile, as_of, created_at
                FROM {PULLBACK_STATE_HISTORY_TABLE}
                """
            )
            conn.execute(f"DROP TABLE {PULLBACK_STATE_HISTORY_TABLE}")
            conn.execute(f"ALTER TABLE {replacement} RENAME TO {PULLBACK_STATE_HISTORY_TABLE}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{PULLBACK_STATE_HISTORY_TABLE}_code_date "
        f"ON {PULLBACK_STATE_HISTORY_TABLE}(sec_code, trade_date)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{PULLBACK_STATE_HISTORY_TABLE}_batch "
        f"ON {PULLBACK_STATE_HISTORY_TABLE}(batch_id, definition_version)"
    )


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except ValueError:
            return None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if pd.isna(value):
        return None
    return str(value)


def _candidate_features(candidate: Candidate) -> dict[str, Any]:
    features = candidate.features
    if is_dataclass(features):
        features = asdict(features)
    return _json_value(dict(features))


def _candidates_from_rows(
    strategy_id: str,
    context: Any,
    rows: pd.DataFrame,
    *,
    reference_column: str = "reference_price",
) -> tuple[Candidate, ...]:
    definition = formal_definition(strategy_id)
    if rows.empty:
        return ()
    candidates: list[Candidate] = []
    for rank, row in enumerate(rows.to_dict(orient="records"), start=1):
        try:
            entry_price = float(row[reference_column])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{strategy_id} row has no valid {reference_column}") from None
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError(f"{strategy_id} row has no positive reference price")
        sec_code = str(row.get("sec_code") or "").zfill(6)
        if not sec_code.strip("0"):
            raise ValueError(f"{strategy_id} row has no sec_code")
        features = {
            key: _json_value(value)
            for key, value in row.items()
            if key not in {"sec_code", "sec_name"}
        }
        candidates.append(
            Candidate(
                strategy_id=definition.strategy_id,
                version=definition.version,
                trade_date=str(context.trade_date),
                sec_type="stock",
                sec_code=sec_code,
                sec_name=str(row.get("sec_name") or sec_code),
                entry_price=entry_price,
                features=features,
                rank=rank,
            )
        )
    return tuple(candidates)


def _shortlist_capability_results(results: Iterable[CapabilityResult]) -> tuple[CapabilityResult, ...]:
    """Apply the one A–E Top-20 contract after subtype evaluation, before persistence."""
    provided = tuple(results)
    selected_by_strategy: dict[str, list[Candidate]] = {item.strategy_id: [] for item in provided}
    by_capability: dict[str, list[Candidate]] = {}
    for item in provided:
        definition = formal_definition(item.strategy_id)
        by_capability.setdefault(definition.capability, []).extend(item.candidates)

    for capability, candidates in by_capability.items():
        def score(candidate: Candidate) -> tuple[float, float, str]:
            features = candidate.features
            try:
                primary = float(features.get("score", float("-inf")))
            except (TypeError, ValueError):
                primary = float("-inf")
            try:
                activity = float(features.get("activity_pct", float("-inf")))
            except (TypeError, ValueError):
                activity = float("-inf")
            return primary, activity, str(candidate.sec_code).zfill(6)

        ranked = shortlist_capability_rows(pd.DataFrame(
            {
                "candidate": candidates,
                "sec_code": [str(item.sec_code).zfill(6) for item in candidates],
                "score": [score(item)[0] for item in candidates],
                "activity_pct": [score(item)[1] for item in candidates],
                "event_subtype": [item.features.get("event_subtype") for item in candidates],
            }
        ))
        for row in ranked.itertuples(index=False):
            candidate = row.candidate
            code = str(candidate.sec_code).zfill(6)
            rank = int(row.capability_rank)
            features = dict(candidate.features)
            features["capability_rank"] = rank
            if capability == "B":
                features["subtype_evidence"] = tuple(row.subtype_evidence)
            selected_by_strategy[candidate.strategy_id].append(
                Candidate(
                    strategy_id=candidate.strategy_id,
                    version=candidate.version,
                    trade_date=candidate.trade_date,
                    sec_type=candidate.sec_type,
                    sec_code=code,
                    sec_name=candidate.sec_name,
                    entry_price=candidate.entry_price,
                    features=features,
                    rank=rank,
                )
            )

    return tuple(
        CapabilityResult(
            item.strategy_id,
            tuple(selected_by_strategy[item.strategy_id]),
            item.universe_size,
            item.status if item.status not in {"ok", "empty"} else ("ok" if selected_by_strategy[item.strategy_id] else "empty"),
            item.error_msg,
            item.state_rows,
        )
        for item in provided
    )


def evaluate_formal_capabilities(conn: sqlite3.Connection, context: Any) -> tuple[CapabilityResult, ...]:
    """Evaluate A–E once from the supplied shared close-final context."""
    universe_size = len(context.universe)
    if context.mode != "close_final" or getattr(context, "price_status", context.data_status) == "unavailable":
        reason = "close_final_context_required" if context.mode != "close_final" else "price_unavailable"
        return tuple(
            CapabilityResult(definition.strategy_id, (), universe_size, "failed", reason)
            for definition in formal_definitions()
        )

    results: list[CapabilityResult] = []

    def append_rows(strategy_id: str, rows: pd.DataFrame, *, reference_column: str = "reference_price") -> None:
        candidates = _candidates_from_rows(strategy_id, context, rows, reference_column=reference_column)
        results.append(
            CapabilityResult(strategy_id, candidates, universe_size, "ok" if candidates else "empty")
        )

    def append_failure(strategy_id: str, exc: Exception) -> None:
        results.append(CapabilityResult(strategy_id, (), universe_size, "failed", f"{type(exc).__name__}: {exc}"))

    try:
        strong = evaluate_strong_trend(context)
        append_rows("strong_trend", strong.rows, reference_column="reference_price")
    except Exception as exc:
        append_failure("strong_trend", exc)
    try:
        rows, _ = evaluate_compression_launch(context)
        append_rows("compression_launch", rows)
    except Exception as exc:
        append_failure("compression_launch", exc)
    try:
        rows, _ = evaluate_momentum_anomaly(context)
        append_rows("momentum_anomaly", rows)
    except Exception as exc:
        append_failure("momentum_anomaly", exc)
    try:
        evaluation = build_pullback_support(conn, context.trade_date, context=context)
        definition = formal_definition("second_launch")
        candidates = tuple(
            select_candidates_from_pullback_support(
                evaluation,
                trade_date=context.trade_date,
                strategy_id=definition.strategy_id,
                version=definition.version,
                top_n=20,
            )
        )
        profile_id = evaluation.trend_profile.profile_id if evaluation.trend_profile else None
        state_rows = tuple(
            PullbackStateRow(
                str(row.sec_code).zfill(6),
                str(row.state),
                profile_id,
                float(row.pullback_pct),
                bool(row.reclaim_ma10),
                bool(row.activity_expand),
            )
            for row in evaluation.rows.itertuples(index=False)
        )
        results.append(
            CapabilityResult(
                "second_launch",
                candidates,
                universe_size,
                "ok" if candidates else "empty",
                state_rows=state_rows,
            )
        )
    except Exception as exc:
        append_failure("second_launch", exc)
    try:
        rows, _ = evaluate_base_breakout(context)
        append_rows("base_breakout", rows)
    except Exception as exc:
        append_failure("base_breakout", exc)
    try:
        rows, _ = evaluate_counter_trend_rs(context)
        append_rows("counter_trend_rs", rows)
    except Exception as exc:
        append_failure("counter_trend_rs", exc)
    return _shortlist_capability_results(results)


def _failed_batch(
    conn: sqlite3.Connection,
    context: Any,
    error_msg: str,
    *,
    result_status: str = "failed",
) -> BatchPersistenceResult:
    try:
        cursor = conn.execute(
            """
            INSERT INTO selection_batches (
              trade_date, definition_version, mode, observed_at, price_as_of,
              input_fingerprint, status, error_msg, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)
            """,
            (
                str(context.trade_date),
                SCREENING_DEFINITION_VERSION,
                str(context.mode),
                str(context.as_of),
                str(context.price_as_of),
                str(context.input_fingerprint),
                error_msg,
                now_str(),
                now_str(),
            ),
        )
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        return BatchPersistenceResult(
            None,
            "persistence_error",
            False,
            f"{error_msg}; failed to record batch: {type(exc).__name__}: {exc}",
        )
    return BatchPersistenceResult(int(cursor.lastrowid), result_status, False, error_msg)


def _validate_results(results: Iterable[CapabilityResult]) -> tuple[CapabilityResult, ...]:
    provided = tuple(results)
    by_strategy = {result.strategy_id: result for result in provided}
    expected = formal_strategy_ids()
    missing = [strategy_id for strategy_id in expected if strategy_id not in by_strategy]
    unexpected = sorted(set(by_strategy) - set(expected))
    failures = [result for result in by_strategy.values() if result.status not in {"ok", "empty"}]
    duplicate = len(by_strategy) != len(provided)
    if missing or unexpected or failures or duplicate:
        parts = []
        if missing:
            parts.append(f"missing={','.join(missing)}")
        if unexpected:
            parts.append(f"unexpected={','.join(unexpected)}")
        if failures:
            parts.append(
                "failed=" + ",".join(f"{item.strategy_id}:{item.error_msg or item.status}" for item in failures)
            )
        if duplicate:
            parts.append("duplicate strategy result")
        raise ValueError("; ".join(parts))
    ordered = tuple(by_strategy[strategy_id] for strategy_id in expected)
    for result in ordered:
        definition = formal_definition(result.strategy_id)
        for candidate in result.candidates:
            if candidate.strategy_id != definition.strategy_id:
                raise ValueError(f"candidate strategy mismatch for {definition.strategy_id}")
        if result.strategy_id != "second_launch" and result.state_rows:
            raise ValueError(f"state rows are not allowed for {result.strategy_id}")
        if result.strategy_id == "second_launch":
            state_codes = [str(row.sec_code).zfill(6) for row in result.state_rows]
            if len(state_codes) != len(set(state_codes)):
                raise ValueError("duplicate second_launch state row")
            if any(not code.strip("0") for code in state_codes):
                raise ValueError("second_launch state row has no sec_code")
            if any(row.state not in PULLBACK_SUPPORT_STATES for row in result.state_rows):
                raise ValueError("second_launch state row has an invalid state")
            candidate_codes = {str(candidate.sec_code).zfill(6) for candidate in result.candidates}
            if not candidate_codes.issubset(set(state_codes)):
                raise ValueError("second_launch candidate has no matching state row")
    by_capability: dict[str, list[Candidate]] = {}
    for result in ordered:
        by_capability.setdefault(formal_definition(result.strategy_id).capability, []).extend(result.candidates)
    for capability, candidates in by_capability.items():
        codes = [str(candidate.sec_code).zfill(6) for candidate in candidates]
        ranks = sorted(candidate.rank for candidate in candidates if candidate.rank is not None)
        if len(codes) > CAPABILITY_TOP_N:
            raise ValueError(f"{capability} exceeds capability Top {CAPABILITY_TOP_N}")
        if len(codes) != len(set(codes)):
            raise ValueError(f"{capability} has duplicate candidate sec_code")
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(f"{capability} ranks are not contiguous")
    return ordered


def persist_close_final_batch(
    conn: sqlite3.Connection,
    context: Any,
    capability_results: Iterable[CapabilityResult],
) -> BatchPersistenceResult:
    """Atomically replace one close-final A–E batch, or record a failed batch separately."""
    schema = selection_batch_schema_state(conn)
    if not schema.ready:
        return BatchPersistenceResult(None, schema.code, False, schema.error_msg)
    if str(context.mode) != "close_final" or not context.input_fingerprint:
        return _failed_batch(conn, context, "close_final mode and input_fingerprint are required")
    try:
        results = _validate_results(_shortlist_capability_results(capability_results))
    except Exception as exc:
        return _failed_batch(conn, context, str(exc))

    try:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute(
            """
            SELECT batch_id, input_fingerprint
            FROM selection_batches
            WHERE trade_date=? AND definition_version=? AND mode='close_final' AND status='complete'
            ORDER BY completed_at DESC, batch_id DESC
            LIMIT 1
            """,
            (str(context.trade_date), SCREENING_DEFINITION_VERSION),
        ).fetchone()
        if latest is not None and str(latest[1]) == str(context.input_fingerprint):
            conn.commit()
            return BatchPersistenceResult(int(latest[0]), "complete", True)
        created_at = now_str()
        cursor = conn.execute(
            """
            INSERT INTO selection_batches (
              trade_date, definition_version, mode, observed_at, price_as_of,
              input_fingerprint, status, error_msg, created_at, completed_at
            ) VALUES (?, ?, 'close_final', ?, ?, ?, 'building', NULL, ?, NULL)
            """,
            (
                str(context.trade_date),
                SCREENING_DEFINITION_VERSION,
                str(context.as_of),
                str(context.price_as_of),
                str(context.input_fingerprint),
                created_at,
            ),
        )
        batch_id = int(cursor.lastrowid)
        run_ids: list[int] = []
        for result in results:
            definition = formal_definition(result.strategy_id)
            conn.execute(
                """
                INSERT OR REPLACE INTO strategies (
                  strategy_id, version, kind, description, params_json, created_at
                ) VALUES (?, ?, 'stock', ?, '{}', ?)
                """,
                (
                    definition.strategy_id,
                    definition.version,
                    f"{definition.capability} {definition.label}",
                    created_at,
                ),
            )
            run = conn.execute(
                """
                INSERT INTO strategy_runs (
                  strategy_id, version, trade_date, run_at, universe_size,
                  n_candidates, status, error_msg, batch_id, mode, input_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'close_final', ?)
                """,
                (
                    definition.strategy_id,
                    definition.version,
                    str(context.trade_date),
                    created_at,
                    result.universe_size,
                    len(result.candidates),
                    result.status,
                    batch_id,
                    str(context.input_fingerprint),
                ),
            )
            run_id = int(run.lastrowid)
            run_ids.append(run_id)
            for candidate in result.candidates:
                features_json = json.dumps(_candidate_features(candidate), ensure_ascii=False, sort_keys=True)
                existing_candidate = conn.execute(
                    """
                    SELECT candidate_id FROM candidates
                    WHERE strategy_id=? AND version=? AND trade_date=? AND sec_code=?
                    """,
                    (definition.strategy_id, definition.version, str(context.trade_date), candidate.sec_code),
                ).fetchone()
                if existing_candidate is None:
                    conn.execute(
                        """
                        INSERT INTO candidates (
                          run_id, strategy_id, version, trade_date, sec_type, sec_code,
                          sec_name, entry_price, features_json, rank
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            definition.strategy_id,
                            definition.version,
                            str(context.trade_date),
                            candidate.sec_type,
                            candidate.sec_code,
                            candidate.sec_name,
                            candidate.entry_price,
                            features_json,
                            candidate.rank,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE candidates
                        SET run_id=?, sec_type=?, sec_name=?, entry_price=?, features_json=?, rank=?
                        WHERE candidate_id=?
                        """,
                        (
                            run_id,
                            candidate.sec_type,
                            candidate.sec_name,
                            candidate.entry_price,
                            features_json,
                            candidate.rank,
                            int(existing_candidate[0]),
                        ),
                    )

        c_result = next(result for result in results if result.strategy_id == "second_launch")
        for state_row in c_result.state_rows:
            conn.execute(
                f"""
                INSERT INTO {PULLBACK_STATE_HISTORY_TABLE} (
                  batch_id, definition_version, trade_date, sec_code, state,
                  trend_profile, as_of, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    SCREENING_DEFINITION_VERSION,
                    str(context.trade_date),
                    str(state_row.sec_code).zfill(6),
                    state_row.state,
                    state_row.trend_profile,
                    str(context.as_of),
                    created_at,
                ),
            )

        strategy_marks = ",".join("?" for _ in formal_strategy_ids())
        run_marks = ",".join("?" for _ in run_ids)
        stale_ids = [
            int(row[0])
            for row in conn.execute(
                f"""
                SELECT candidate_id FROM candidates
                WHERE trade_date=? AND version=? AND strategy_id IN ({strategy_marks})
                  AND run_id NOT IN ({run_marks})
                """,
                (str(context.trade_date), SCREENING_DEFINITION_VERSION, *formal_strategy_ids(), *run_ids),
            ).fetchall()
        ]
        if stale_ids:
            stale_marks = ",".join("?" for _ in stale_ids)
            conn.execute(f"DELETE FROM outcomes WHERE candidate_id IN ({stale_marks})", stale_ids)
            conn.execute(f"DELETE FROM candidates WHERE candidate_id IN ({stale_marks})", stale_ids)
        completed_at = now_str()
        conn.execute(
            """
            UPDATE selection_batches
            SET status='complete', completed_at=?
            WHERE batch_id=?
            """,
            (completed_at, batch_id),
        )
        conn.commit()
        return BatchPersistenceResult(batch_id, "complete", False)
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        return _failed_batch(
            conn,
            context,
            f"persistence SQL error: {type(exc).__name__}: {exc}",
            result_status="persistence_error",
        )
    except Exception as exc:
        conn.rollback()
        return _failed_batch(conn, context, f"{type(exc).__name__}: {exc}")


def audit_candidate_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read-only triple inventory for pre-R3 unions and incomplete/legacy runs."""
    rows = conn.execute(
        """
        SELECT r.strategy_id, r.version, r.trade_date,
               COUNT(*) AS run_count,
               SUM(CASE WHEN r.batch_id IS NULL THEN 1 ELSE 0 END) AS unbatched_runs,
               SUM(CASE WHEN b.status='complete' THEN 1 ELSE 0 END) AS complete_batch_runs,
               SUM(CASE WHEN b.status IS NULL OR b.status!='complete' THEN 1 ELSE 0 END) AS incomplete_batch_runs,
               COUNT(DISTINCT c.candidate_id) AS candidate_rows
        FROM strategy_runs r
        LEFT JOIN selection_batches b ON b.batch_id=r.batch_id
        LEFT JOIN candidates c
          ON c.run_id=r.run_id
        GROUP BY r.strategy_id, r.version, r.trade_date
        ORDER BY r.trade_date, r.strategy_id, r.version
        """
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="R3 selection-batch migration and read-only history audit.")
    parser.add_argument("action", choices=("migrate", "audit"))
    parser.add_argument("--base-dir", default=Path.cwd())
    args = parser.parse_args()
    conn = connect(base_dir=args.base_dir)
    try:
        if args.action == "migrate":
            migrate_selection_batch_schema(conn)
            print("selection-batch migration complete")
        else:
            print(json.dumps(audit_candidate_history(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    main()
