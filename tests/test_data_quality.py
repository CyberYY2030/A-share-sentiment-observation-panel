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
    STATUS_USABLE_WITH_QUARANTINE,
    clean_stock_trade_dates,
    ensure_session_diagnostics_table,
    ensure_session_revalidations_table,
    inspect_stock_session,
    mark_known_bad_session,
    reinspect_stock_session,
    revalidate_known_bad_session,
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
    def test_public_stock_row_classification_exposes_completion_contract(self) -> None:
        from mining.data_quality import classify_stock_row

        valid = classify_stock_row(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000.0,
                "amount": 100_000.0,
            }
        )
        missing = classify_stock_row(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000.0,
                "amount": None,
            }
        )
        confirmed_halt = classify_stock_row(
            {
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "pre_close": 10.0,
                "volume": 0.0,
                "amount": 0.0,
                "halt_source": "confirmed",
            }
        )
        provider_halt = classify_stock_row(
            {
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "pre_close": 10.0,
                "volume": 0.0,
                "amount": 0.0,
            }
        )
        invalid_price = classify_stock_row(
            {
                "open": 10.0,
                "high": 9.0,
                "low": 9.0,
                "close": 10.0,
                "pre_close": 10.0,
                "volume": 100.0,
                "amount": 1_000.0,
            }
        )
        invalid_activity = classify_stock_row(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 10.0,
                "close": 11.0,
                "pre_close": 10.0,
                "volume": 0.0,
                "amount": 0.0,
            }
        )

        self.assertEqual(valid, {"row_status": "valid_trade", "reason": None, "complete": True, "retryable": False})
        self.assertEqual(
            missing,
            {
                "row_status": "missing",
                "reason": "missing_required_field",
                "complete": False,
                "retryable": True,
            },
        )
        self.assertEqual(confirmed_halt["row_status"], "confirmed_halt")
        self.assertTrue(confirmed_halt["complete"])
        self.assertEqual(provider_halt["row_status"], "provider_halt_placeholder")
        self.assertTrue(provider_halt["complete"])
        self.assertEqual(invalid_price["row_status"], "invalid_price")
        self.assertTrue(invalid_price["retryable"])
        self.assertEqual(invalid_activity["row_status"], "invalid_activity")
        self.assertTrue(invalid_activity["retryable"])

    def test_raw_market_postcondition_ignores_latch_and_requires_all_indexes(self) -> None:
        from mining.data_quality import raw_market_postcondition

        conn = _connect()
        try:
            for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                _insert_clean_day(conn, day)
            _insert_clean_day(conn, "2026-07-06")
            for code in ("000001", "399001", "000300", "000852"):
                conn.execute(
                    "INSERT INTO kline_daily(sec_type, sec_code, trade_date) VALUES ('index', ?, '2026-07-06')",
                    (code,),
                )
            conn.commit()
            mark_known_bad_session(conn, "2026-07-06", reason="old_latch")

            complete = raw_market_postcondition(conn, "2026-07-06")
            conn.execute("DELETE FROM kline_daily WHERE sec_type='index' AND sec_code='000852'")
            conn.commit()
            missing_index = raw_market_postcondition(conn, "2026-07-06")
        finally:
            conn.close()

        self.assertTrue(complete["ok"])
        self.assertTrue(complete["stock"])
        self.assertTrue(complete["index"])
        self.assertEqual(complete["session_quality"]["status"], STATUS_CLEAN)
        self.assertFalse(missing_index["ok"])
        self.assertFalse(missing_index["index"])

    def test_clean_calendar_excludes_full_zero_session_and_keeps_normal_halt(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"):
                _insert_clean_day(conn, day)
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
        self.assertIn("no_valid_trade", bad["reasons"])
        self.assertEqual(halt["status"], STATUS_CLEAN)
        self.assertEqual(halt["normal_halt_rows"], 1)
        self.assertNotIn("2026-07-13", calendar)
        self.assertIn("2026-07-14", calendar)

    def test_partial_missing_ohlc_and_activity_fail_closed(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"):
                _insert_clean_day(conn, day, count=100)
            _insert_clean_day(conn, "2026-07-12", count=90)
            _insert_row(conn, "2026-07-13", "600100", high=9.0)
            _insert_row(conn, "2026-07-14", "600101", open_price=10.0, high=11.0, low=10.0, close=11.0, volume=0.0, amount=0.0)
            conn.commit()

            partial = inspect_stock_session(conn, "2026-07-12")
            invalid_ohlc = inspect_stock_session(conn, "2026-07-13")
            invalid_activity = inspect_stock_session(conn, "2026-07-14")
        finally:
            conn.close()

        self.assertEqual(partial["status"], STATUS_PARTIAL)
        self.assertIn("coverage_ratio_below_0_95", partial["reasons"])
        self.assertEqual(invalid_ohlc["status"], STATUS_BAD)
        self.assertIn("no_valid_trade", invalid_ohlc["reasons"])
        self.assertEqual(invalid_activity["status"], STATUS_BAD)
        self.assertIn("no_valid_trade", invalid_activity["reasons"])

    def test_isolated_zero_provider_placeholder_keeps_the_market_session_usable(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                _insert_clean_day(conn, day)
            _insert_clean_day(conn, "2026-07-06")
            _insert_row(
                conn,
                "2026-07-06",
                "600999",
                open_price=0.0,
                high=0.0,
                low=0.0,
                close=0.0,
                pre_close=12.93,
                volume=0.0,
                amount=0.0,
            )
            conn.commit()
            quality = inspect_stock_session(conn, "2026-07-06")
            calendar = clean_stock_trade_dates(conn)
        finally:
            conn.close()

        self.assertEqual(quality["status"], STATUS_CLEAN)
        self.assertEqual(quality["provider_halt_rows"], 1)
        placeholder = next(row for row in quality["row_status_counts"].items() if row[0] == "provider_halt_placeholder")
        self.assertEqual(placeholder[1], 1)
        self.assertIn("2026-07-06", calendar)

    def test_marketwide_zero_provider_shape_remains_known_bad(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                _insert_clean_day(conn, day)
            for index in range(5):
                _insert_row(
                    conn,
                    "2026-07-06",
                    f"600{index:03d}",
                    open_price=0.0,
                    high=0.0,
                    low=0.0,
                    close=0.0,
                    pre_close=12.93,
                    volume=0.0,
                    amount=0.0,
                )
            conn.commit()
            quality = inspect_stock_session(conn, "2026-07-06")
        finally:
            conn.close()

        self.assertEqual(quality["status"], STATUS_KNOWN_BAD)
        self.assertEqual(quality["provider_halt_rows"], 5)
        self.assertIn("no_valid_trade", quality["reasons"])

    def test_v25_two_axis_boundaries_and_duplicate_conflict_are_deterministic(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                _insert_clean_day(conn, day, count=100)
            _insert_clean_day(conn, "2026-07-06", count=99)
            _insert_row(conn, "2026-07-06", "601999", high=9.0)
            usable = inspect_stock_session(conn, "2026-07-06")
            calendar = clean_stock_trade_dates(conn)
            _insert_clean_day(conn, "2026-07-07", count=36)
            known_bad = inspect_stock_session(conn, "2026-07-07")
            _insert_row(conn, "2026-07-08", "600001")
            _insert_row(conn, "2026-07-08", "600001", close=11.0, high=11.0)
            duplicate = inspect_stock_session(conn, "2026-07-08")
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(usable["status"], STATUS_USABLE_WITH_QUARANTINE)
        self.assertEqual(usable["coverage_ratio"], 1.0)
        self.assertGreaterEqual(usable["usable_ratio"], 0.98)
        self.assertIn("2026-07-06", calendar)
        self.assertEqual(known_bad["status"], STATUS_KNOWN_BAD)
        self.assertLess(known_bad["coverage_ratio"], 0.80)
        self.assertEqual(duplicate["status"], STATUS_KNOWN_BAD)
        self.assertEqual(duplicate["quarantined_rows"][0]["reason"], "conflicting_duplicate")

    def test_proto1_usable_ratio_and_coverage_boundaries(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                _insert_clean_day(conn, day, count=100)
            for index in range(90):
                _insert_row(conn, "2026-07-06", f"60{index:04d}")
            for index in range(10):
                _insert_row(
                    conn,
                    "2026-07-06",
                    f"61{index:04d}",
                    high=11.0,
                    close=11.0,
                    volume=0.0,
                    amount=0.0,
                )
            for index in range(89):
                _insert_row(conn, "2026-07-07", f"62{index:04d}")
            for index in range(11):
                _insert_row(
                    conn,
                    "2026-07-07",
                    f"63{index:04d}",
                    high=11.0,
                    close=11.0,
                    volume=0.0,
                    amount=0.0,
                )
            _insert_clean_day(conn, "2026-07-08", count=94)
            for index in range(10):
                _insert_row(
                    conn,
                    "2026-07-09",
                    f"64{index:04d}",
                    high=11.0,
                    close=11.0,
                    volume=0.0,
                    amount=0.0,
                )
            conn.commit()
            usable = inspect_stock_session(conn, "2026-07-06")
            partial_usable = inspect_stock_session(conn, "2026-07-07")
            partial_coverage = inspect_stock_session(conn, "2026-07-08")
            known_bad = inspect_stock_session(conn, "2026-07-09")
        finally:
            conn.close()

        self.assertEqual(usable["status"], STATUS_USABLE_WITH_QUARANTINE)
        self.assertEqual(usable["usable_ratio"], 0.90)
        self.assertEqual(partial_usable["status"], STATUS_PARTIAL)
        self.assertIn("usable_ratio_below_0_90", partial_usable["reasons"])
        self.assertEqual(partial_coverage["status"], STATUS_PARTIAL)
        self.assertIn("coverage_ratio_below_0_95", partial_coverage["reasons"])
        self.assertEqual(known_bad["status"], STATUS_KNOWN_BAD)
        self.assertIn("no_valid_trade", known_bad["reasons"])

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

    def test_revalidation_retains_bad_latch_until_raw_rows_are_usable_then_clears_atomically(self) -> None:
        conn = _connect()
        try:
            for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                _insert_clean_day(conn, day)
            _insert_clean_day(conn, "2026-07-06", count=3)
            mark_known_bad_session(conn, "2026-07-06", reason="bounded_repair_failed")
            ensure_session_diagnostics_table(conn)
            ensure_session_revalidations_table(conn)

            self.assertEqual(reinspect_stock_session(conn, "2026-07-06")["status"], STATUS_KNOWN_BAD)
            with self.assertRaisesRegex(RuntimeError, "caller-owned"):
                revalidate_known_bad_session(conn, "2026-07-06")

            conn.execute("BEGIN IMMEDIATE")
            retained = revalidate_known_bad_session(conn, "2026-07-06")
            conn.commit()
            self.assertEqual(retained["action"], "retained_after_raw_not_usable")
            self.assertEqual(retained["after"]["status"], STATUS_KNOWN_BAD)

            _insert_row(conn, "2026-07-06", "600003")
            _insert_row(conn, "2026-07-06", "600004")
            conn.commit()
            self.assertEqual(reinspect_stock_session(conn, "2026-07-06")["status"], STATUS_CLEAN)
            conn.execute("BEGIN IMMEDIATE")
            cleared = revalidate_known_bad_session(conn, "2026-07-06")
            conn.commit()

            audit = conn.execute(
                "SELECT before_latched, raw_status, action, after_status "
                "FROM selection_session_revalidations ORDER BY id"
            ).fetchall()
            remaining = conn.execute(
                "SELECT COUNT(*) FROM selection_session_diagnostics WHERE trade_date='2026-07-06'"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(cleared["action"], "cleared_after_raw_usable")
        self.assertEqual(cleared["after"]["status"], STATUS_CLEAN)
        self.assertEqual(audit[0][0], 1)
        self.assertEqual(audit[0][2], "retained_after_raw_not_usable")
        self.assertEqual(audit[1][1], STATUS_CLEAN)
        self.assertEqual(audit[1][2], "cleared_after_raw_usable")
        self.assertEqual(audit[1][3], STATUS_CLEAN)
        self.assertEqual(remaining, 0)

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
                [{"domain": "stock_index", "cmd": ["repair", "--date", "2026-07-13"], "started": True, "output": "AkShare timeout"}],
            )
            repeated = _mark_unrecoverable_bad_stock_sessions(
                stock_path,
                ["2026-07-13"],
                [{"domain": "stock_index", "cmd": ["repair", "--date", "2026-07-13"], "started": True, "output": "AkShare timeout"}],
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
        self.assertEqual(repeated, [])
        self.assertEqual(quality["status"], STATUS_KNOWN_BAD)

    def test_ten_day_repair_plan_skips_quarantined_session_but_repairs_known_bad(self) -> None:
        from offline_daily_update import build_missing_update_plan

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stock_path = base / "a_share_mvp.db"
            conn = _connect(str(stock_path))
            try:
                for day in (f"2026-07-{day:02d}" for day in range(1, 11)):
                    _insert_clean_day(conn, day, count=100)
                _insert_clean_day(conn, "2026-07-11", count=99)
                _insert_row(conn, "2026-07-11", "601999", high=9.0)
                for index in range(5):
                    _insert_row(
                        conn,
                        "2026-07-12",
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

            plan = build_missing_update_plan(
                base,
                ["2026-07-11", "2026-07-12"],
                domains=["stock"],
                stock_min_rows=3,
            )

        coverage = plan["coverage"]
        self.assertEqual(coverage["2026-07-11"]["session_quality"]["status"], STATUS_USABLE_WITH_QUARANTINE)
        self.assertNotIn("2026-07-11", plan["missing_by_day"])
        self.assertEqual(coverage["2026-07-12"]["session_quality"]["status"], STATUS_KNOWN_BAD)
        self.assertEqual(plan["missing_by_day"]["2026-07-12"], ["stock"])


if __name__ == "__main__":
    unittest.main()
