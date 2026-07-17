from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._mining_test_helpers import create_sample_market_dbs, trading_days


def _set_market_day(conn, trade_date: str, changes: dict[str, float], null_pre_close: set[str] | None = None) -> None:
    null_pre_close = null_pre_close or set()
    previous_day = conn.execute(
        "SELECT MAX(trade_date) FROM ash.kline_daily WHERE sec_type='stock' AND trade_date < ?",
        (trade_date,),
    ).fetchone()[0]
    for code, pct in changes.items():
        base = 10.0
        if code in null_pre_close:
            conn.execute(
                "UPDATE ash.kline_daily SET close=?, pre_close=? WHERE sec_type='stock' AND sec_code=? AND trade_date=?",
                (base, base, code, previous_day),
            )
        conn.execute(
            "UPDATE ash.kline_daily SET close=?, pre_close=? WHERE sec_type='stock' AND sec_code=? AND trade_date=?",
            (base * (1.0 + pct), None if code in null_pre_close else base, code, trade_date),
        )
    conn.commit()


def _insert_candidate_outcome(
    conn,
    run_id: int,
    strategy_id: str,
    trade_date: str,
    code: str,
    open_t1: float | None,
    close_t2: float | None,
    close_t5: float | None,
    features: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO strategy_runs (
          run_id, strategy_id, version, trade_date, run_at, universe_size,
          n_candidates, status, error_msg
        ) VALUES (?, ?, 'vtest', ?, '2026-07-17 00:00:00', 1, 1, 'ok', NULL)
        """,
        (run_id, strategy_id, trade_date),
    )
    cursor = conn.execute(
        """
        INSERT INTO candidates (
          run_id, strategy_id, version, trade_date, sec_type, sec_code,
          sec_name, entry_price, features_json, rank
        ) VALUES (?, ?, 'vtest', ?, 'stock', ?, ?, 10.0, ?, 1)
        """,
        (run_id, strategy_id, trade_date, code, code, json.dumps(features or {})),
    )
    conn.execute(
        """
        INSERT INTO outcomes (
          candidate_id, open_t1, close_t2, close_t5, backfilled_at, status
        ) VALUES (?, ?, ?, ?, '2026-07-17 00:00:00', 'complete')
        """,
        (cursor.lastrowid, open_t1, close_t2, close_t5),
    )
    conn.commit()


def _insert_watchlist_outcome(
    conn,
    snapshot_date: str,
    code: str,
    state: str,
    open_t1: float,
    close_t2: float,
    close_t5: float,
) -> None:
    conn.execute(
        """
        INSERT INTO watchlist_snapshots (
          snapshot_date, sec_code, sec_name, state, entry_price, created_at
        ) VALUES (?, ?, ?, ?, 10.0, '2026-07-17 00:00:00')
        """,
        (snapshot_date, code, code, state),
    )
    conn.execute(
        """
        INSERT INTO watchlist_outcomes (
          snapshot_date, sec_code, state, open_t1, close_t2, close_t5,
          backfilled_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, '2026-07-17 00:00:00', 'complete')
        """,
        (snapshot_date, code, state, open_t1, close_t2, close_t5),
    )
    conn.commit()


class MiningReportTests(unittest.TestCase):
    def test_market_regime_uses_fallback_close_and_strict_red_green_thresholds(self) -> None:
        from mining.db import connect
        from mining.reports import compute_market_regime

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            calendar = trading_days("2026-02-20", 40)
            red_day = calendar[20]
            green_day = calendar[21]
            conn = connect(base_dir=base)
            try:
                _set_market_day(
                    conn,
                    red_day,
                    {"600001": -0.01, "300001": -0.02, "600003": 0.01},
                    null_pre_close={"600001"},
                )
                _set_market_day(
                    conn,
                    green_day,
                    {"600001": 0.01, "300001": 0.02, "600003": -0.01},
                )
                red = compute_market_regime(conn, red_day)
                green = compute_market_regime(conn, green_day)
            finally:
                conn.close()

        self.assertEqual(red["light"], "RED")
        self.assertEqual(red["n"], 3)
        self.assertAlmostEqual(red["advancers_ratio"], 1 / 3)
        self.assertEqual(green["light"], "GREEN")
        self.assertAlmostEqual(green["advancers_ratio"], 2 / 3)

    def test_forward_summary_groups_candidates_and_watchlist_and_skips_null_open(self) -> None:
        from mining.db import connect
        from mining.reports import get_forward_summary

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            calendar = trading_days("2026-02-20", 40)
            red_day = calendar[20]
            green_day = calendar[21]
            conn = connect(base_dir=base)
            try:
                _set_market_day(conn, red_day, {"600001": -0.01, "300001": -0.02, "600003": 0.01})
                _set_market_day(conn, green_day, {"600001": 0.01, "300001": 0.02, "600003": -0.01})
                _insert_candidate_outcome(conn, 1, "launch_burst", red_day, "600001", 10.0, 11.0, 12.0)
                _insert_candidate_outcome(conn, 2, "launch_burst", red_day, "600003", None, 100.0, 100.0)
                _insert_candidate_outcome(conn, 3, "launch_burst", green_day, "300001", 20.0, 22.0, 18.0)
                _insert_watchlist_outcome(conn, red_day, "600001", "回踩到位", 10.0, 9.0, 8.0)
                _insert_watchlist_outcome(conn, green_day, "300001", "回踩到位", 10.0, 11.0, 12.0)
                summary = get_forward_summary(conn, green_day, lookback=30)
            finally:
                conn.close()

        candidates = summary[(summary["source"] == "candidate") & (summary["group"] == "launch_burst")]
        red_candidate = candidates[candidates["market_regime"] == "RED"].iloc[0]
        green_candidate = candidates[candidates["market_regime"] == "GREEN"].iloc[0]
        watchlist = summary[(summary["source"] == "watchlist") & (summary["group"] == "回踩到位")]

        self.assertEqual(red_candidate["n"], 1)
        self.assertAlmostEqual(red_candidate["avg_open_t5"], 0.2)
        self.assertAlmostEqual(green_candidate["avg_open_t2"], 0.1)
        self.assertAlmostEqual(green_candidate["avg_open_t5"], -0.1)
        self.assertEqual(set(watchlist["market_regime"]), {"RED", "GREEN"})

    def test_markdown_report_contains_market_launch_and_forward_sections(self) -> None:
        from mining.db import connect
        from mining.reports import generate_markdown_report

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                _set_market_day(conn, trade_date, {"600001": 0.01, "300001": 0.02, "600003": -0.01})
                _insert_candidate_outcome(
                    conn,
                    1,
                    "launch_burst",
                    trade_date,
                    "002821",
                    10.0,
                    11.0,
                    12.0,
                    features={
                        "pct": 0.0767,
                        "volume_ratio": 1.58,
                        "sigma_multiple": 3.02,
                        "cluster_days": 14,
                    },
                )
                path = generate_markdown_report(conn, trade_date, base / "output")
            finally:
                conn.close()
            content = Path(path).read_text(encoding="utf-8")

        self.assertIn("市场状态：GREEN", content)
        self.assertIn("## 今日主升启动", content)
        self.assertIn("002821", content)
        self.assertIn("## 前向验证（次日开盘口径 × 市场状态）", content)
        self.assertIn("launch_burst", content)


if __name__ == "__main__":
    unittest.main()
