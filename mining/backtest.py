from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

from .capabilities import SCREENING_DEFINITION_VERSION, formal_strategy_ids
from .db import connect, list_stock_trade_dates, now_str


def _compute_return(numerator: float | None, entry_price: float) -> float | None:
    if numerator is None or not math.isfinite(entry_price) or entry_price == 0:
        return None
    return (float(numerator) - entry_price) / entry_price


def _forward_bars(
    conn: sqlite3.Connection,
    sec_code: str,
    trade_date: str,
    calendar: list[str] | None = None,
) -> dict[str, object] | None:
    calendar = calendar if calendar is not None else list_stock_trade_dates(conn)
    calendar_index = {day: idx for idx, day in enumerate(calendar)}
    idx = calendar_index.get(trade_date)
    if idx is None or idx + 1 >= len(calendar):
        return None

    target_dates = {
        "t1": calendar[idx + 1] if idx + 1 < len(calendar) else None,
        "t2": calendar[idx + 2] if idx + 2 < len(calendar) else None,
        "t5": calendar[idx + 5] if idx + 5 < len(calendar) else None,
    }
    future_rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND sec_code=?
          AND trade_date > ?
        ORDER BY trade_date
        """,
        (sec_code, trade_date),
    ).fetchall()
    future_map = {future["trade_date"]: future for future in future_rows}
    t1 = future_map.get(target_dates["t1"]) if target_dates["t1"] else None
    t2 = future_map.get(target_dates["t2"]) if target_dates["t2"] else None
    t5 = future_map.get(target_dates["t5"]) if target_dates["t5"] else None

    if not future_rows:
        status = "delisted"
    elif t1 is None:
        status = "halted"
    elif t2 is None or t5 is None:
        status = "partial"
    else:
        status = "complete"
    return {"t1": t1, "t2": t2, "t5": t5, "status": status}


def _forward_outcome_values(entry_price: float, bars: dict[str, object]) -> dict[str, object]:
    t1 = bars.get("t1")
    t2 = bars.get("t2")
    t5 = bars.get("t5")
    r1 = _compute_return(None if t1 is None else t1["open"], entry_price)
    r2 = _compute_return(None if t1 is None else t1["high"], entry_price)
    r3 = _compute_return(None if t1 is None else t1["low"], entry_price)
    r4 = _compute_return(None if t2 is None else t2["close"], entry_price)
    r5 = _compute_return(None if t5 is None else t5["close"], entry_price)
    is_win = None
    if r2 is not None and r3 is not None:
        is_win = 1 if (r2 > 0.02 and r3 > -0.03) else 0
    return {
        "open_t1": None if t1 is None else t1["open"],
        "high_t1": None if t1 is None else t1["high"],
        "low_t1": None if t1 is None else t1["low"],
        "close_t1": None if t1 is None else t1["close"],
        "close_t2": None if t2 is None else t2["close"],
        "close_t5": None if t5 is None else t5["close"],
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "r4": r4,
        "r5": r5,
        "is_win": is_win,
        "status": bars["status"],
    }


def backfill_outcomes(
    conn: sqlite3.Connection, trade_date: str | None = None, force: bool = False
) -> dict[str, int]:
    formal_ids = formal_strategy_ids()
    formal_marks = ",".join("?" for _ in formal_ids)
    if force:
        rows = conn.execute(
            f"""
            SELECT candidate_id, trade_date, sec_code, entry_price
            FROM candidates c
            WHERE c.sec_type='stock' AND (? IS NULL OR c.trade_date=?)
              AND NOT (c.version=? AND c.strategy_id IN ({formal_marks}))
            ORDER BY trade_date, candidate_id
            """,
            (trade_date, trade_date, SCREENING_DEFINITION_VERSION, *formal_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT c.candidate_id, c.trade_date, c.sec_code, c.entry_price
            FROM candidates c
            LEFT JOIN outcomes o ON o.candidate_id = c.candidate_id
            WHERE c.sec_type='stock'
              AND (? IS NULL OR c.trade_date=?)
              AND NOT (c.version=? AND c.strategy_id IN ({formal_marks}))
              AND (o.candidate_id IS NULL OR o.status != 'complete')
            ORDER BY c.trade_date, c.candidate_id
            """,
            (trade_date, trade_date, SCREENING_DEFINITION_VERSION, *formal_ids),
        ).fetchall()

    processed = 0
    completed = 0
    partial = 0
    skipped = 0
    delisted = 0
    halted = 0
    calendar = list_stock_trade_dates(conn)
    for row in rows:
        processed += 1
        bars = _forward_bars(conn, row["sec_code"], row["trade_date"], calendar=calendar)
        if bars is None:
            skipped += 1
            conn.execute("DELETE FROM outcomes WHERE candidate_id=?", (row["candidate_id"],))
            continue
        values = _forward_outcome_values(float(row["entry_price"]), bars)
        if values["status"] == "complete":
            completed += 1
        elif values["status"] == "partial":
            partial += 1
        elif values["status"] == "delisted":
            delisted += 1
        elif values["status"] == "halted":
            halted += 1

        conn.execute(
            """
            INSERT OR REPLACE INTO outcomes (
              candidate_id, open_t1, high_t1, low_t1, close_t1, close_t2, close_t5,
              r1, r2, r3, r4, r5, is_win, backfilled_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["candidate_id"],
                values["open_t1"],
                values["high_t1"],
                values["low_t1"],
                values["close_t1"],
                values["close_t2"],
                values["close_t5"],
                values["r1"],
                values["r2"],
                values["r3"],
                values["r4"],
                values["r5"],
                values["is_win"],
                now_str(),
                values["status"],
            ),
        )

    conn.commit()
    return {
        "processed": processed,
        "complete": completed,
        "partial": partial,
        "skipped": skipped,
        "delisted": delisted,
        "halted": halted,
    }


def backfill_snapshot_outcomes(
    conn: sqlite3.Connection,
    snapshot_date: str | None = None,
    force: bool = False,
) -> dict[str, int]:
    if force:
        rows = conn.execute(
            """
            SELECT snapshot_date, sec_code, state, entry_price
            FROM watchlist_snapshots
            WHERE (? IS NULL OR snapshot_date=?)
            ORDER BY snapshot_date, sec_code, state
            """,
            (snapshot_date, snapshot_date),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.snapshot_date, s.sec_code, s.state, s.entry_price
            FROM watchlist_snapshots s
            LEFT JOIN watchlist_outcomes o
              ON o.snapshot_date=s.snapshot_date
             AND o.sec_code=s.sec_code
             AND o.state=s.state
            WHERE (? IS NULL OR s.snapshot_date=?)
              AND (o.snapshot_date IS NULL OR o.status != 'complete')
            ORDER BY s.snapshot_date, s.sec_code, s.state
            """,
            (snapshot_date, snapshot_date),
        ).fetchall()

    processed = 0
    completed = 0
    partial = 0
    skipped = 0
    delisted = 0
    halted = 0
    calendar = list_stock_trade_dates(conn)
    for row in rows:
        processed += 1
        bars = _forward_bars(conn, row["sec_code"], row["snapshot_date"], calendar=calendar)
        if bars is None:
            skipped += 1
            conn.execute(
                """
                DELETE FROM watchlist_outcomes
                WHERE snapshot_date=? AND sec_code=? AND state=?
                """,
                (row["snapshot_date"], row["sec_code"], row["state"]),
            )
            continue
        values = _forward_outcome_values(float(row["entry_price"]), bars)
        if values["status"] == "complete":
            completed += 1
        elif values["status"] == "partial":
            partial += 1
        elif values["status"] == "delisted":
            delisted += 1
        elif values["status"] == "halted":
            halted += 1

        conn.execute(
            """
            INSERT OR REPLACE INTO watchlist_outcomes (
              snapshot_date, sec_code, state, open_t1, high_t1, low_t1,
              close_t1, close_t2, close_t5, r1, r2, r3, r4, r5,
              is_win, backfilled_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["snapshot_date"],
                row["sec_code"],
                row["state"],
                values["open_t1"],
                values["high_t1"],
                values["low_t1"],
                values["close_t1"],
                values["close_t2"],
                values["close_t5"],
                values["r1"],
                values["r2"],
                values["r3"],
                values["r4"],
                values["r5"],
                values["is_win"],
                now_str(),
                values["status"],
            ),
        )

    conn.commit()
    return {
        "processed": processed,
        "complete": completed,
        "partial": partial,
        "skipped": skipped,
        "delisted": delisted,
        "halted": halted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill mining outcomes.")
    parser.add_argument("--date", dest="trade_date")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--base-dir", default=Path.cwd())
    args = parser.parse_args()

    conn = connect(base_dir=args.base_dir)
    try:
        result = backfill_outcomes(conn, trade_date=args.trade_date, force=args.force)
    finally:
        conn.close()

    print(
        f"processed={result['processed']} complete={result['complete']} partial={result['partial']}"
    )


if __name__ == "__main__":
    main()


