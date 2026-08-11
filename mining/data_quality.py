from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from collections import Counter
from itertools import groupby
from statistics import median
from typing import Any, Iterable, Mapping


SESSION_DIAGNOSTICS_TABLE = "selection_session_diagnostics"
SESSION_REVALIDATIONS_TABLE = "selection_session_revalidations"
STATUS_CLEAN = "clean"
STATUS_USABLE_WITH_QUARANTINE = "usable_with_quarantine"
STATUS_PARTIAL = "partial_missing"
STATUS_KNOWN_BAD = "known_bad_session"
STATUS_UNAVAILABLE = "quality_unavailable"
# Compatibility for existing repair callers. A systemically unusable session is
# now reported with the explicit v2.5 name rather than an ambiguous "bad" label.
STATUS_BAD = STATUS_KNOWN_BAD

ROW_VALID_TRADE = "valid_trade"
ROW_CONFIRMED_HALT = "confirmed_halt"
ROW_PROVIDER_HALT = "provider_halt_placeholder"
ROW_INVALID_PRICE = "invalid_price"
ROW_INVALID_ACTIVITY = "invalid_activity"
ROW_MISSING = "missing"
_PRICE_USABLE_STATUSES = {ROW_VALID_TRADE, ROW_CONFIRMED_HALT, ROW_PROVIDER_HALT}
_REQUIRED_KLINE_COLUMNS = {"open", "high", "low", "close", "pre_close", "volume", "amount"}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0.0


def _zero_or_missing(value: Any) -> bool:
    return value is None or (_finite(value) and float(value) == 0.0)


def _provider_zero_no_activity_placeholder(
    prices: Mapping[str, Any],
    pre_close: Any,
    volume: Any,
    amount: Any,
) -> bool:
    return (
        all(_finite(value) and float(value) == 0.0 for value in prices.values())
        and _positive(pre_close)
        and _zero_or_missing(volume)
        and _zero_or_missing(amount)
    )


def _value(row: Mapping[str, Any] | sqlite3.Row, column: str) -> Any:
    try:
        return row[column]
    except (KeyError, IndexError):
        return None


def _schema_prefix(conn: sqlite3.Connection) -> str:
    attached = {str(row[1]) for row in conn.execute("PRAGMA database_list").fetchall()}
    return "ash." if "ash" in attached else ""


def _kline_table(conn: sqlite3.Connection) -> str:
    return f"{_schema_prefix(conn)}kline_daily"


def _diagnostics_table(conn: sqlite3.Connection) -> str:
    return f"{_schema_prefix(conn)}{SESSION_DIAGNOSTICS_TABLE}"


def ensure_session_diagnostics_table(conn: sqlite3.Connection, *, commit: bool = True) -> None:
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
    if commit:
        conn.commit()


def ensure_session_revalidations_table(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Create the append-only audit ledger before a caller-owned transaction."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_schema_prefix(conn)}{SESSION_REVALIDATIONS_TABLE} (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          trade_date TEXT NOT NULL,
          revalidated_at TEXT NOT NULL,
          before_latched INTEGER NOT NULL,
          before_status TEXT,
          before_reason TEXT,
          raw_status TEXT NOT NULL,
          raw_reasons TEXT NOT NULL,
          action TEXT NOT NULL,
          after_status TEXT NOT NULL,
          after_reasons TEXT NOT NULL
        )
        """
    )
    if commit:
        conn.commit()


def mark_known_bad_session(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    reason: str,
    source_errors: str = "",
    commit: bool = True,
) -> None:
    ensure_session_diagnostics_table(conn, commit=commit)
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
    if commit:
        conn.commit()


def clear_known_bad_session(conn: sqlite3.Connection, trade_date: str, *, commit: bool = True) -> None:
    ensure_session_diagnostics_table(conn, commit=commit)
    conn.execute(f"DELETE FROM {_diagnostics_table(conn)} WHERE trade_date=?", (str(trade_date),))
    if commit:
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


def _row_status(row: Mapping[str, Any] | sqlite3.Row) -> tuple[str, str | None]:
    prices = {name: _value(row, name) for name in ("open", "high", "low", "close")}
    pre_close = _value(row, "pre_close")
    volume = _value(row, "volume")
    amount = _value(row, "amount")
    if _provider_zero_no_activity_placeholder(prices, pre_close, volume, amount):
        return ROW_PROVIDER_HALT, "provider_zero_no_activity_placeholder"
    if any(value is None for value in (*prices.values(), volume, amount)):
        return ROW_MISSING, "missing_required_field"
    if not all(_finite(value) for value in prices.values()):
        return ROW_INVALID_PRICE, "nonfinite_price"
    if not all(_positive(value) for value in prices.values()):
        return ROW_INVALID_PRICE, "nonpositive_price"
    open_price, high, low, close = (float(prices[name]) for name in ("open", "high", "low", "close"))
    if low > min(open_price, close) or max(open_price, close) > high:
        return ROW_INVALID_PRICE, "ohlc_relation_invalid"

    volume_positive = _positive(volume)
    amount_positive = _positive(amount)
    if volume_positive and amount_positive:
        return ROW_VALID_TRADE, None
    if not (_finite(volume) and _finite(amount)):
        return ROW_MISSING, "missing_activity"

    halt_source = str(_value(row, "halt_source") or _value(row, "halt_status") or "").strip().lower()
    no_activity = float(volume) == 0.0 and float(amount) == 0.0
    if halt_source in {"confirmed", "confirmed_halt", "exchange_confirmed"} and no_activity:
        return ROW_CONFIRMED_HALT, "traceable_halt_source"
    flat_prices = math.isclose(open_price, high) and math.isclose(high, low) and math.isclose(low, close)
    pre_close_matches = _positive(pre_close) and math.isclose(float(pre_close), close, rel_tol=0.0, abs_tol=1e-9)
    if no_activity and flat_prices and pre_close_matches:
        return ROW_PROVIDER_HALT, "provider_flat_no_activity_placeholder"
    return ROW_INVALID_ACTIVITY, "activity_invalid_for_price_move"


def _duplicate_signature(row: Mapping[str, Any] | sqlite3.Row) -> tuple[str, ...]:
    values: list[str] = []
    for column in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
        value = _value(row, column)
        if _finite(value):
            values.append(f"{float(value):.12g}")
        else:
            values.append(repr(value))
    return tuple(values)


def classify_stock_rows(rows: Iterable[Mapping[str, Any] | sqlite3.Row]) -> list[dict[str, Any]]:
    """Return one classified record per ``(trade_date, sec_code)``.

    Identical duplicates are collapsed before every coverage calculation. Conflicting
    duplicates are quarantined instead of depending on SQLite's incidental row order.
    """
    grouped: dict[str, list[Mapping[str, Any] | sqlite3.Row]] = {}
    for row in rows:
        code = str(_value(row, "sec_code") or "").zfill(6)
        grouped.setdefault(code, []).append(row)

    classified: list[dict[str, Any]] = []
    for code in sorted(grouped):
        candidates = grouped[code]
        representative = candidates[0]
        signatures = {_duplicate_signature(row) for row in candidates}
        duplicate_count = len(candidates) - 1
        if len(signatures) > 1:
            status, reason = ROW_INVALID_PRICE, "conflicting_duplicate"
        else:
            status, reason = _row_status(representative)
        classified.append(
            {
                "trade_date": str(_value(representative, "trade_date") or ""),
                "sec_code": code,
                "row_status": status,
                "reason": reason,
                "duplicate_count": duplicate_count,
            }
        )
    return classified


def _session_rows(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT sec_code, trade_date, open, high, low, close, pre_close, volume, amount
        FROM {_kline_table(conn)}
        WHERE sec_type='stock' AND trade_date=?
        """,
        (str(trade_date),),
    ).fetchall()


def _has_quality_columns(conn: sqlite3.Connection) -> bool:
    table = _kline_table(conn).split(".")[-1]
    schema = "ash." if _schema_prefix(conn) else ""
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA {schema}table_info({table})").fetchall()}
    return _REQUIRED_KLINE_COLUMNS.issubset(columns)


def _raw_session_summary(rows: Iterable[Mapping[str, Any] | sqlite3.Row]) -> dict[str, Any]:
    classified = classify_stock_rows(rows)
    counts = Counter(record["row_status"] for record in classified)
    quarantined = [record for record in classified if record["row_status"] not in _PRICE_USABLE_STATUSES]
    return {
        "stock_rows": len(classified),
        "distinct_stock_codes": len(classified),
        "valid_rows": int(counts[ROW_VALID_TRADE]),
        "valid_trade_rows": int(counts[ROW_VALID_TRADE]),
        "confirmed_halt_rows": int(counts[ROW_CONFIRMED_HALT]),
        "provider_halt_rows": int(counts[ROW_PROVIDER_HALT]),
        "normal_halt_rows": int(counts[ROW_PROVIDER_HALT]),
        "price_usable_rows": sum(int(counts[status]) for status in _PRICE_USABLE_STATUSES),
        "isolated_rows": len(quarantined),
        "invalid_close_rows": int(counts[ROW_INVALID_PRICE]),
        "invalid_ohlc_rows": int(counts[ROW_INVALID_PRICE]),
        "invalid_activity_rows": int(counts[ROW_INVALID_ACTIVITY]),
        "nonfinite_rows": sum(1 for record in quarantined if record["reason"] == "nonfinite_price"),
        "missing_rows": int(counts[ROW_MISSING]),
        "duplicate_rows_collapsed": sum(int(record["duplicate_count"]) for record in classified if record["reason"] != "conflicting_duplicate"),
        "conflicting_duplicate_rows": int(counts[ROW_INVALID_PRICE] and sum(1 for record in classified if record["reason"] == "conflicting_duplicate")),
        "row_status_counts": dict(sorted(counts.items())),
        "quarantined_rows": quarantined,
    }


def _recent_reference_coverage(conn: sqlite3.Connection, trade_date: str, lookback: int) -> list[int]:
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
        if summary["stock_rows"]:
            coverages.append(int(summary["distinct_stock_codes"]))
        if len(coverages) >= lookback:
            break
    return coverages


def _classify_summary(
    trade_date: str,
    summary: dict[str, Any],
    coverage_baseline: int | None,
) -> dict[str, Any]:
    coverage_ratio = (
        float(summary["distinct_stock_codes"]) / float(coverage_baseline)
        if coverage_baseline and coverage_baseline > 0
        else None
    )
    usable_ratio = (
        float(summary["price_usable_rows"]) / float(summary["distinct_stock_codes"])
        if summary["distinct_stock_codes"]
        else None
    )
    result: dict[str, Any] = {
        "trade_date": str(trade_date),
        **summary,
        "coverage_baseline": coverage_baseline,
        "coverage_ratio": coverage_ratio,
        "usable_ratio": usable_ratio,
        "status": STATUS_UNAVAILABLE,
        "reasons": [],
    }
    if summary["stock_rows"] == 0:
        result.update(status=STATUS_KNOWN_BAD, reasons=["missing_stock_rows"])
        return result
    if summary["valid_trade_rows"] == 0:
        result.update(status=STATUS_KNOWN_BAD, reasons=["no_valid_trade"])
        return result
    if usable_ratio is not None and usable_ratio < 0.80:
        result.update(status=STATUS_KNOWN_BAD, reasons=["usable_ratio_below_0_80"])
        return result
    if coverage_ratio is not None and coverage_ratio < 0.80:
        result.update(status=STATUS_KNOWN_BAD, reasons=["coverage_ratio_below_0_80"])
        return result
    if coverage_baseline is None:
        result.update(status=STATUS_UNAVAILABLE, reasons=["coverage_baseline_unavailable"])
        return result
    if coverage_ratio < 0.95 or usable_ratio < 0.98:
        reasons: list[str] = []
        if coverage_ratio < 0.95:
            reasons.append("coverage_ratio_below_0_95")
        if usable_ratio < 0.98:
            reasons.append("usable_ratio_below_0_98")
        result.update(status=STATUS_PARTIAL, reasons=reasons)
        return result
    if summary["isolated_rows"]:
        result.update(status=STATUS_USABLE_WITH_QUARANTINE, reasons=["row_quarantine"])
        return result
    result.update(status=STATUS_CLEAN, reasons=[])
    return result


def inspect_stock_session(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    coverage_lookback: int = 20,
    min_coverage_ratio: float = 0.8,
) -> dict[str, Any]:
    """Classify a stock session with independent coverage and usable-row axes."""
    del min_coverage_ratio  # v2.5 freezes the 0.80/0.95/0.98 state machine.
    if not _has_quality_columns(conn):
        return {
            "trade_date": str(trade_date),
            "stock_rows": 0,
            "valid_rows": 0,
            "normal_halt_rows": 0,
            "coverage_baseline": None,
            "coverage_ratio": None,
            "usable_ratio": None,
            "status": STATUS_UNAVAILABLE,
            "reasons": ["quality_columns_unavailable"],
            "quarantined_rows": [],
        }
    summary = _raw_session_summary(_session_rows(conn, trade_date))
    known_bad = _known_bad_record(conn, trade_date)
    if known_bad is not None:
        return {
            "trade_date": str(trade_date),
            **summary,
            "coverage_baseline": None,
            "coverage_ratio": None,
            "usable_ratio": None,
            "status": STATUS_KNOWN_BAD,
            "reasons": [str(known_bad["reason"] or "marked_unavailable")],
            "source_errors": str(known_bad["source_errors"] or ""),
        }
    baseline = _recent_reference_coverage(conn, trade_date, max(1, int(coverage_lookback)))
    return _classify_summary(
        trade_date,
        summary,
        int(median(baseline)) if len(baseline) >= 5 else None,
    )


def reinspect_stock_session(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    coverage_lookback: int = 20,
) -> dict[str, Any]:
    """Classify the raw session while deliberately ignoring its diagnostic latch."""
    if not _has_quality_columns(conn):
        return {
            "trade_date": str(trade_date),
            "stock_rows": 0,
            "valid_rows": 0,
            "normal_halt_rows": 0,
            "coverage_baseline": None,
            "coverage_ratio": None,
            "usable_ratio": None,
            "status": STATUS_UNAVAILABLE,
            "reasons": ["quality_columns_unavailable"],
            "quarantined_rows": [],
        }
    summary = _raw_session_summary(_session_rows(conn, trade_date))
    baseline = _recent_reference_coverage(conn, trade_date, max(1, int(coverage_lookback)))
    return _classify_summary(
        trade_date,
        summary,
        int(median(baseline)) if len(baseline) >= 5 else None,
    )


def revalidate_known_bad_session(conn: sqlite3.Connection, trade_date: str) -> dict[str, Any]:
    """Atomically retain or clear a known-bad latch from freshly inspected raw rows.

    The caller owns ``BEGIN IMMEDIATE`` / commit / rollback.  Both schema helpers
    must be run before the transaction, so a failure cannot leave a half-created
    audit table beside a changed diagnostic record.
    """
    if not conn.in_transaction:
        raise RuntimeError("revalidation requires a caller-owned active transaction")

    before = _known_bad_record(conn, trade_date)
    raw = reinspect_stock_session(conn, trade_date)
    raw_status = str(raw["status"])
    raw_reasons = [str(reason) for reason in raw.get("reasons", [])]
    raw_is_usable = raw_status in {STATUS_CLEAN, STATUS_USABLE_WITH_QUARANTINE}

    if raw_is_usable:
        action = "cleared_after_raw_usable" if before is not None else "raw_usable_without_latch"
        if before is not None:
            clear_known_bad_session(conn, trade_date, commit=False)
    else:
        if before is None:
            action = "raw_not_usable_without_latch"
        else:
            action = "retained_after_raw_not_usable"
            mark_known_bad_session(
                conn,
                trade_date,
                reason="revalidation_raw_" + (raw_reasons[0] if raw_reasons else raw_status),
                source_errors=str(before["source_errors"] or ""),
                commit=False,
            )

    after = inspect_stock_session(conn, trade_date)
    conn.execute(
        f"""
        INSERT INTO {_schema_prefix(conn)}{SESSION_REVALIDATIONS_TABLE} (
          trade_date, revalidated_at, before_latched, before_status, before_reason,
          raw_status, raw_reasons, action, after_status, after_reasons
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(trade_date),
            dt.datetime.now().isoformat(timespec="seconds"),
            int(before is not None),
            STATUS_KNOWN_BAD if before is not None else None,
            str(before["reason"] or "") if before is not None else None,
            raw_status,
            json.dumps(raw_reasons, ensure_ascii=False),
            action,
            str(after["status"]),
            json.dumps([str(reason) for reason in after.get("reasons", [])], ensure_ascii=False),
        ),
    )
    return {"trade_date": str(trade_date), "before_latched": before is not None, "raw": raw, "action": action, "after": after}


def inspect_stock_sessions(
    conn: sqlite3.Connection,
    *,
    end_date: str | None = None,
    include_end: bool = True,
) -> list[dict[str, Any]]:
    """Classify a date range in one pass without repeatedly rebuilding baselines."""
    if not _has_quality_columns(conn):
        return []
    where = "WHERE sec_type='stock'"
    params: list[str] = []
    if end_date:
        where += f" AND trade_date {'<=' if include_end else '<'} ?"
        params.append(str(end_date))
    dates = [
        str(row[0])
        for row in conn.execute(f"SELECT DISTINCT trade_date FROM {_kline_table(conn)} {where} ORDER BY trade_date", params).fetchall()
    ]
    row_cursor = conn.execute(
        f"""
        SELECT sec_code, trade_date, open, high, low, close, pre_close, volume, amount
        FROM {_kline_table(conn)}
        {where}
        ORDER BY trade_date
        """,
        params,
    )
    summaries = {
        day: _raw_session_summary(rows)
        for day, rows in (
            (day, list(group))
            for day, group in groupby(row_cursor, key=lambda row: str(row["trade_date"]))
        )
    }
    try:
        manual_known_bad = {
            str(row[0]): row
            for row in conn.execute(
                f"SELECT trade_date, reason, source_errors FROM {_diagnostics_table(conn)} WHERE status=?",
                (STATUS_KNOWN_BAD,),
            ).fetchall()
        }
    except sqlite3.OperationalError:
        manual_known_bad = {}

    reference_coverages: list[int] = []
    qualities: list[dict[str, Any]] = []
    for day in dates:
        summary = summaries[day]
        known_bad = manual_known_bad.get(day)
        if known_bad is not None:
            quality = {
                "trade_date": day,
                **summary,
                "coverage_baseline": None,
                "coverage_ratio": None,
                "usable_ratio": None,
                "status": STATUS_KNOWN_BAD,
                "reasons": [str(known_bad[1] or "marked_unavailable")],
                "source_errors": str(known_bad[2] or ""),
            }
        else:
            baseline = int(median(reference_coverages[-20:])) if len(reference_coverages) >= 5 else None
            quality = _classify_summary(day, summary, baseline)
        if known_bad is None and summary["stock_rows"]:
            reference_coverages.append(int(summary["distinct_stock_codes"]))
        qualities.append(quality)
    return qualities


def usable_stock_trade_dates(
    conn: sqlite3.Connection,
    *,
    end_date: str | None = None,
    include_end: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return v2.5 usable sessions and their row-level quarantine ledger."""
    usable: list[str] = []
    quarantined: list[dict[str, Any]] = []
    for quality in inspect_stock_sessions(conn, end_date=end_date, include_end=include_end):
        if quality["status"] in {STATUS_CLEAN, STATUS_USABLE_WITH_QUARANTINE}:
            usable.append(str(quality["trade_date"]))
            quarantined.extend(quality.get("quarantined_rows", []))
    return usable, quarantined


def clean_stock_trade_dates(
    conn: sqlite3.Connection,
    *,
    end_date: str | None = None,
    include_end: bool = True,
) -> list[str]:
    """Compatibility alias for the v2.5 usable market-date calendar."""
    return usable_stock_trade_dates(conn, end_date=end_date, include_end=include_end)[0]
