from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mining.db import connect
from mining.universe import build_selection_universe
from tests._mining_test_helpers import create_sample_market_dbs


class SelectionUniverseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.dates = create_sample_market_dbs(self.base_dir)
        self.trade_date = self.dates["target_trade_date"]
        self.stock_db = self.base_dir / "a_share_mvp.db"
        self._seed_universe_cases()
        self.conn = connect(self.base_dir)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _seed_universe_cases(self) -> None:
        source_code = "600001"
        rows = [
            ("301001", "Growth Board", "normal"),
            ("688001", "Star Board", "normal"),
            ("920001", "Beijing Exchange", "normal"),
            ("830001", "Beijing Exchange Two", "normal"),
            ("430001", "Beijing Exchange Three", "normal"),
            ("200001", "Shenzhen B", "normal"),
            ("600002", "*ST Excluded", "normal"),
            ("600004", "Normal Halt", "halt"),
            ("600005", "One Price With Trades", "one_price"),
        ]
        con = sqlite3.connect(self.stock_db)
        try:
            con.execute("UPDATE stock_info SET updated_at='2026-01-01 00:00:00'")
            for code, name, kind in rows:
                con.execute(
                    "INSERT OR REPLACE INTO stock_info VALUES (?, ?, ?, ?, ?)",
                    (code, f"sz.{code}", name, "正常交易", "2026-01-01 00:00:00"),
                )
                con.execute(
                    """
                    INSERT OR REPLACE INTO kline_daily (
                      sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                      change, change_pct, volume, amount, turnover_ratio, source, updated_at
                    )
                    SELECT sec_type, ?, trade_date, open, high, low, close, pre_close,
                           change, change_pct, volume, amount, turnover_ratio, source, updated_at
                    FROM kline_daily
                    WHERE sec_type='stock' AND sec_code=? AND trade_date=?
                    """,
                    (code, source_code, self.trade_date),
                )
                if kind == "halt":
                    con.execute(
                        """
                        UPDATE kline_daily
                        SET open=10, high=10, low=10, close=10, pre_close=10,
                            change=0, change_pct=0, volume=0, amount=0
                        WHERE sec_type='stock' AND sec_code=? AND trade_date=?
                        """,
                        (code, self.trade_date),
                    )
                elif kind == "one_price":
                    con.execute(
                        """
                        UPDATE kline_daily
                        SET open=10, high=10, low=10, close=10, pre_close=9,
                            change=1, change_pct=11.11, volume=1000000, amount=10000000
                        WHERE sec_type='stock' AND sec_code=? AND trade_date=?
                        """,
                        (code, self.trade_date),
                    )
            con.commit()
        finally:
            con.close()

    def test_white_list_preserves_growth_and_star_while_excluding_b_and_beijing(self) -> None:
        result = build_selection_universe(self.conn, self.trade_date)
        codes = set(result.rows["sec_code"])

        self.assertTrue({"300001", "301001", "688001", "600005"}.issubset(codes))
        self.assertFalse({"920001", "830001", "430001", "200001"} & codes)
        self.assertNotIn("600002", codes)
        self.assertNotIn("600004", codes)
        self.assertEqual(result.excluded_reason_counts["non_a_share_prefix"], 4)
        self.assertEqual(result.excluded_reason_counts["st_name"], 1)
        self.assertEqual(result.excluded_reason_counts["no_valid_activity"], 1)
        self.assertEqual(result.universe_count, len(result.rows))
        self.assertIn("clean_bar_count", result.rows.columns)
        self.assertIn("liquidity_pct", result.rows.columns)
        alpha = result.rows.set_index("sec_code").loc["600001"]
        self.assertEqual(int(alpha["clean_bar_count"]), 30)
        self.assertGreater(float(alpha["amount_mean_20d"]), 0.0)
        self.assertEqual(len(result.diagnostics["liquidity_history_dates"]), 20)

    def test_stale_metadata_is_provisional_without_blocking_price_pool(self) -> None:
        result = build_selection_universe(self.conn, self.trade_date)

        self.assertEqual(result.metadata_as_of, "2026-01-01")
        self.assertEqual(result.data_status, "ready")
        self.assertEqual(result.price_status, "ready")
        self.assertFalse(result.metadata_fresh)
        self.assertTrue(result.metadata_provisional)
        self.assertEqual(
            result.diagnostics["metadata_note"],
            "metadata_provisional_last_known_name_filter",
        )
        self.assertEqual(result.diagnostics["latest_clean_trade_date"], self.trade_date)

    def test_snapshot_name_overrides_stale_metadata_for_current_st_status(self) -> None:
        result = build_selection_universe(
            self.conn,
            self.trade_date,
            snapshot_names={"600001": "*ST Current Snapshot"},
        )

        self.assertEqual(result.data_status, "ready")
        self.assertNotIn("600001", set(result.rows["sec_code"]))
        self.assertEqual(result.excluded_reason_counts["st_name"], 2)

    def test_fresh_metadata_is_publishable_without_snapshot_names(self) -> None:
        self.conn.execute("UPDATE ash.stock_info SET updated_at=?", (f"{self.trade_date} 15:30:00",))
        self.conn.commit()

        result = build_selection_universe(self.conn, self.trade_date)

        self.assertEqual(result.metadata_as_of, self.trade_date)
        self.assertEqual(result.data_status, "ready")


if __name__ == "__main__":
    unittest.main()
