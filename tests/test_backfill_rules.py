import unittest
import tempfile
from pathlib import Path
from unittest import mock


class BackfillRuleTests(unittest.TestCase):
    def test_initial_build_uses_full_history_window(self) -> None:
        from app_panel import calc_backfill_days

        self.assertEqual(calc_backfill_days(None, "2026-04-10", cap=15), 60)

    def test_close_day_catchup_starts_background_daily_backfill_when_snapshot_is_unsafe(self) -> None:
        import app_panel as panel

        local_before = {"stock": "2026-04-21", "index": "2026-04-21", "concept": None, "etf": None}

        with (
            mock.patch.object(panel, "can_use_snapshot_for_close_target", return_value=False),
            mock.patch.object(panel, "pick_existing_script", return_value="backfill_baostock_hsA_60d_v2.py"),
            mock.patch.object(
                panel,
                "start_background_job",
                return_value={"pid": 1234, "log_path": "output/backfill_jobs/test.log"},
            ) as start_job,
            mock.patch.object(panel, "run_cmd", side_effect=AssertionError("startup catch-up must not block")),
            mock.patch.object(panel, "get_local_last_dates", return_value=local_before),
        ):
            result, logs = panel.maybe_backfill_to_close_day(
                stock_db="a_share_mvp.db",
                concept_db=None,
                etf_db=None,
                target_day="2026-04-22",
                local_dates=local_before,
                include_concept=False,
                include_etf=False,
            )

        self.assertEqual(result["stock"], "2026-04-21")
        start_job.assert_called_once()
        self.assertTrue(any("[TARGET_BAOSTOCK_BACKGROUND]" in line for line in logs))

    def test_etf_ref_dates_must_cover_requested_asof(self) -> None:
        from backfill_etf_equity_60d_v2 import ref_dates_cover_asof

        self.assertFalse(ref_dates_cover_asof(["2026-04-18", "2026-04-21"], "2026-04-22"))
        self.assertTrue(ref_dates_cover_asof(["2026-04-21", "2026-04-22"], "2026-04-22"))

    def test_etf_ref_dates_are_filtered_by_asof_before_tail_selection(self) -> None:
        import sqlite3

        from backfill_etf_equity_60d_v2 import load_trade_dates_from_ref_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ref.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE kline_daily (trade_date TEXT)")
                for day in [
                    "2026-04-08",
                    "2026-04-09",
                    "2026-04-10",
                    "2026-04-13",
                    "2026-04-14",
                    "2026-04-15",
                    "2026-04-16",
                    "2026-04-17",
                    "2026-04-20",
                    "2026-04-21",
                    "2026-04-22",
                    "2026-04-23",
                ]:
                    conn.execute("INSERT INTO kline_daily VALUES (?)", (day,))
                conn.commit()
            finally:
                conn.close()

            dates = load_trade_dates_from_ref_db(str(db_path), 1, asof="2026-04-22")

        self.assertEqual(dates, ["2026-04-22"])

    def test_offline_update_plan_detects_middle_gap_even_when_latest_is_newer(self) -> None:
        import sqlite3

        from offline_daily_update import build_missing_update_plan

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conn = sqlite3.connect(base / "a_share_mvp.db")
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                        trade_date TEXT,
                        sec_type TEXT,
                        sec_code TEXT,
                        close REAL
                    )
                    """
                )
                for day in ("2026-04-21", "2026-04-23"):
                    conn.execute(
                        "INSERT INTO kline_daily VALUES (?, 'stock', '600001', 10.0)",
                        (day,),
                    )
                    for code in ("000001", "399001", "000300", "000852"):
                        conn.execute(
                            "INSERT INTO kline_daily VALUES (?, 'index', ?, 10.0)",
                            (day, code),
                        )
                conn.commit()
            finally:
                conn.close()

            plan = build_missing_update_plan(
                base,
                ["2026-04-21", "2026-04-22", "2026-04-23"],
                stock_min_rows=1,
                concept_min_rows=1,
                etf_min_rows=1,
            )

        self.assertIn("2026-04-22", plan["missing_by_day"])
        self.assertIn("stock", plan["missing_by_day"]["2026-04-22"])
        self.assertIn("index", plan["missing_by_day"]["2026-04-22"])
        self.assertIn("2026-04-22", plan["missing_by_domain"]["stock"])
        self.assertNotIn("2026-04-23", plan["missing_by_domain"]["stock"])

    def test_expected_trade_days_use_market_calendar_not_plain_weekdays(self) -> None:
        from unittest import mock

        from offline_daily_update import resolve_expected_trade_days

        calendar_days = [
            "2026-04-23",
            "2026-04-24",
            "2026-04-27",
            "2026-04-28",
            "2026-04-29",
            "2026-04-30",
            "2026-05-06",
        ]
        with mock.patch(
            "offline_daily_update._akshare_trade_days",
            return_value=calendar_days,
            create=True,
        ):
            days = resolve_expected_trade_days(
                "a_share_mvp.db",
                asof="2026-05-06",
                days=5,
            )

        self.assertEqual(days, ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06"])
        self.assertNotIn("2026-05-01", days)
        self.assertNotIn("2026-05-04", days)
        self.assertNotIn("2026-05-05", days)

    def test_missing_window_size_keeps_single_day_stock_repair_narrow(self) -> None:
        from offline_daily_update import _missing_window_size

        expected = ["2026-04-17", "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23"]

        self.assertEqual(_missing_window_size(expected, ["2026-04-22"]), 1)
        self.assertEqual(_missing_window_size(expected, ["2026-04-20", "2026-04-22"]), 3)

    def test_run_with_retries_stops_after_three_failures(self) -> None:
        from offline_daily_update import _run_with_retries

        calls = []

        def runner(_cmd, cwd, timeout_sec):
            calls.append((tuple(_cmd), str(cwd), timeout_sec))
            return 999, "interface failed"

        rc, out, attempts = _run_with_retries(
            ["fake-source"],
            cwd=".",
            timeout_sec=1,
            runner=runner,
        )

        self.assertEqual(rc, 999)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(calls), 3)
        self.assertIn("attempt 3/3", out)

    def test_akshare_stock_fallback_aborts_after_three_extraction_failures(self) -> None:
        import sqlite3

        from offline_daily_update import akshare_stock_index_backfill

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.executemany(
                    "INSERT INTO stock_info(sec_code, bs_code, name) VALUES(?,?,?)",
                    [
                        ("600001", "sh.600001", "A"),
                        ("600002", "sh.600002", "B"),
                        ("600003", "sh.600003", "C"),
                        ("600004", "sh.600004", "D"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            class FakeAk:
                def stock_zh_a_hist(self, **_kwargs):
                    raise RuntimeError("source down")

            result = akshare_stock_index_backfill(db_path, "2026-04-22", ak_module=FakeAk())

        self.assertFalse(result["ok"])
        self.assertLessEqual(result["attempts"], 5)
        self.assertIn("probe", result["error"])

    def test_akshare_stock_fallback_aborts_when_probe_cannot_read_target_day(self) -> None:
        import sqlite3

        import pandas as pd

        from offline_daily_update import akshare_stock_index_backfill

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.executemany(
                    "INSERT INTO stock_info(sec_code, bs_code, name) VALUES(?,?,?)",
                    [
                        ("600000", "sh.600000", "A"),
                        ("000001", "sz.000001", "B"),
                        ("300750", "sz.300750", "C"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            class FakeAk:
                def stock_zh_a_hist(self, **_kwargs):
                    return pd.DataFrame()

            result = akshare_stock_index_backfill(db_path, "2026-05-06", ak_module=FakeAk())

        self.assertFalse(result["ok"])
        self.assertLessEqual(result["attempts"], 3)
        self.assertIn("probe", result["error"])

    def test_offline_update_does_not_start_slow_baostock_after_akshare_market_failure(self) -> None:
        import sqlite3

        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conn = sqlite3.connect(base / "a_share_mvp.db")
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                        trade_date TEXT,
                        sec_type TEXT,
                        sec_code TEXT,
                        close REAL
                    )
                    """
                )
                conn.execute("INSERT INTO kline_daily VALUES ('2026-04-29', 'stock', '600001', 10.0)")
                for code in ("000001", "399001", "000300", "000852"):
                    conn.execute("INSERT INTO kline_daily VALUES ('2026-04-29', 'index', ?, 10.0)", (code,))
                conn.commit()
            finally:
                conn.close()

            with (
                mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=["2026-04-29", "2026-04-30"]),
                mock.patch("offline_daily_update.akshare_stock_index_backfill", return_value={"ok": False, "rows": 0, "attempts": 1, "error": "source down"}),
                mock.patch("offline_daily_update._find_script", return_value=None),
            ):
                result = run_offline_update(base, asof="2026-04-30", days=2, timeout_sec=30)

        self.assertFalse(result["ok"])
        self.assertTrue(any("skip baostock" in line for line in result["logs"]))

    def test_repair_market_day_skips_existing_target_day_stock_rows(self) -> None:
        import sqlite3

        from repair_market_day_akshare import KLINE_COLS, repair_day

        fetched_codes: list[str] = []

        def kline_row(sec_type: str, code: str, trade_date: str) -> tuple:
            values = {
                "sec_type": sec_type,
                "sec_code": code,
                "trade_date": trade_date,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "change_pct": 5.0,
                "volume": 1000.0,
                "amount": 100000.0,
                "turnover_ratio": 1.0,
                "source": "test",
                "updated_at": "2026-05-20T00:00:00",
            }
            return tuple(values[col] for col in KLINE_COLS)

        def fake_stock_fetch(code: str, day_dash: str, _day_compact: str) -> dict:
            fetched_codes.append(code)
            values = dict(zip(KLINE_COLS, kline_row("stock", code, day_dash)))
            return values

        def fake_index_fetch(code: str, day_dash: str, _day_compact: str) -> dict:
            values = dict(zip(KLINE_COLS, kline_row("index", code, day_dash)))
            return values

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.executemany(
                    "INSERT INTO stock_info(sec_code, bs_code, name) VALUES(?,?,?)",
                    [
                        ("600001", "sh.600001", "A"),
                        ("600002", "sh.600002", "B"),
                        ("600003", "sh.600003", "C"),
                    ],
                )
                conn.execute(
                    f"""
                    CREATE TABLE kline_daily (
                      {",".join(col + " TEXT" for col in KLINE_COLS)},
                      PRIMARY KEY (sec_type, sec_code, trade_date)
                    )
                    """
                )
                conn.execute(
                    f"INSERT INTO kline_daily ({','.join(KLINE_COLS)}) VALUES ({','.join(['?'] * len(KLINE_COLS))})",
                    kline_row("stock", "600001", "2026-05-19"),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                mock.patch("repair_market_day_akshare.fetch_stock_em", side_effect=fake_stock_fetch),
                mock.patch("repair_market_day_akshare.fetch_stock_sina", side_effect=fake_stock_fetch),
                mock.patch("repair_market_day_akshare.fetch_stock_tx", side_effect=fake_stock_fetch),
                mock.patch("repair_market_day_akshare.fetch_index_sina", side_effect=fake_index_fetch),
                mock.patch("repair_market_day_akshare.fetch_index_tx", side_effect=fake_index_fetch),
            ):
                result = repair_day(db_path, "2026-05-19", workers=1, attempts_per_source=1)

        self.assertEqual(result["stock_count"], 3)
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(set(fetched_codes), {"600002", "600003"})


if __name__ == "__main__":
    unittest.main()
