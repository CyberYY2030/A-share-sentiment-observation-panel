from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

from .db import connect, list_stock_trade_dates, now_str


def _compute_return(numerator: float | None, entry_price: float) -> float | None:
    if numerator is None or not math.isfinite(entry_price) or entry_price == 0:
        return None
    return (float(numerator) - entry_price) / entry_price


def backfill_outcomes(
    conn: sqlite3.Connection, trade_date: str | None = None, force: bool = False
) -> dict[str, int]:
    if force:
        rows = conn.execute(
            """
            SELECT candidate_id, trade_date, sec_code, entry_price
            FROM candidates
            WHERE sec_type='stock' AND (? IS NULL OR trade_date=?)
            ORDER BY trade_date, candidate_id
            """,
            (trade_date, trade_date),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT c.candidate_id, c.trade_date, c.sec_code, c.entry_price
            FROM candidates c
            LEFT JOIN outcomes o ON o.candidate_id = c.candidate_id
            WHERE c.sec_type='stock'
              AND (? IS NULL OR c.trade_date=?)
              AND (o.candidate_id IS NULL OR o.status='partial')
            ORDER BY c.trade_date, c.candidate_id
            """,
            (trade_date, trade_date),
        ).fetchall()

    calendar = list_stock_trade_dates(conn)
    calendar_index = {day: idx for idx, day in enumerate(calendar)}

    processed = 0
    completed = 0
    partial = 0
    for row in rows:
        processed += 1
        idx = calendar_index.get(row["trade_date"])
        if idx is None:
            continue

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
            (row["sec_code"], row["trade_date"]),
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
            partial += 1
        else:
            status = "complete"
            completed += 1

        entry_price = float(row["entry_price"])
        r1 = _compute_return(None if t1 is None else t1["open"], entry_price)
        r2 = _compute_return(None if t1 is None else t1["high"], entry_price)
        r3 = _compute_return(None if t1 is None else t1["low"], entry_price)
        r4 = _compute_return(None if t2 is None else t2["close"], entry_price)
        r5 = _compute_return(None if t5 is None else t5["close"], entry_price)
        is_win = None
        if r2 is not None and r3 is not None:
            is_win = 1 if (r2 > 0.02 and r3 > -0.03) else 0

        conn.execute(
            """
            INSERT OR REPLACE INTO outcomes (
              candidate_id, open_t1, high_t1, low_t1, close_t1, close_t2, close_t5,
              r1, r2, r3, r4, r5, is_win, backfilled_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["candidate_id"],
                None if t1 is None else t1["open"],
                None if t1 is None else t1["high"],
                None if t1 is None else t1["low"],
                None if t1 is None else t1["close"],
                None if t2 is None else t2["close"],
                None if t5 is None else t5["close"],
                r1,
                r2,
                r3,
                r4,
                r5,
                is_win,
                now_str(),
                status,
            ),
        )

    conn.commit()
    return {"processed": processed, "complete": completed, "partial": partial}


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

