import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._mining_test_helpers import create_sample_market_dbs


def _insert_run(conn: sqlite3.Connection, run_id: int, strategy_id: str, trade_date: str) -> None:
    conn.execute(
        """
        INSERT INTO strategy_runs (
          run_id, strategy_id, version, trade_date, run_at, universe_size,
          n_candidates, status, error_msg
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, strategy_id, "vtest", trade_date, "2026-04-10 00:00:00", 4, 4, "ok", None),
    )


def _insert_candidate(
    conn: sqlite3.Connection,
    strategy_id: str,
    trade_date: str,
    sec_code: str,
    score: float,
    rank: int,
    version: str = "vtest",
    run_id: int = 1,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO candidates (
          run_id, strategy_id, version, trade_date, sec_type, sec_code,
          sec_name, entry_price, features_json, rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, strategy_id, version, trade_date, "stock", sec_code, sec_code, 10.0, json.dumps({"score": score}), rank),
    )
    return int(cursor.lastrowid)


def _insert_outcome(conn: sqlite3.Connection, candidate_id: int, r4: float | None, r5: float | None, is_win: int | None) -> None:
    conn.execute(
        """
        INSERT INTO outcomes (
          candidate_id, open_t1, high_t1, low_t1, close_t1, close_t2, close_t5,
          r1, r2, r3, r4, r5, is_win, backfilled_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            10.0,
            10.5,
            9.8,
            10.2,
            None if r4 is None else 10.0 * (1.0 + r4),
            None if r5 is None else 10.0 * (1.0 + r5),
            0.0,
            0.05,
            -0.02,
            r4,
            r5,
            is_win,
            "2026-04-10 00:00:00",
            "complete" if r4 is not None and r5 is not None else "partial",
        ),
    )


class MiningEvaluateTests(unittest.TestCase):
    def test_aggregate_results_counts_complete_outcomes_score_buckets_and_topk(self) -> None:
        from mining.db import connect
        from mining.evaluate import aggregate_results

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                trade_date = dates["target_trade_date"]
                _insert_run(conn, 1, "unit_strategy", trade_date)
                high = _insert_candidate(conn, "unit_strategy", trade_date, "600001", 0.9, 1)
                mid = _insert_candidate(conn, "unit_strategy", trade_date, "300001", 0.5, 2)
                low = _insert_candidate(conn, "unit_strategy", trade_date, "600003", 0.1, 3)
                partial = _insert_candidate(conn, "unit_strategy", trade_date, "600004", 0.0, 4)
                _insert_outcome(conn, high, 0.04, 0.09, 1)
                _insert_outcome(conn, mid, 0.02, 0.03, 0)
                _insert_outcome(conn, low, -0.01, -0.02, 1)
                _insert_outcome(conn, partial, None, None, None)
                conn.commit()

                summary = aggregate_results(
                    conn,
                    trade_date,
                    trade_date,
                    ["unit_strategy"],
                    baseline={"baseline_median_r5": -0.01, "baseline_win_rate": 0.5},
                    top_k=1,
                )
            finally:
                conn.close()

        self.assertEqual(len(summary), 1)
        row = summary.iloc[0]
        self.assertEqual(row["n_candidates"], 4)
        self.assertEqual(row["n_evaluated"], 3)
        self.assertAlmostEqual(row["win_rate"], 2 / 3)
        self.assertAlmostEqual(row["avg_r4"], (0.04 + 0.02 - 0.01) / 3)
        self.assertAlmostEqual(row["avg_r5"], (0.09 + 0.03 - 0.02) / 3)
        self.assertAlmostEqual(row["median_r5"], 0.03)
        self.assertAlmostEqual(row["median_r5_vs_baseline"], 0.04)
        self.assertAlmostEqual(row["win_rate_vs_baseline"], (2 / 3) - 0.5)
        self.assertAlmostEqual(row["score_high_avg_r5"], 0.09)
        self.assertAlmostEqual(row["score_mid_avg_r5"], 0.03)
        self.assertAlmostEqual(row["score_low_avg_r5"], -0.02)
        self.assertTrue(row["score_monotonic"])
        self.assertEqual(row["topk_n"], 1)
        self.assertAlmostEqual(row["topk_avg_r5"], 0.09)
        self.assertAlmostEqual(row["topk_median_r5"], 0.09)
        self.assertAlmostEqual(row["topk_discovery_hit"], 1.0)

    def test_aggregate_results_handles_empty_and_incomplete_ranges(self) -> None:
        from mining.db import connect
        from mining.evaluate import aggregate_results

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                empty = aggregate_results(conn, "2026-01-01", "2026-01-02")
            finally:
                conn.close()

        self.assertTrue(empty.empty)
        self.assertIn("median_r5_vs_baseline", empty.columns)
        self.assertIn("topk_avg_r5", empty.columns)
        self.assertIn("topk_discovery_hit", empty.columns)

    def test_vectorized_baseline_matches_daily_sql_baseline(self) -> None:
        from mining.db import connect
        from mining.evaluate import compute_baseline, compute_baseline_daily_sql

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                fast = compute_baseline(conn, dates["target_trade_date"], dates["target_trade_date"])
                slow = compute_baseline_daily_sql(conn, dates["target_trade_date"], dates["target_trade_date"])
            finally:
                conn.close()

        self.assertEqual(fast["baseline_n"], slow["baseline_n"])
        self.assertAlmostEqual(fast["baseline_avg_r5"], slow["baseline_avg_r5"])
        self.assertAlmostEqual(fast["baseline_median_r5"], slow["baseline_median_r5"])
        self.assertAlmostEqual(fast["baseline_win_rate"], slow["baseline_win_rate"])

    def test_run_scanners_for_dates_skips_existing_candidates_by_default(self) -> None:
        from mining.db import connect
        from mining.evaluate import run_scanners_for_dates
        from mining.scanners.true_leader import TrueLeaderScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                scanner = TrueLeaderScanner()
                _insert_run(conn, 1, scanner.strategy_id, trade_date)
                _insert_candidate(
                    conn,
                    scanner.strategy_id,
                    trade_date,
                    "600001",
                    1.0,
                    1,
                    version=scanner.version,
                    run_id=1,
                )
                conn.commit()
                result = run_scanners_for_dates(
                    conn,
                    [trade_date],
                    strategy_ids=[scanner.strategy_id],
                    skip_existing=True,
                    progress_every=0,
                )
            finally:
                conn.close()

        self.assertEqual(result["runs"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["candidates_saved"], 0)

    def test_resolve_eval_dates_defaults_to_dates_with_t5_available(self) -> None:
        from mining.db import connect, list_stock_trade_dates
        from mining.evaluate import resolve_eval_dates

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                dates = resolve_eval_dates(conn)
                calendar = list_stock_trade_dates(conn)
            finally:
                conn.close()

        self.assertGreater(len(dates), 0)
        self.assertGreaterEqual(calendar.index(dates[0]), 20)
        self.assertLessEqual(calendar.index(dates[-1]) + 5, len(calendar) - 1)

    def test_aggregate_results_uses_current_registered_scanner_version(self) -> None:
        from mining.db import connect
        from mining.evaluate import aggregate_results
        from mining.scanners.trend_embryo import TrendEmbryoScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                trade_date = dates["target_trade_date"]
                _insert_run(conn, 1, "trend_embryo", trade_date)
                current_version = TrendEmbryoScanner.version
                old_id = _insert_candidate(conn, "trend_embryo", trade_date, "600001", 9.0, 1, version="v1.0")
                current_id = _insert_candidate(conn, "trend_embryo", trade_date, "600003", 1.0, 1, version=current_version)
                _insert_outcome(conn, old_id, 0.01, 0.50, 1)
                _insert_outcome(conn, current_id, 0.01, -0.10, 0)
                conn.commit()

                summary = aggregate_results(conn, trade_date, trade_date, ["trend_embryo"])
            finally:
                conn.close()

        self.assertEqual(summary.iloc[0]["n_candidates"], 1)
        self.assertEqual(summary.iloc[0]["n_evaluated"], 1)
        self.assertAlmostEqual(summary.iloc[0]["avg_r5"], -0.10)
    def test_evaluate_range_without_rerun_writes_report_with_fair_columns(self) -> None:
        from mining.db import connect
        from mining.evaluate import evaluate_range

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                trade_date = dates["target_trade_date"]
                _insert_run(conn, 1, "unit_strategy", trade_date)
                candidate_id = _insert_candidate(conn, "unit_strategy", trade_date, "600001", 0.8, 1)
                _insert_outcome(conn, candidate_id, 0.01, 0.04, 1)
                conn.commit()
            finally:
                conn.close()

            result = evaluate_range(
                base_dir=base,
                start=trade_date,
                end=trade_date,
                strategies=["unit_strategy"],
                out_dir=base / "output",
                rerun=False,
                top_k=1,
            )

            self.assertTrue(Path(result["report"]).exists())
            self.assertEqual(result["summary"].iloc[0]["strategy_id"], "unit_strategy")
            self.assertIn("median_r5_vs_baseline", result["summary"].columns)
            self.assertIn("topk_avg_r5", result["summary"].columns)

    def test_compute_forward_window_metrics_calculates_discovery_values_and_missing_window(self) -> None:
        import pandas as pd

        from mining.db import connect
        from mining.evaluate import compute_forward_window_metrics

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                trade_date = dates["target_trade_date"]
                calendar = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT trade_date FROM ash.kline_daily WHERE sec_type='stock' ORDER BY trade_date"
                    )
                ]
                idx = calendar.index(trade_date)
                updates = [
                    (trade_date, 10.0, 10.0, 10.0),
                    (calendar[idx + 1], 10.2, 10.7, 9.8),
                    (calendar[idx + 2], 10.8, 11.0, 9.4),
                    (calendar[idx + 3], 10.4, 10.5, 9.6),
                    (calendar[idx + 4], 10.3, 10.4, 9.9),
                    (calendar[idx + 5], 10.5, 10.6, 9.7),
                ]
                for day, close, high, low in updates:
                    conn.execute(
                        """
                        UPDATE ash.kline_daily
                        SET close=?, high=?, low=?
                        WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
                        """,
                        (close, high, low, day),
                    )
                conn.commit()

                pairs = pd.DataFrame(
                    [
                        {"trade_date": trade_date, "sec_code": "600001"},
                        {"trade_date": trade_date, "sec_code": "600002"},
                    ]
                )
                metrics = compute_forward_window_metrics(conn, pairs, hit_threshold=0.05)
            finally:
                conn.close()

        self.assertAlmostEqual(metrics.iloc[0]["mfe"], 0.10)
        self.assertAlmostEqual(metrics.iloc[0]["mae"], -0.06)
        self.assertAlmostEqual(metrics.iloc[0]["best_close"], 0.08)
        self.assertAlmostEqual(metrics.iloc[0]["forward_r5"], 0.05)
        self.assertAlmostEqual(metrics.iloc[0]["forward_is_win"], 1.0)
        self.assertAlmostEqual(metrics.iloc[0]["discovery_hit"], 1.0)
        self.assertTrue(math.isnan(metrics.iloc[1]["mfe"]))
        self.assertTrue(math.isnan(metrics.iloc[1]["discovery_hit"]))

    def test_compute_baseline_universe_uses_candidate_pool_not_full_market(self) -> None:
        from mining.db import connect
        from mining.evaluate import compute_baseline, compute_baseline_universe

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                trade_date = dates["target_trade_date"]
                calendar = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT trade_date FROM ash.kline_daily WHERE sec_type='stock' ORDER BY trade_date"
                    )
                ]
                idx = calendar.index(trade_date)
                for offset in range(1, 6):
                    day = calendar[idx + offset]
                    close = 6.0 + offset * 2.0
                    conn.execute(
                        """
                        INSERT INTO ash.kline_daily (
                          sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                          change, change_pct, volume, amount, turnover_ratio, source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "stock",
                            "830001",
                            day,
                            close,
                            close * 1.01,
                            close * 0.99,
                            close,
                            close - 1.0,
                            1.0,
                            10.0,
                            100000,
                            12000000,
                            1.0,
                            "unit-test",
                            "2026-04-10 00:00:00",
                        ),
                    )
                conn.commit()

                market = compute_baseline(conn, trade_date, trade_date)
                universe = compute_baseline_universe(conn, trade_date, trade_date, hit_threshold=0.05)
            finally:
                conn.close()

        self.assertEqual(universe["baseline_n"], 3)
        self.assertEqual(market["baseline_n"], 4)
        self.assertGreater(market["baseline_avg_r5"], universe["baseline_avg_r5"])
        self.assertIn("baseline_discovery_hit", universe)

    def test_edge_verdicts_apply_fixed_thresholds(self) -> None:
        import pandas as pd

        from mining.evaluate import edge_verdicts

        summary = pd.DataFrame(
            [
                {
                    "strategy_id": "true_leader",
                    "topk_median_r5_vs_baseline": -0.01,
                    "topk_discovery_hit_vs_baseline": 0.04,
                    "topk_win_rate_vs_baseline": 0.01,
                },
                {
                    "strategy_id": "trend_embryo",
                    "topk_median_r5_vs_baseline": -0.01,
                    "topk_discovery_hit_vs_baseline": 0.02,
                    "topk_win_rate_vs_baseline": 0.01,
                },
            ]
        )

        verdicts = edge_verdicts(summary).set_index("strategy_id")

        self.assertEqual(verdicts.loc["true_leader", "edge_verdict"], "discovery_only")
        self.assertEqual(verdicts.loc["trend_embryo", "edge_verdict"], "no_edge")


    def test_evaluate_watchlist_snapshots_groups_state_and_triage_buckets(self) -> None:
        from mining.db import connect
        from mining.evaluate import evaluate_watchlist_snapshots

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                trade_date = dates["target_trade_date"]
                snapshots = [
                    (trade_date, "600001", "Alpha", "ready", 9.0, 15.6),
                    (trade_date, "300001", "Beta", "ready", 1.0, 25.3),
                ]
                conn.executemany(
                    """
                    INSERT INTO watchlist_snapshots (
                      snapshot_date, sec_code, sec_name, state, triage, entry_price,
                      created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '2026-04-10 00:00:00')
                    """,
                    snapshots,
                )
                outcomes = [
                    (trade_date, "600001", "ready", 0.01, 0.05, -0.01, 0.02, 0.06, 1, "complete"),
                    (trade_date, "300001", "ready", -0.01, 0.01, -0.04, -0.02, -0.03, 0, "complete"),
                ]
                conn.executemany(
                    """
                    INSERT INTO watchlist_outcomes (
                      snapshot_date, sec_code, state, r1, r2, r3, r4, r5,
                      is_win, status, backfilled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-04-10 00:00:00')
                    """,
                    outcomes,
                )
                conn.commit()
                summary = evaluate_watchlist_snapshots(conn, start=trade_date, end=trade_date, min_sample=3)
            finally:
                conn.close()

        all_row = summary[(summary["state"] == "ready") & (summary["triage_bucket"] == "all")].iloc[0]
        top_row = summary[(summary["state"] == "ready") & (summary["triage_bucket"] == "top_q1")].iloc[0]
        bottom_row = summary[(summary["state"] == "ready") & (summary["triage_bucket"] == "bottom_q4")].iloc[0]

        self.assertEqual(all_row["n_snapshots"], 2)
        self.assertEqual(all_row["n_evaluated"], 2)
        self.assertEqual(all_row["sample_status"], "insufficient_sample")
        self.assertAlmostEqual(all_row["win_rate"], 0.5)
        self.assertAlmostEqual(all_row["median_r5"], 0.015)
        self.assertEqual(top_row["n_snapshots"], 1)
        self.assertAlmostEqual(top_row["median_r5"], 0.06)
        self.assertEqual(bottom_row["n_snapshots"], 1)
        self.assertAlmostEqual(bottom_row["median_r5"], -0.03)
        self.assertIn("discovery_hit_vs_baseline", summary.columns)

    def test_evaluate_watchlist_snapshots_handles_empty_table(self) -> None:
        from mining.db import connect
        from mining.evaluate import evaluate_watchlist_snapshots

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                summary = evaluate_watchlist_snapshots(conn)
            finally:
                conn.close()

        self.assertTrue(summary.empty)
        self.assertIn("sample_status", summary.columns)

if __name__ == "__main__":
    unittest.main()