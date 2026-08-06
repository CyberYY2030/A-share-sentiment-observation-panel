from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mining.db import connect
from mining.selection_context import build_selection_context
from mining.selection_runtime import (
    MODE_CLOSE_FINAL,
    MODE_CLOSE_PENDING,
    MODE_DATA_UNAVAILABLE,
    MODE_INTRADAY,
    SelectionRuntime,
    SnapshotPolicy,
    resolve_selection_mode,
)
from tests._mining_test_helpers import create_sample_market_dbs


class SelectionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.dates = create_sample_market_dbs(self.base_dir)
        self.conn = connect(self.base_dir)
        self.final_date = self.dates["target_trade_date"]
        self.snapshot_date = "2026-04-17"
        self.policy = SnapshotPolicy(max_age_minutes=10, min_coverage_ratio=0.8)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _snapshot(self) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT k.sec_code, s.name AS sec_name, k.open, k.high, k.low, k.close,
                   k.pre_close, k.volume, k.amount, k.turnover_ratio
            FROM ash.kline_daily k
            LEFT JOIN ash.stock_info s ON s.sec_code=k.sec_code
            WHERE k.sec_type='stock' AND k.trade_date=?
            ORDER BY k.sec_code
            """,
            self.conn,
            params=[self.final_date],
        )

    def _copy_final_rows_to_snapshot_date(self) -> None:
        self.conn.execute(
            """
            INSERT INTO ash.kline_daily (
              sec_type, sec_code, trade_date, open, high, low, close, pre_close,
              change, change_pct, volume, amount, turnover_ratio, source, updated_at
            )
            SELECT sec_type, sec_code, ?, open, high, low, close, pre_close,
                   change, change_pct, volume, amount, turnover_ratio, source, updated_at
            FROM ash.kline_daily
            WHERE sec_type='stock' AND trade_date=?
            """,
            (self.snapshot_date, self.final_date),
        )
        self.conn.commit()

    def test_mode_resolver_keeps_final_over_snapshot_and_marks_pending_after_close(self) -> None:
        now = dt.datetime(2026, 4, 17, 15, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        final = resolve_selection_mode(
            trade_date=self.snapshot_date,
            now=now,
            close_quality_status="clean",
            snapshot_as_of=now,
            snapshot_coverage=1.0,
            policy=self.policy,
        )
        pending = resolve_selection_mode(
            trade_date=self.snapshot_date,
            now=now,
            close_quality_status="partial_missing",
            snapshot_as_of=now,
            snapshot_coverage=1.0,
            policy=self.policy,
        )

        self.assertEqual(final.mode, MODE_CLOSE_FINAL)
        self.assertEqual(pending.mode, MODE_CLOSE_PENDING)

    def test_intraday_snapshot_is_cached_and_never_writes_official_candidates(self) -> None:
        runtime = SelectionRuntime(self.policy)
        now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        before = self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

        result = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=now - dt.timedelta(minutes=2),
            snapshot_source="unit-test",
        )
        repeated = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=now - dt.timedelta(minutes=2),
            snapshot_source="unit-test",
        )

        self.assertEqual(result.mode, MODE_INTRADAY)
        self.assertFalse(result.official)
        self.assertEqual(result.cache_key, repeated.cache_key)
        self.assertIs(result.context, repeated.context)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], before)

    def test_price_change_rebuilds_snapshot_context_and_missing_activity_is_explicit(self) -> None:
        runtime = SelectionRuntime(self.policy)
        now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        first = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=now - dt.timedelta(minutes=3),
            snapshot_source="unit-test",
        )
        changed = self._snapshot()
        changed.loc[changed.index[0], ["open", "high", "low", "close"]] = [20.0, 20.5, 19.5, 20.2]
        changed = changed.drop(columns=["volume", "amount"])
        second = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=changed,
            snapshot_as_of=now - dt.timedelta(minutes=1),
            snapshot_source="unit-test",
        )

        first_close = float(first.context.bars[first.context.bars["sec_code"] == "300001"].sort_values("trade_date").iloc[-1]["close"])
        second_close = float(second.context.bars[second.context.bars["sec_code"] == "300001"].sort_values("trade_date").iloc[-1]["close"])
        self.assertNotEqual(first_close, second_close)
        self.assertTrue(second.context.diagnostics["activity_unavailable"])

    def test_close_final_clears_snapshot_cache_and_only_final_invokes_finalizer(self) -> None:
        runtime = SelectionRuntime(self.policy)
        now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        snapshot = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=now - dt.timedelta(minutes=1),
            snapshot_source="unit-test",
        )
        self._copy_final_rows_to_snapshot_date()
        finalized: list[str] = []
        final = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            finalizer=lambda context: finalized.append(context.mode),
        )

        self.assertEqual(snapshot.mode, MODE_INTRADAY)
        self.assertEqual(final.mode, MODE_CLOSE_FINAL)
        self.assertTrue(final.official)
        self.assertEqual(finalized, [MODE_CLOSE_FINAL])
        self.assertFalse(runtime._snapshot_cache)

    def test_snapshot_equal_to_final_has_the_same_adjusted_bars(self) -> None:
        snapshot = self._snapshot()
        close_final = build_selection_context(self.conn, self.final_date, mode=MODE_CLOSE_FINAL)
        snapshot_context = build_selection_context(
            self.conn,
            self.final_date,
            mode=MODE_INTRADAY,
            current_bars=snapshot,
            snapshot_names=snapshot[["sec_code", "sec_name"]],
            clean_dates=close_final.diagnostics["clean_dates"][:-1],
            allow_missing_activity=True,
        )
        columns = ["sec_code", "trade_date", "adj_open", "adj_high", "adj_low", "adj_close"]

        pd.testing.assert_frame_equal(
            close_final.bars[columns].reset_index(drop=True),
            snapshot_context.bars[columns].reset_index(drop=True),
        )

    def test_old_snapshot_cannot_revive_on_the_next_trade_day(self) -> None:
        runtime = SelectionRuntime(self.policy)
        now = dt.datetime(2026, 4, 20, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        result = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=dt.datetime(2026, 4, 17, 14, 59, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            snapshot_source="unit-test",
        )

        self.assertEqual(result.mode, MODE_DATA_UNAVAILABLE)
        self.assertFalse(result.official)

    def test_snapshot_below_configured_coverage_is_unavailable(self) -> None:
        runtime = SelectionRuntime(self.policy)
        now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        result = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot().head(1),
            snapshot_as_of=now - dt.timedelta(minutes=1),
            snapshot_source="unit-test",
        )

        self.assertEqual(result.mode, MODE_DATA_UNAVAILABLE)
        self.assertEqual(result.reason, "snapshot_coverage_below_threshold")


if __name__ == "__main__":
    unittest.main()
