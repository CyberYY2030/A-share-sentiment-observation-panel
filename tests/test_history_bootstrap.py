from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mining.history_bootstrap import run_history_bootstrap_dry_run
from tests._mining_test_helpers import create_sample_market_dbs, trading_days


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        conn.commit()
    finally:
        conn.close()
    return days[-1]


class HistoryBootstrapTests(unittest.TestCase):
    def test_insufficient_history_is_reported_without_a_formal_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            with mock.patch.dict("os.environ", {"SCREENING_BASE_DIR": str(base)}, clear=False):
                result = run_history_bootstrap_dry_run(base, dates["target_trade_date"], sessions=5)

        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["skipped_insufficient_history"], 5)
        self.assertEqual(result["increments"]["selection_batches"]["delta"], 0)
        self.assertEqual(result["increments"]["outcomes"]["delta"], 0)

    def test_shadow_reuses_fingerprints_without_outcome_or_source_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            target_date = _extend_to_p60_history(base)
            source_hash_before = _sha256(base / "mining_mvp.db")
            with mock.patch.dict("os.environ", {"SCREENING_BASE_DIR": str(base)}, clear=False):
                first = run_history_bootstrap_dry_run(base, target_date, sessions=5)
                shadow_exists = Path(first["shadow_mining_db"]).exists()
                second = run_history_bootstrap_dry_run(base, target_date, sessions=5)
            source_hash_after = _sha256(base / "mining_mvp.db")

        self.assertEqual(first["processed"], 5)
        self.assertEqual(first["skipped_insufficient_history"], 0)
        self.assertEqual(first["increments"]["strategy_runs"]["delta"], 30)
        self.assertEqual(first["increments"]["outcomes"]["delta"], 0)
        self.assertEqual(source_hash_before, source_hash_after)
        self.assertTrue(shadow_exists)
        self.assertEqual(second["increments"]["selection_batches"]["delta"], 0)
        self.assertEqual(second["increments"]["strategy_runs"]["delta"], 0)
        self.assertEqual(second["increments"]["candidates"]["delta"], 0)
        self.assertEqual(second["increments"]["outcomes"]["delta"], 0)
        self.assertEqual(second["reused_batches"], 5)


if __name__ == "__main__":
    unittest.main()
