from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from runtime_paths import build_runtime_paths, ensure_runtime_dirs


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strategies (
  strategy_id     TEXT NOT NULL,
  version         TEXT NOT NULL,
  kind            TEXT NOT NULL,
  description     TEXT,
  params_json     TEXT,
  created_at      TEXT,
  PRIMARY KEY (strategy_id, version)
);

CREATE TABLE IF NOT EXISTS strategy_runs (
  run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id     TEXT NOT NULL,
  version         TEXT NOT NULL,
  trade_date      TEXT NOT NULL,
  run_at          TEXT NOT NULL,
  universe_size   INTEGER,
  n_candidates    INTEGER,
  status          TEXT,
  error_msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy_date ON strategy_runs(strategy_id, trade_date);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          INTEGER NOT NULL,
  strategy_id     TEXT NOT NULL,
  version         TEXT NOT NULL,
  trade_date      TEXT NOT NULL,
  sec_type        TEXT NOT NULL,
  sec_code        TEXT NOT NULL,
  sec_name        TEXT,
  entry_price     REAL NOT NULL,
  features_json   TEXT,
  rank            INTEGER,
  FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cand_upsert
ON candidates(strategy_id, version, trade_date, sec_code);
CREATE INDEX IF NOT EXISTS idx_cand_strat_date ON candidates(strategy_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_cand_date_code ON candidates(trade_date, sec_code);

CREATE TABLE IF NOT EXISTS outcomes (
  candidate_id    INTEGER PRIMARY KEY,
  open_t1         REAL,
  high_t1         REAL,
  low_t1          REAL,
  close_t1        REAL,
  close_t2        REAL,
  close_t5        REAL,
  r1              REAL,
  r2              REAL,
  r3              REAL,
  r4              REAL,
  r5              REAL,
  is_win          INTEGER,
  backfilled_at   TEXT,
  status          TEXT,
  FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_win ON outcomes(is_win);

CREATE TABLE IF NOT EXISTS stock_market_cap (
  sec_code        TEXT PRIMARY KEY,
  total_mv        REAL,
  snapshot_date   TEXT,
  updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS stock_listing (
  sec_code        TEXT PRIMARY KEY,
  list_date       TEXT,
  snapshot_date   TEXT,
  updated_at      TEXT
);
"""


def _quote_path(path: Path) -> str:
    return str(path).replace("'", "''")


def connect(
    base_dir: str | Path = ".",
    mining_db_path: str | Path | None = None,
    stock_db_path: str | Path | None = None,
    concept_db_path: str | Path | None = None,
) -> sqlite3.Connection:
    base = Path(base_dir).resolve()
    runtime_paths = build_runtime_paths(str(base))
    ensure_runtime_dirs(runtime_paths)

    mining_db = Path(mining_db_path) if mining_db_path else base / "mining_mvp.db"
    stock_db = Path(stock_db_path) if stock_db_path else Path(runtime_paths.stock_db)
    concept_db = Path(concept_db_path) if concept_db_path else Path(runtime_paths.concept_db)

    conn = sqlite3.connect(mining_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)

    attached = {row["name"] for row in conn.execute("PRAGMA database_list")}
    if "ash" not in attached:
        conn.execute(f"ATTACH DATABASE '{_quote_path(stock_db)}' AS ash")
    if concept_db.exists() and "ths" not in attached:
        conn.execute(f"ATTACH DATABASE '{_quote_path(concept_db)}' AS ths")

    return conn


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def latest_stock_trade_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(trade_date)
        FROM ash.kline_daily
        WHERE sec_type='stock'
        """
    ).fetchone()
    return None if row is None else row[0]


def list_stock_trade_dates(
    conn: sqlite3.Connection,
    end_date: str | None = None,
    limit: int | None = None,
    include_end: bool = True,
) -> list[str]:
    where = "WHERE sec_type='stock'"
    params: list[Any] = []
    if end_date:
        op = "<=" if include_end else "<"
        where += f" AND trade_date {op} ?"
        params.append(end_date)
    rows = conn.execute(
        f"""
        SELECT DISTINCT trade_date
        FROM ash.kline_daily
        {where}
        ORDER BY trade_date
        """,
        params,
    ).fetchall()
    dates = [row[0] for row in rows]
    if limit is not None:
        return dates[-limit:]
    return dates


def list_concept_trade_dates(
    conn: sqlite3.Connection,
    end_date: str | None = None,
    limit: int | None = None,
    include_end: bool = True,
) -> list[str]:
    attached = {row["name"] for row in conn.execute("PRAGMA database_list")}
    if "ths" not in attached:
        return []
    where = ""
    params: list[Any] = []
    if end_date:
        op = "<=" if include_end else "<"
        where = f"WHERE trade_date {op} ?"
        params.append(end_date)
    rows = conn.execute(
        f"""
        SELECT DISTINCT trade_date
        FROM ths.concept_kline
        {where}
        ORDER BY trade_date
        """,
        params,
    ).fetchall()
    dates = [row[0] for row in rows]
    if limit is not None:
        return dates[-limit:]
    return dates


def register_strategy(conn: sqlite3.Connection, scanner: Any) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO strategies (
          strategy_id, version, kind, description, params_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scanner.strategy_id,
            scanner.version,
            scanner.kind,
            getattr(scanner, "description", ""),
            json.dumps(scanner.params, ensure_ascii=False, sort_keys=True),
            now_str(),
        ),
    )
    conn.commit()


def create_strategy_run(
    conn: sqlite3.Connection,
    scanner: Any,
    trade_date: str,
    status: str,
    n_candidates: int,
    error_msg: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO strategy_runs (
          strategy_id, version, trade_date, run_at, universe_size,
          n_candidates, status, error_msg
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scanner.strategy_id,
            scanner.version,
            trade_date,
            now_str(),
            getattr(scanner, "last_universe_size", None),
            n_candidates,
            status,
            error_msg,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _candidate_features(candidate: Any) -> dict[str, Any]:
    features = getattr(candidate, "features", {})
    if is_dataclass(features):
        return asdict(features)
    return dict(features)


def save_candidates(
    conn: sqlite3.Connection,
    run_id: int,
    candidates: Iterable[Any],
) -> list[int]:
    candidate_ids: list[int] = []
    for candidate in candidates:
        features_json = json.dumps(
            _candidate_features(candidate), ensure_ascii=False, sort_keys=True
        )
        existing = conn.execute(
            """
            SELECT candidate_id
            FROM candidates
            WHERE strategy_id=? AND version=? AND trade_date=? AND sec_code=?
            """,
            (
                candidate.strategy_id,
                candidate.version,
                candidate.trade_date,
                candidate.sec_code,
            ),
        ).fetchone()
        if existing:
            candidate_id = int(existing[0])
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
                    candidate_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO candidates (
                  run_id, strategy_id, version, trade_date, sec_type, sec_code,
                  sec_name, entry_price, features_json, rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    candidate.strategy_id,
                    candidate.version,
                    candidate.trade_date,
                    candidate.sec_type,
                    candidate.sec_code,
                    candidate.sec_name,
                    candidate.entry_price,
                    features_json,
                    candidate.rank,
                ),
            )
            candidate_id = int(cursor.lastrowid)
        candidate_ids.append(candidate_id)
    conn.commit()
    return candidate_ids

