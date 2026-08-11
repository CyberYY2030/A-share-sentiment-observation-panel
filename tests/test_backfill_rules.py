import unittest
import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock


class BackfillRuleTests(unittest.TestCase):
    def test_close_ready_target_excludes_pre_cutoff_current_day_and_asof_cannot_bypass(self) -> None:
        from offline_daily_update import resolve_target_close_date

        before_close = dt.datetime(2026, 8, 11, 17, 29, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        after_close = dt.datetime(2026, 8, 11, 17, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        self.assertEqual(resolve_target_close_date(now_cn=before_close), "2026-08-10")
        self.assertEqual(resolve_target_close_date(now_cn=after_close), "2026-08-11")
        self.assertEqual(resolve_target_close_date("2026-08-12", now_cn=before_close), "2026-08-10")
        self.assertEqual(resolve_target_close_date("2026-08-07", now_cn=after_close), "2026-08-07")

    def test_one_invocation_budget_uses_remaining_time_and_preserves_reserve(self) -> None:
        from offline_daily_update import _run_with_remaining_budget

        calls: list[int] = []

        def runner(_cmd, *, cwd, timeout_sec):
            del cwd
            calls.append(timeout_sec)
            return 0, "ok"

        with mock.patch("offline_daily_update.time.monotonic", return_value=100.0):
            rc, output, allowance = _run_with_remaining_budget(
                ["worker"],
                cwd=".",
                deadline=1_000.0,
                reserve_seconds=120,
                runner=runner,
            )
        self.assertEqual((rc, output, allowance), (0, "ok", 780))
        self.assertEqual(calls, [780])

    def test_concept_and_etf_coverage_use_explicit_eligible_universes(self) -> None:
        import json
        import sqlite3

        from offline_daily_update import concept_coverage_for_date, etf_coverage_for_date

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept = sqlite3.connect(root / "ths_concept.db")
            try:
                concept.execute("CREATE TABLE concept_kline (trade_date TEXT, concept_code TEXT)")
                concept.execute(
                    "CREATE TABLE concept_coverage_expectations (trade_date TEXT PRIMARY KEY, eligible_codes_json TEXT, expect_source TEXT)"
                )
                concept.execute("INSERT INTO concept_kline VALUES ('2026-04-22', '880001')")
                concept.execute(
                    "INSERT INTO concept_coverage_expectations VALUES ('2026-04-22', ?, 'provider_eligible_universe')",
                    (json.dumps(["880001", "880002"]),),
                )
                concept.commit()
            finally:
                concept.close()

            etf = sqlite3.connect(root / "etf_mvp.db")
            try:
                etf.execute("CREATE TABLE etf_master (fund_code TEXT, is_equity INTEGER)")
                etf.execute("CREATE TABLE etf_scale (trade_date TEXT, fund_code TEXT)")
                etf.execute("CREATE TABLE etf_total (trade_date TEXT)")
                etf.executemany("INSERT INTO etf_master VALUES (?, 1)", [("510300",), ("510500",)])
                etf.execute("INSERT INTO etf_scale VALUES ('2026-04-22', '510300')")
                etf.execute("INSERT INTO etf_total VALUES ('2026-04-22')")
                etf.commit()
            finally:
                etf.close()

            concept_coverage = concept_coverage_for_date(root / "ths_concept.db", "2026-04-22")
            etf_coverage = etf_coverage_for_date(root / "etf_mvp.db", "2026-04-22")

        self.assertFalse(concept_coverage["concept"])
        self.assertEqual((concept_coverage["concept_have"], concept_coverage["concept_expect"], concept_coverage["concept_missing"]), (1, 2, 1))
        self.assertFalse(etf_coverage["etf"])
        self.assertEqual((etf_coverage["etf_have"], etf_coverage["etf_expect"], etf_coverage["etf_missing"]), (1, 2, 1))

    def test_skip_mining_excludes_mining_from_the_ops1_market_recovery_plan(self) -> None:
        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a_share_mvp.db").touch()
            (base / "mining_mvp.db").touch()
            with mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=[]):
                result = run_offline_update(base, asof="2026-04-30", include_mining=False, dry_run=True)

        self.assertNotIn("mining", result["plan"]["domains"])
        self.assertTrue(result["mining_deferred"])

    def test_no_health_suppresses_health_file_and_push_for_ops1_recovery(self) -> None:
        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=[]),
                mock.patch("offline_daily_update.write_health_summary") as write_health,
                mock.patch("offline_daily_update._send_daily_push") as send_push,
            ):
                result = run_offline_update(
                    base,
                    asof="2026-04-30",
                    include_mining=False,
                    publish_health=False,
                    dry_run=True,
                )

        write_health.assert_not_called()
        send_push.assert_not_called()
        self.assertEqual(result["health"], {"status": "not_published"})
        self.assertEqual(result["push"], {"status": "disabled"})

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
                for day in (
                    "2026-04-16",
                    "2026-04-17",
                    "2026-04-18",
                    "2026-04-19",
                    "2026-04-20",
                    "2026-04-21",
                    "2026-04-23",
                ):
                    conn.execute(
                        """
                        INSERT INTO kline_daily VALUES
                        (?, 'stock', '600001', 10.0, 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0)
                        """,
                        (day,),
                    )
                    for code in ("000001", "399001", "000300", "000852"):
                        conn.execute(
                            "INSERT INTO kline_daily (trade_date, sec_type, sec_code, close) VALUES (?, 'index', ?, 10.0)",
                            (day, code),
                        )
                conn.commit()
            finally:
                conn.close()

            plan = build_missing_update_plan(
                base,
                ["2026-04-21", "2026-04-22", "2026-04-23"],
                stock_min_rows=1,
            )

        self.assertIn("2026-04-22", plan["missing_by_day"])
        self.assertIn("stock", plan["missing_by_day"]["2026-04-22"])
        self.assertIn("index", plan["missing_by_day"]["2026-04-22"])
        self.assertIn("2026-04-22", plan["missing_by_domain"]["stock"])
        self.assertNotIn("2026-04-23", plan["missing_by_domain"]["stock"])

    def test_stock_coverage_accepts_only_usable_session_quality_states(self) -> None:
        import sqlite3

        from mining.data_quality import (
            STATUS_CLEAN,
            STATUS_KNOWN_BAD,
            STATUS_PARTIAL,
            STATUS_UNAVAILABLE,
            STATUS_USABLE_WITH_QUARANTINE,
        )
        from offline_daily_update import stock_coverage_for_date

        with tempfile.TemporaryDirectory() as tmp:
            stock_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(stock_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                        trade_date TEXT,
                        sec_type TEXT,
                        sec_code TEXT,
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
                for code in ("600001", "600002", "600003"):
                    conn.execute(
                        """
                        INSERT INTO kline_daily VALUES
                        ('2026-04-22', 'stock', ?, 10.0, 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0)
                        """,
                        (code,),
                    )
                conn.commit()
            finally:
                conn.close()

            expected = {
                STATUS_CLEAN: True,
                STATUS_USABLE_WITH_QUARANTINE: True,
                STATUS_PARTIAL: False,
                STATUS_KNOWN_BAD: False,
                STATUS_UNAVAILABLE: False,
            }
            for status, usable in expected.items():
                with mock.patch("offline_daily_update.inspect_stock_session", return_value={"status": status}):
                    coverage = stock_coverage_for_date(
                        stock_path,
                        "2026-04-22",
                        stock_min_rows=3,
                        required_index_codes=(),
                    )
                self.assertEqual(coverage["stock"], usable, status)

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


    def test_health_summary_alerts_on_stale_domain_and_zero_complete_streak(self) -> None:
        import json
        import sqlite3

        from offline_daily_update import write_health_summary

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stock = sqlite3.connect(base / "a_share_mvp.db")
            try:
                stock.execute("CREATE TABLE kline_daily (sec_type TEXT, sec_code TEXT, trade_date TEXT, close REAL)")
                for day in ("2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24"):
                    stock.execute("INSERT INTO kline_daily VALUES ('stock', '600001', ?, 10.0)", (day,))
                    stock.execute("INSERT INTO kline_daily VALUES ('index', '000001', ?, 3000.0)", (day,))
                stock.commit()
            finally:
                stock.close()

            concept = sqlite3.connect(base / "ths_concept.db")
            try:
                concept.execute("CREATE TABLE concept_kline (trade_date TEXT, concept_code TEXT)")
                concept.execute("INSERT INTO concept_kline VALUES ('2026-04-20', '880001')")
                concept.commit()
            finally:
                concept.close()

            etf = sqlite3.connect(base / "etf_mvp.db")
            try:
                etf.execute("CREATE TABLE etf_scale (trade_date TEXT)")
                etf.execute("CREATE TABLE etf_total (trade_date TEXT)")
                etf.execute("INSERT INTO etf_scale VALUES ('2026-04-24')")
                etf.execute("INSERT INTO etf_total VALUES ('2026-04-24')")
                etf.commit()
            finally:
                etf.close()

            mining = sqlite3.connect(base / "mining_mvp.db")
            try:
                mining.execute("CREATE TABLE candidates (trade_date TEXT)")
                mining.execute("CREATE TABLE watchlist_snapshots (snapshot_date TEXT)")
                mining.execute("CREATE TABLE outcomes (status TEXT)")
                mining.execute("CREATE TABLE watchlist_outcomes (status TEXT)")
                mining.executemany("INSERT INTO candidates VALUES (?)", [("2026-04-24",), ("2026-04-24",)])
                mining.execute("INSERT INTO watchlist_snapshots VALUES ('2026-04-24')")
                mining.execute("INSERT INTO outcomes VALUES ('complete')")
                mining.commit()
            finally:
                mining.close()

            output = base / "output"
            output.mkdir()
            (output / "health_state.json").write_text(
                json.dumps(
                    {
                        "complete_counts": {"outcomes": 0, "watchlist_outcomes": 0},
                        "zero_complete_delta_streaks": {"outcomes": 0, "watchlist_outcomes": 2},
                    }
                ),
                encoding="utf-8",
            )

            result = write_health_summary(
                base,
                {
                    "ok": False,
                    "plan": {"expected_dates": ["2026-04-24"]},
                    "commands": [{"domain": "etf", "returncode": 999, "output": "source down"}],
                    "logs": [],
                },
            )
            body = Path(result["path"]).read_text(encoding="utf-8")

        self.assertEqual(result["today_rows"]["candidates"], 2)
        self.assertEqual(result["today_rows"]["watchlist_snapshots"], 1)
        self.assertIn("[ALERT] concept lag_vs_stock_days=4", body)
        self.assertIn("[ALERT] watchlist_outcomes complete_delta_zero_streak=3", body)
        self.assertIn("[ALERT] domain_errors=1", body)

    def test_health_summary_handles_missing_tables_as_na(self) -> None:
        from offline_daily_update import write_health_summary

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = write_health_summary(base, {"ok": True, "plan": {"expected_dates": ["2026-04-24"]}})
            body = Path(result["path"]).read_text(encoding="utf-8")

        self.assertIn("| stock | n/a | n/a |", body)
        self.assertIn("- candidates: n/a", body)
        self.assertIn("- outcomes: n/a; complete_delta=0", body)



    def test_repair_market_prefix_maps_stock_and_index_symbols_separately(self) -> None:
        from repair_market_day_akshare import index_market_prefix, market_prefix

        self.assertEqual(market_prefix("000001"), "sz")
        self.assertEqual(market_prefix("002001"), "sz")
        self.assertEqual(market_prefix("300001"), "sz")
        self.assertEqual(market_prefix("301001"), "sz")
        self.assertEqual(market_prefix("600001"), "sh")
        self.assertEqual(market_prefix("688001"), "sh")
        self.assertEqual(market_prefix("830001"), "bj")
        self.assertEqual(index_market_prefix("000001"), "sh")
        self.assertEqual(index_market_prefix("000852"), "sh")
        self.assertEqual(index_market_prefix("399001"), "sz")

    def test_repair_market_continuity_guard_rejects_index_price_in_stock_rows(self) -> None:
        from repair_market_day_akshare import _passes_continuity_guard

        self.assertFalse(
            _passes_continuity_guard(
                {"sec_type": "stock", "sec_code": "000001", "close": 4000.0},
                previous_close=10.0,
                max_jump=0.30,
            )
        )
        self.assertTrue(
            _passes_continuity_guard(
                {"sec_type": "stock", "sec_code": "000001", "close": 10.8},
                previous_close=10.0,
                max_jump=0.30,
            )
        )
        self.assertTrue(
            _passes_continuity_guard(
                {"sec_type": "index", "sec_code": "000001", "close": 4000.0},
                previous_close=10.0,
                max_jump=0.30,
            )
        )

    def test_etf_safe_call_allows_akshare_read_excel_bytes_payload(self) -> None:
        from io import BytesIO

        import pandas as pd

        from backfill_etf_equity_60d_v2 import _safe_call

        payload = BytesIO()
        pd.DataFrame({"基金代码": ["510300"], "基金份额": [1.0]}).to_excel(payload, index=False)
        raw = payload.getvalue()

        def fake_akshare_endpoint():
            return pd.read_excel(raw, engine="openpyxl")

        result = _safe_call(fake_akshare_endpoint)

        self.assertEqual(result.iloc[0]["基金代码"], 510300)
        self.assertEqual(float(result.iloc[0]["基金份额"]), 1.0)



if __name__ == "__main__":
    unittest.main()
