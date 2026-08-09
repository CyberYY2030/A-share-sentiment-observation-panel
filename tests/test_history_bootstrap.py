from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mining.history_bootstrap import run_history_bootstrap_dry_run
from tests._mining_test_helpers import create_sample_market_dbs, trading_days


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_etf_source_fixture(base_dir: Path) -> None:
    conn = sqlite3.connect(base_dir / "etf_mvp.db")
    conn.close()


def _extend_to_p60_history(base_dir: Path) -> str:
    """Add deterministic local bars only to a temporary test database."""
    stock_db = base_dir / "a_share_mvp.db"
    conn = sqlite3.connect(stock_db)
    try:
        days = trading_days("2026-04-20", 80)
        for code, base_price in (("600001", 10.0), ("300001", 20.0), ("600003", 30.0)):
            previous = base_price
            for index, trade_date in enumerate(days):
                close = round(previous * 1.004, 2)
                conn.execute(
                    """
                    INSERT INTO kline_daily (
                      sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                      change, change_pct, volume, amount, turnover_ratio, source, updated_at
                    ) VALUES ('stock', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'history-bootstrap-test', ?)
                    """,
                    (
                        code,
                        trade_date,
                        previous,
                        round(max(previous, close) * 1.01, 2),
                        round(min(previous, close) * 0.99, 2),
                        close,
                        previous,
                        round(close - previous, 2),
                        round((close - previous) / previous * 100, 2),
                        10_000_000 + index,
                        1_000_000_000 + index,
                        3.0,
                        "2026-08-08 00:00:00",
                    ),
                )
                previous = close
        for code, base_price in (("000001", 3000.0), ("000300", 4000.0), ("000852", 6000.0), ("399001", 10000.0)):
            previous = base_price
            for index, trade_date in enumerate(days):
                close = round(previous * 1.002, 2)
                conn.execute(
                    """
                    INSERT INTO kline_daily (
                      sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                      change, change_pct, volume, amount, turnover_ratio, source, updated_at
                    ) VALUES ('index', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'history-bootstrap-test', ?)
                    """,
                    (
                        code,
                        trade_date,
                        previous,
                        max(previous, close),
                        min(previous, close),
                        close,
                        previous,
                        round(close - previous, 2),
                        round((close - previous) / previous * 100, 2),
                        100_000_000 + index,
                        100_000_000_000 + index,
                        "2026-08-08 00:00:00",
                    ),
                )
                previous = close

        peak_date = days[-9]
        peak_close = float(
            conn.execute(
                "SELECT close FROM kline_daily WHERE sec_type='stock' AND sec_code='600003' AND trade_date=?",
                (peak_date,),
            ).fetchone()[0]
        )
        previous = peak_close
        for trade_date in days[-8:-3]:
            close = round(peak_close * 0.90, 2)
            conn.execute(
                """
                UPDATE kline_daily
                SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?,
                    volume=?, amount=?
                WHERE sec_type='stock' AND sec_code='600003' AND trade_date=?
                """,
                (
                    round(close * 1.005, 2),
                    round(close * 1.01, 2),
                    round(close * 0.995, 2),
                    close,
                    previous,
                    round(close - previous, 2),
                    round((close - previous) / previous * 100, 2),
                    5_000_000,
                    500_000_000,
                    trade_date,
                ),
            )
            previous = close
        retrigger_date = days[-3]
        retrigger_close = round(peak_close * 0.949, 2)
        conn.execute(
            """
            UPDATE kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?,
                volume=?, amount=?
            WHERE sec_type='stock' AND sec_code='600003' AND trade_date=?
            """,
            (
                round(retrigger_close * 0.98, 2),
                round(retrigger_close * 1.01, 2),
                round(retrigger_close * 0.97, 2),
                retrigger_close,
                previous,
                round(retrigger_close - previous, 2),
                round((retrigger_close - previous) / previous * 100, 2),
                20_000_000,
                2_000_000_000,
                retrigger_date,
            ),
        )
        following_date = days[-2]
        following_close = round(retrigger_close * 1.002, 2)
        conn.execute(
            """
            UPDATE kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?,
                volume=?, amount=?
            WHERE sec_type='stock' AND sec_code='600003' AND trade_date=?
            """,
            (
                retrigger_close,
                round(following_close * 1.01, 2),
                round(retrigger_close * 0.99, 2),
                following_close,
                retrigger_close,
                round(following_close - retrigger_close, 2),
                round((following_close - retrigger_close) / retrigger_close * 100, 2),
                10_000_000,
                1_000_000_000,
                following_date,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return days[-1]


class HistoryBootstrapTests(unittest.TestCase):
    def test_insufficient_history_is_reported_without_a_formal_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            _add_etf_source_fixture(base)
            with mock.patch.dict("os.environ", {"SCREENING_BASE_DIR": str(base)}, clear=False):
                result = run_history_bootstrap_dry_run(
                    base,
                    dates["target_trade_date"],
                    sessions=5,
                    shadow_db_path=base / "insufficient-shadow.db",
                )

        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["skipped_insufficient_history"], 5)
        self.assertEqual(result["increments"]["selection_batches"]["delta"], 0)
        self.assertEqual(result["increments"]["outcomes"]["delta"], 0)

    def test_shadow_reuses_fingerprints_without_outcome_or_source_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            _add_etf_source_fixture(base)
            target_date = _extend_to_p60_history(base)
            source_hash_before = _sha256(base / "mining_mvp.db")
            shadow_db = base / "explicit-history-shadow.db"
            with mock.patch.dict("os.environ", {"SCREENING_BASE_DIR": str(base)}, clear=False):
                first = run_history_bootstrap_dry_run(
                    base,
                    target_date,
                    sessions=8,
                    shadow_db_path=shadow_db,
                )
                shadow_exists = Path(first["shadow_mining_db"]).exists()
                manifest_exists = Path(first["manifest_path"]).exists()
                with mock.patch(
                    "mining.history_bootstrap.evaluate_formal_capabilities",
                    side_effect=AssertionError("matching fingerprint must reuse persisted evidence"),
                ), mock.patch(
                    "mining.history_bootstrap.build_selection_context",
                    side_effect=AssertionError("matching manifest must reuse complete batch context evidence"),
                ):
                    second = run_history_bootstrap_dry_run(
                        base,
                        target_date,
                        sessions=8,
                        shadow_db_path=shadow_db,
                        reuse_shadow=True,
                    )
            source_hash_after = _sha256(base / "mining_mvp.db")

        self.assertEqual(first["processed"], 8)
        self.assertEqual(first["skipped_insufficient_history"], 0)
        self.assertEqual(first["increments"]["strategy_runs"]["delta"], 48)
        self.assertEqual(first["increments"]["outcomes"]["delta"], 0)
        self.assertEqual(source_hash_before, source_hash_after)
        self.assertTrue(shadow_exists)
        self.assertTrue(manifest_exists)
        self.assertEqual(first["source_sha256_before"], first["source_sha256_after"])
        self.assertEqual(first["failed"], 0)
        self.assertEqual(first["evidence_schema_version"], 2)
        self.assertEqual(first["generator"], second["generator"])
        self.assertRegex(first["generator"]["git_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(first["generator"]["source_sha256"], r"^[0-9A-F]{64}$")
        self.assertNotIn("fingerprints", first)
        self.assertEqual(set(first["empty_by_strategy"]), set(first["candidate_counts"]))
        self.assertGreater(first["increments"]["pullback_state_history"]["delta"], 0)
        self.assertTrue(any(day["c_state_counts"] for day in first["daily"]))
        observed_c_states = {
            state
            for day in first["daily"]
            for state in day["c_state_counts"]
        }
        self.assertIn("再启动", observed_c_states, first["daily"])
        self.assertEqual(second["increments"]["selection_batches"]["delta"], 0)
        self.assertEqual(second["increments"]["strategy_runs"]["delta"], 0)
        self.assertEqual(second["increments"]["candidates"]["delta"], 0)
        self.assertEqual(second["increments"]["outcomes"]["delta"], 0)
        self.assertEqual(second["reused_batches"], 8)

    def test_shadow_reuse_requires_an_exact_manifest_and_explicit_flag(self) -> None:
        from mining.capabilities import SCREENING_DEFINITION_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            _add_etf_source_fixture(base)
            shadow_db = base / "manifest-shadow.db"
            with mock.patch.dict("os.environ", {"SCREENING_BASE_DIR": str(base)}, clear=False):
                first = run_history_bootstrap_dry_run(
                    base,
                    dates["target_trade_date"],
                    sessions=5,
                    shadow_db_path=shadow_db,
                )
                with self.assertRaisesRegex(FileExistsError, "explicit --reuse-shadow"):
                    run_history_bootstrap_dry_run(
                        base,
                        dates["target_trade_date"],
                        sessions=5,
                        shadow_db_path=shadow_db,
                    )
                with self.assertRaisesRegex(ValueError, "manifest does not match"):
                    run_history_bootstrap_dry_run(
                        base,
                        dates["target_trade_date"],
                        sessions=4,
                        shadow_db_path=shadow_db,
                        reuse_shadow=True,
                    )

            manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(SCREENING_DEFINITION_VERSION, manifest["definition_version"])
            self.assertEqual(
                {"a_share_mvp.db", "etf_mvp.db", "mining_mvp.db", "ths_concept.db"},
                set(manifest["source_sha256"]),
            )

    def test_persistence_failure_writes_report_before_cli_exits_nonzero(self) -> None:
        import run_daily
        from mining.candidate_persistence import BatchPersistenceResult

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            _add_etf_source_fixture(base)
            target_date = _extend_to_p60_history(base)
            shadow_db = base / "failed-shadow.db"
            report_path = base / "failed-history.json"
            with mock.patch.dict("os.environ", {"SCREENING_BASE_DIR": str(base)}, clear=False):
                with mock.patch(
                    "mining.history_bootstrap.persist_close_final_batch",
                    return_value=BatchPersistenceResult(None, "persistence_error", False, "deliberate failure"),
                ):
                    failed_result = run_history_bootstrap_dry_run(
                        base,
                        target_date,
                        sessions=2,
                        shadow_db_path=shadow_db,
                    )

            self.assertEqual(2, failed_result["failed"])
            self.assertEqual({"persistence_error"}, {day["status"] for day in failed_result["daily"]})
            with mock.patch.object(run_daily, "run_history_bootstrap_dry_run", return_value=failed_result):
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "run_daily.py",
                        "--history-bootstrap-dry-run",
                        "--base-dir",
                        str(base),
                        "--target-date",
                        target_date,
                        "--sessions",
                        "2",
                        "--shadow-db",
                        str(shadow_db),
                        "--out",
                        str(report_path),
                    ],
                ):
                    with self.assertRaises(SystemExit) as exited:
                        run_daily.main()
            self.assertEqual(1, exited.exception.code)
            self.assertEqual(2, json.loads(report_path.read_text(encoding="utf-8"))["failed"])


if __name__ == "__main__":
    unittest.main()
