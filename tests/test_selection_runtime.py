from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mining.db import connect
from mining.event_activity import build_event_activity
from mining.scanners.base_breakout import evaluate_base_breakout
from mining.scanners.counter_trend_rs import evaluate_counter_trend_rs
from mining.scanners.launch_burst import evaluate_compression_launch
from mining.scanners.momentum_breakout import evaluate_momentum_anomaly
from mining.scanners.second_launch import select_candidates_from_pullback_support
from mining.scanners.strong_trend import evaluate_strong_trend
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
from mining.watchlist import build_pullback_support
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
                   k.pre_close, k.change, k.change_pct, k.volume, k.amount, k.turnover_ratio
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

    def _full_snapshot_benchmark_closes(self, context) -> dict[str, float]:
        return {
            code: float(series.loc[self.final_date])
            for code, series in context.benchmark_closes.items()
            if pd.notna(series.loc[self.final_date])
        }

    def _formal_rows(self, context) -> dict[str, pd.DataFrame]:
        pullback = build_pullback_support(self.conn, context.trade_date, context=context)
        pullback_candidates = select_candidates_from_pullback_support(
            pullback,
            trade_date=context.trade_date,
            strategy_id="second_launch",
            version="v2.0",
            top_n=20,
        )
        pullback_rows = pd.DataFrame(
            [
                {
                    "sec_code": candidate.sec_code,
                    "sec_name": candidate.sec_name,
                    "entry_price": candidate.entry_price,
                    "rank": candidate.rank,
                    "features": tuple(sorted((key, repr(value)) for key, value in candidate.features.items())),
                }
                for candidate in pullback_candidates
            ]
        )
        return {
            "A": evaluate_strong_trend(context).rows,
            "B_compression": evaluate_compression_launch(context)[0],
            "B_momentum": evaluate_momentum_anomaly(context)[0],
            "C": pullback_rows,
            "D": evaluate_base_breakout(context)[0],
            "E": evaluate_counter_trend_rs(context)[0],
        }

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
        quarantined = resolve_selection_mode(
            trade_date=self.snapshot_date,
            now=now,
            close_quality_status="usable_with_quarantine",
            snapshot_as_of=None,
            snapshot_coverage=None,
            policy=self.policy,
        )

        self.assertEqual(final.mode, MODE_CLOSE_FINAL)
        self.assertEqual(pending.mode, MODE_CLOSE_PENDING)
        self.assertEqual(quarantined.mode, MODE_CLOSE_FINAL)
        self.assertEqual(quarantined.reason, "usable_with_quarantine_close_final")

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

    def test_same_timestamp_price_change_rebuilds_snapshot_cache_key(self) -> None:
        runtime = SelectionRuntime(self.policy)
        now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        first = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=now - dt.timedelta(minutes=1),
            snapshot_source="unit-test",
        )
        changed = self._snapshot()
        changed.loc[changed.index[0], "close"] = float(changed.loc[changed.index[0], "close"]) + 0.5
        second = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=changed,
            snapshot_as_of=now - dt.timedelta(minutes=1),
            snapshot_source="unit-test",
        )

        self.assertNotEqual(first.cache_key, second.cache_key)
        self.assertIsNot(first.context, second.context)

    def test_snapshot_without_same_batch_index_keeps_price_ready_and_e_unavailable(self) -> None:
        now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        runtime = SelectionRuntime(self.policy)

        result = runtime.run(
            self.conn,
            self.snapshot_date,
            now=now,
            snapshot_bars=self._snapshot(),
            snapshot_as_of=now - dt.timedelta(minutes=1),
            snapshot_source="unit-test",
        )
        rows, diagnostics = evaluate_counter_trend_rs(result.context)

        self.assertEqual(result.context.price_status, "ready")
        self.assertEqual(result.context.benchmark_status, "unavailable")
        self.assertTrue(rows.empty)
        self.assertTrue(diagnostics["skipped_reason_counts"])

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
            current_benchmark_closes=self._full_snapshot_benchmark_closes(close_final),
            allow_missing_activity=True,
        )
        columns = ["sec_code", "trade_date", "adj_open", "adj_high", "adj_low", "adj_close"]

        pd.testing.assert_frame_equal(
            close_final.bars[columns].reset_index(drop=True),
            snapshot_context.bars[columns].reset_index(drop=True),
        )

    def test_full_snapshot_and_final_re_evaluate_identical_a_to_e_results(self) -> None:
        snapshot = self._snapshot()
        close_final = build_selection_context(self.conn, self.final_date, mode=MODE_CLOSE_FINAL)
        snapshot_context = build_selection_context(
            self.conn,
            self.final_date,
            mode=MODE_INTRADAY,
            current_bars=snapshot,
            snapshot_names=snapshot[["sec_code", "sec_name"]],
            clean_dates=close_final.diagnostics["clean_dates"][:-1],
            current_benchmark_closes=self._full_snapshot_benchmark_closes(close_final),
            allow_missing_activity=True,
        )

        self.assertEqual(snapshot_context.price_status, "ready")
        self.assertEqual(snapshot_context.benchmark_status, "ready")
        self.assertEqual(snapshot_context.input_fingerprint, close_final.input_fingerprint)
        final_rows = self._formal_rows(close_final)
        snapshot_rows = self._formal_rows(snapshot_context)

        self.assertGreater(sum(len(rows) for rows in final_rows.values()), 0)
        for capability in final_rows:
            pd.testing.assert_frame_equal(
                final_rows[capability].reset_index(drop=True),
                snapshot_rows[capability].reset_index(drop=True),
            )

    def test_partial_snapshot_is_provisional_and_not_a_final_parity_input(self) -> None:
        full_snapshot = self._snapshot()
        close_final = build_selection_context(self.conn, self.final_date, mode=MODE_CLOSE_FINAL)
        partial_snapshot = full_snapshot.copy()
        partial_snapshot["volume"] = partial_snapshot["volume"] * 0.5
        partial_snapshot["amount"] = partial_snapshot["amount"] * 0.5
        full_context = build_selection_context(
            self.conn,
            self.final_date,
            mode=MODE_INTRADAY,
            current_bars=full_snapshot,
            snapshot_names=full_snapshot[["sec_code", "sec_name"]],
            clean_dates=close_final.diagnostics["clean_dates"][:-1],
            current_benchmark_closes=self._full_snapshot_benchmark_closes(close_final),
            allow_missing_activity=True,
        )
        partial_context = build_selection_context(
            self.conn,
            self.final_date,
            mode=MODE_INTRADAY,
            current_bars=partial_snapshot,
            snapshot_names=partial_snapshot[["sec_code", "sec_name"]],
            clean_dates=close_final.diagnostics["clean_dates"][:-1],
            current_benchmark_closes=self._full_snapshot_benchmark_closes(close_final),
            allow_missing_activity=True,
        )
        full_activity = build_event_activity(full_context).rows.sort_values("sec_code").reset_index(drop=True)
        partial_activity = build_event_activity(partial_context).rows.sort_values("sec_code").reset_index(drop=True)

        self.assertNotEqual(partial_context.input_fingerprint, close_final.input_fingerprint)
        self.assertTrue(partial_activity["activity_provisional"].all())
        self.assertFalse(full_activity["activity_ratio"].equals(partial_activity["activity_ratio"]))

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
