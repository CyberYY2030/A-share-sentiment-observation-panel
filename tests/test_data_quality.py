from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mining.data_quality import (
    STATUS_BAD,
    STATUS_CLEAN,
    STATUS_KNOWN_BAD,
    STATUS_PARTIAL,
    clean_stock_trade_dates,
    inspect_stock_session,
    mark_known_bad_session,
)


def _connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE kline_daily (
          sec_type TEXT,
          sec_code TEXT,
          trade_date TEXT,
          open REAL,
          high REAL,
          low REAL,
          close REAL,
          pre_close REAL,
          volume REAL,
          amount REAL
        )
        """
    )
    return conn


def _insert_row(
    conn: sqlite3.Connection,
    day: str,
    code: str,
    *,
    open_price: float = 10.0,
    high: float = 10.0,
    low: float = 10.0,
    close: float = 10.0,
    pre_close: float = 10.0,
    volume: float = 100.0,
    amount: float = 1_000.0,
) -> None:
    conn.execute(
        """
        INSERT INTO kline_daily VALUES ('stock', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (code, day, open_price, high, low, close, pre_close, volume, amount),
    )


def _insert_clean_day(conn: sqlite3.Connection, day: str, count: int = 5) -> None:
    for index in range(count):
        _insert_row(conn, day, f"600{index:03d}")
    conn.commit()


class DataQualityTests(unittest.TestCase):
    def test_clean_calendar_excludes_full_zero_session_and_keeps_normal_halt(self) -> None:
        conn = _connect()
        try:
            _insert_clean_day(conn, "2026-07-10")
            _insert_clean_day(conn, "2026-07-11")
            for index in range(5):
                _insert_row(
                    conn,
                    "2026-07-13",
                    f"600{index:03d}",
                    open_price=0.0,
                    high=0.0,
                    low=0.0,
                    close=0.0,
                    pre_close=0.0,
                    volume=0.0,
                    amount=0.0,
                )
            _insert_row(conn, "2026-07-14", "600999", volume=0.0, amount=0.0)
            _insert_clean_day(conn, "2026-07-14")
            conn.commit()

            bad = inspect_stock_session(conn, "2026-07-13")
            halt = inspect_stock_session(conn, "2026-07-14")
            calendar = clean_stock_trade_dates(conn)
        finally:
            conn.close()

        self.assertEqual(bad["status"], STATUS_BAD)
        self.assertIn("invalid_close", bad["reasons"])
        self.assertEqual(halt["status"], STATUS_CLEAN)
        self.assertEqual(halt["normal_halt_rows"], 1)
        self.assertNotIn("2026-07-13", calendar)
        self.assertIn("2026-07-14", calendar)

    def test_partial_missing_ohlc_and_activity_fail_closed(self) -> None:
        conn = _connect()
        try:
            _insert_clean_day(conn, "2026-07-10")
            _insert_clean_day(conn, "2026-07-11")
            _insert_clean_day(conn, "2026-07-12", count=3)
            _insert_row(conn, "2026-07-13", "600100", high=9.0)
            _insert_row(conn, "2026-07-14", "600101", open_price=10.0, high=11.0, low=10.0, close=11.0, volume=0.0, amount=0.0)
            conn.commit()

            partial = inspect_stock_session(conn, "2026-07-12")
            invalid_ohlc = inspect_stock_session(conn, "2026-07-13")
            invalid_activity = inspect_stock_session(conn, "2026-07-14")
        finally:
            conn.close()

        self.assertEqual(partial["status"], STATUS_PARTIAL)
        self.assertIn("coverage_below_clean_median", partial["reasons"])
        self.assertEqual(invalid_ohlc["status"], STATUS_BAD)
        self.assertIn("invalid_ohlc", invalid_ohlc["reasons"])
        self.assertEqual(invalid_activity["status"], STATUS_BAD)
        self.assertIn("activity_without_volume_or_amount", invalid_activity["reasons"])

    def test_known_bad_session_remains_explicit_and_is_not_calendar_data(self) -> None:
        conn = _connect()
        try:
            _insert_clean_day(conn, "2026-07-10")
            mark_known_bad_session(conn, "2026-07-10", reason="all_sources_failed", source_errors="AkShare: timeout")
            quality = inspect_stock_session(conn, "2026-07-10")
            calendar = clean_stock_trade_dates(conn)
        finally:
            conn.close()

        self.assertEqual(quality["status"], STATUS_KNOWN_BAD)
        self.assertEqual(quality["source_errors"], "AkShare: timeout")
        self.assertEqual(calendar, [])

    def test_existing_but_bad_day_enters_repair_plan_and_failure_marks_it_known_bad(self) -> None:
        from offline_daily_update import (
            _mark_unrecoverable_bad_stock_sessions,
            build_missing_update_plan,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stock_path = base / "a_share_mvp.db"
            conn = _connect(str(stock_path))
            try:
                for index in range(5):
                    _insert_row(
                        conn,
                        "2026-07-13",
                        f"600{index:03d}",
                        open_price=0.0,
                        high=0.0,
                        low=0.0,
                        close=0.0,
                        pre_close=0.0,
                        volume=0.0,
                        amount=0.0,
                    )
                conn.commit()
            finally:
                conn.close()

            plan = build_missing_update_plan(base, ["2026-07-13"], domains=["stock"], stock_min_rows=3)
            marked = _mark_unrecoverable_bad_stock_sessions(
                stock_path,
                ["2026-07-13"],
                [{"domain": "stock_index", "cmd": ["repair", "--date", "2026-07-13"], "output": "AkShare timeout"}],
            )
            conn = sqlite3.connect(stock_path)
            conn.row_factory = sqlite3.Row
            try:
                quality = inspect_stock_session(conn, "2026-07-13")
            finally:
                conn.close()

        self.assertEqual(plan["missing_by_day"]["2026-07-13"], ["stock"])
        self.assertEqual(plan["coverage"]["2026-07-13"]["session_quality"]["status"], STATUS_BAD)
        self.assertEqual(marked, ["2026-07-13"])
        self.assertEqual(quality["status"], STATUS_KNOWN_BAD)


if __name__ == "__main__":
    unittest.main()
