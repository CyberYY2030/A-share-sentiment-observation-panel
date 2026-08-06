from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mining.data_quality import mark_known_bad_session
from mining.db import connect
from mining.selection_context import build_selection_context, select_from_context
from tests._mining_test_helpers import create_sample_market_dbs, trading_days


class SelectionContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.dates = create_sample_market_dbs(self.base_dir)
        self.conn = connect(self.base_dir)
        self.trade_date = self.dates["target_trade_date"]

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_context_uses_clean_calendar_and_preserves_raw_activity(self) -> None:
        bad_day = trading_days("2026-02-20", 40)[30]
        mark_known_bad_session(self.conn, bad_day, reason="unit_test_bad_session")

        context = build_selection_context(self.conn, self.trade_date)

        self.assertNotIn(bad_day, context.diagnostics["clean_dates"])
        self.assertEqual(context.data_status, "ready")
        self.assertTrue({"adj_open", "adj_high", "adj_low", "adj_close"}.issubset(context.bars.columns))
        self.assertTrue((context.bars["amount"] > 0).all())
        self.assertTrue((context.bars["volume"] > 0).all())
        latest = context.bars.sort_values("trade_date").groupby("sec_code", as_index=False).tail(1)
        self.assertTrue((latest["adj_close"] == latest["close"]).all())

    def test_context_excludes_invalid_adjustment_series_instead_of_fallback(self) -> None:
        self.conn.execute(
            """
            UPDATE ash.kline_daily
            SET pre_close=0.01
            WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
            """,
            (self.trade_date,),
        )
        self.conn.commit()

        context = build_selection_context(self.conn, self.trade_date)

        self.assertNotIn("600001", set(context.universe["sec_code"]))
        self.assertIn("600001", context.diagnostics["adjustment_invalid_codes"])
        self.assertEqual(context.diagnostics["skipped_reason_counts"]["adjustment_ratio_out_of_range"], 1)

    def test_same_context_inputs_are_deterministic_and_selector_is_pure(self) -> None:
        first = build_selection_context(self.conn, self.trade_date)
        second = build_selection_context(self.conn, self.trade_date)

        pd.testing.assert_frame_equal(first.bars, second.bars)
        pd.testing.assert_frame_equal(first.universe, second.universe)
        self.assertEqual(first.diagnostics, second.diagnostics)
        selection = select_from_context(first, lambda context: context.universe["sec_code"].tolist())
        self.assertListEqual(selection, first.universe["sec_code"].tolist())


if __name__ == "__main__":
    unittest.main()
