from __future__ import annotations

import datetime as dt
import math
import sqlite3
from statistics import median
from typing import Any, Iterable


SESSION_DIAGNOSTICS_TABLE = "selection_session_diagnostics"
STATUS_CLEAN = "clean"
STATUS_PARTIAL = "partial_missing"
STATUS_BAD = "bad_session"
STATUS_KNOWN_BAD = "known_bad_session"
STATUS_UNAVAILABLE = "quality_unavailable"
_REQUIRED_KLINE_COLUMNS = {"open", "high", "low", "close", "pre_close", "volume", "amount"}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0.0


def _schema_prefix(conn: sqlite3.Connection) -> str:
    attached = {str(row[1]) for row in conn.execute("PRAGMA database_list").fetchall()}
    return "ash." if "ash" in attached else ""


def _kline_table(conn: sqlite3.Connection) -> str:
    return f"{_schema_prefix(conn)}kline_daily"


def _diagnostics_table(conn: sqlite3.Connection) -> str:
    return f"{_schema_prefix(conn)}{SESSION_DIAGNOSTICS_TABLE}"


def ensure_session_diagnostics_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_diagnostics_table(conn)} (
          trade_date TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          reason TEXT,
          source_errors TEXT,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def mark_known_bad_session(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    reason: str,
    source_errors: str = "",
) -> None:
    ensure_session_diagnostics_table(conn)
    conn.execute(
        f"""
        INSERT INTO {_diagnostics_table(conn)} (
          trade_date, status, reason, source_errors, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET
          status=excluded.status,
          reason=excluded.reason,
          source_errors=excluded.source_errors,
          updated_at=excluded.updated_at
        """,
        (
            str(trade_date),
            STATUS_KNOWN_BAD,
            str(reason),
            str(source_errors),
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def clear_known_bad_session(conn: sqlite3.Connection, trade_date: str) -> None:
    ensure_session_diagnostics_table(conn)
    conn.execute(f"DELETE FROM {_diagnostics_table(conn)} WHERE trade_date=?", (str(trade_date),))
    conn.commit()


def _known_bad_record(conn: sqlite3.Connection, trade_date: str) -> sqlite3.Row | None:
    try:
        return conn.execute(
            f"""
            SELECT status, reason, source_errors
            FROM {_diagnostics_table(conn)}
            WHERE trade_date=? AND status=?
            """,
            (str(trade_date), STATUS_KNOWN_BAD),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _row_quality(row: sqlite3.Row) -> dict[str, bool]:
    values = {key: row[key] for key in ("open", "high", "low", "close", "pre_close", "volume", "amount")}
    prices = [values[key] for key in ("open", "high", "low", "close")]
    price_finite = all(_finite(value) for value in prices)
    close_valid = _positive(values["close"])
    ohlc_valid = (
        price_finite
        and float(values["low"]) <= min(float(values["open"]), float(values["close"]))
        and max(float(values["open"]), float(values["close"])) <= float(values["high"])
    )
    flat_prices = price_finite and len({float(value) for value in prices}) == 1
    pre_close = values["pre_close"]
    pre_close_matches = not _positive(pre_close) or math.isclose(
        float(pre_close), float(values["close"]), rel_tol=0.0, abs_tol=1e-9
    )
    has_price_move = price_finite and (
        not flat_prices
        or (_positive(pre_close) and not pre_close_matches)
    )
    has_activity = _positive(values["volume"]) and _positive(values["amount"])
    normal_halt = close_valid and ohlc_valid and flat_prices and pre_close_matches and not has_activity
    invalid_activity = has_price_move and not has_activity
    invalid_numeric = not all(_finite(value) for value in prices)
    return {
        "close_valid": close_valid,
        "ohlc_valid": ohlc_valid,
        "normal_halt": normal_halt,
        "invalid_activity": invalid_activity,
        "invalid_numeric": invalid_numeric,
    }


def _session_rows(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT open, high, low, close, pre_close, volume, amount
        FROM {_kline_table(conn)}
        WHERE sec_type='stock' AND trade_date=?
        """,
        (str(trade_date),),
    ).fetchall()


def _has_quality_columns(conn: sqlite3.Connection) -> bool:
    table = _kline_table(conn).split(".")[-1]
    schema = "ash." if _schema_prefix(conn) else ""
    columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA {schema}table_info({table})").fetchall()
    }
    return _REQUIRED_KLINE_COLUMNS.issubset(columns)


def _raw_session_summary(rows: Iterable[sqlite3.Row]) -> dict[str, int]:
    result = {
        "stock_rows": 0,
        "valid_rows": 0,
        "normal_halt_rows": 0,
        "invalid_close_rows": 0,
        "invalid_ohlc_rows": 0,
        "invalid_activity_rows": 0,
        "nonfinite_rows": 0,
    }
    for row in rows:
        quality = _row_quality(row)
        result["stock_rows"] += 1
        if quality["close_valid"] and quality["ohlc_valid"] and not quality["invalid_activity"]:
            result["valid_rows"] += 1
        if quality["normal_halt"]:
            result["normal_halt_rows"] += 1
        if not quality["close_valid"]:
            result["invalid_close_rows"] += 1
        if not quality["ohlc_valid"]:
            result["invalid_ohlc_rows"] += 1
        if quality["invalid_activity"]:
            result["invalid_activity_rows"] += 1
        if quality["invalid_numeric"]:
            result["nonfinite_rows"] += 1
    return result


def _recent_clean_coverage(conn: sqlite3.Connection, trade_date: str, lookback: int) -> list[int]:
    dates = [
        str(row[0])
        for row in conn.execute(
            f"""
            SELECT DISTINCT trade_date
            FROM {_kline_table(conn)}
            WHERE sec_type='stock' AND trade_date < ?
            ORDER BY trade_date DESC
            """,
            (str(trade_date),),
        ).fetchall()
    ]
    coverages: list[int] = []
    for day in dates:
        if _known_bad_record(conn, day) is not None:
            continue
        summary = _raw_session_summary(_session_rows(conn, day))
        if summary["stock_rows"] and summary["valid_rows"] == summary["stock_rows"]:
            coverages.append(summary["valid_rows"])
        if len(coverages) >= lookback:
            break
    return coverages


def _classify_summary(
    trade_date: str,
    summary: dict[str, int],
    coverage_baseline: int | None,
    min_coverage_ratio: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "trade_date": str(trade_date),
        **summary,
        "coverage_baseline": coverage_baseline,
        "coverage_ratio": (summary["valid_rows"] / coverage_baseline) if coverage_baseline else None,
        "status": STATUS_BAD,
        "reasons": [],
    }
    if summary["stock_rows"] == 0:
        result["reasons"] = ["missing_stock_rows"]
        return result
    if summary["valid_rows"] == 0:
        result["reasons"] = ["no_valid_rows"]
        if summary["nonfinite_rows"]:
            result["reasons"].append("nonfinite_values")
        if summary["invalid_close_rows"]:
            result["reasons"].append("invalid_close")
        if summary["invalid_ohlc_rows"]:
            result["reasons"].append("invalid_ohlc")
        if summary["invalid_activity_rows"]:
            result["reasons"].append("activity_without_volume_or_amount")
        return result
    if summary["nonfinite_rows"]:
        result["status"] = STATUS_PARTIAL
        result["reasons"].append("nonfinite_values")
    if summary["invalid_close_rows"]:
        result["status"] = STATUS_PARTIAL
        result["reasons"].append("invalid_close")
    if summary["invalid_ohlc_rows"]:
        result["status"] = STATUS_PARTIAL
        result["reasons"].append("invalid_ohlc")
    if summary["invalid_activity_rows"]:
        result["status"] = STATUS_PARTIAL
        result["reasons"].append("activity_without_volume_or_amount")
    if result["coverage_ratio"] is not None and result["coverage_ratio"] < float(min_coverage_ratio):
        result["status"] = STATUS_PARTIAL
        result["reasons"].append("coverage_below_clean_median")
    if result["status"] == STATUS_BAD:
        result["status"] = STATUS_CLEAN
    return result


def inspect_stock_session(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    coverage_lookback: int = 20,
    min_coverage_ratio: float = 0.8,
) -> dict[str, Any]:
    """Classify a stock session without treating normal halts as bad data."""
    if not _has_quality_columns(conn):
        return {
            "trade_date": str(trade_date),
            "stock_rows": 0,
            "valid_rows": 0,
            "normal_halt_rows": 0,
            "invalid_close_rows": 0,
            "invalid_ohlc_rows": 0,
            "invalid_activity_rows": 0,
            "nonfinite_rows": 0,
            "coverage_baseline": None,
            "coverage_ratio": None,
            "status": STATUS_UNAVAILABLE,
            "reasons": ["quality_columns_unavailable"],
        }
    summary = _raw_session_summary(_session_rows(conn, trade_date))
    known_bad = _known_bad_record(conn, trade_date)
    if known_bad is not None:
        return {
            "trade_date": str(trade_date),
            **summary,
            "coverage_baseline": None,
            "coverage_ratio": None,
            "status": STATUS_KNOWN_BAD,
            "reasons": [str(known_bad["reason"] or "marked_unavailable")],
            "source_errors": str(known_bad["source_errors"] or ""),
        }

    baseline = _recent_clean_coverage(conn, trade_date, max(1, int(coverage_lookback)))
    return _classify_summary(
        trade_date,
        summary,
        int(median(baseline)) if baseline else None,
        min_coverage_ratio,
    )


def clean_stock_trade_dates(
    conn: sqlite3.Connection,
    *,
    end_date: str | None = None,
    include_end: bool = True,
) -> list[str]:
    if not _has_quality_columns(conn):
        return []
    where = "WHERE sec_type='stock'"
    params: list[str] = []
    if end_date:
        where += f" AND trade_date {'<=' if include_end else '<'} ?"
        params.append(str(end_date))
    dates = [
        str(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT trade_date FROM {_kline_table(conn)} {where} ORDER BY trade_date",
            params,
        ).fetchall()
    ]
    quality_rows = {day: _raw_session_summary(_session_rows(conn, day)) for day in dates}
    clean_coverages: list[int] = []
    result: list[str] = []
    for day in dates:
        if _known_bad_record(conn, day) is not None:
            continue
        baseline = int(median(clean_coverages[-20:])) if clean_coverages else None
        quality = _classify_summary(day, quality_rows[day], baseline, 0.8)
        if quality["status"] == STATUS_CLEAN:
            result.append(day)
            clean_coverages.append(int(quality["valid_rows"]))
    return result
