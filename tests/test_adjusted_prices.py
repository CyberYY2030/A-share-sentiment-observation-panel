from __future__ import annotations

import unittest

import pandas as pd

from mining.adjusted_prices import build_forward_adjusted_bars


class AdjustedPricesTests(unittest.TestCase):
    def _split_bars(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "sec_code": "600001",
                    "trade_date": "2026-01-02",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.0,
                    "pre_close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                    "turnover_ratio": 2.0,
                },
                {
                    "sec_code": "600001",
                    "trade_date": "2026-01-03",
                    "open": 5.0,
                    "high": 5.2,
                    "low": 4.9,
                    "close": 5.1,
                    "pre_close": 5.0,
                    "volume": 120.0,
                    "amount": 600.0,
                    "turnover_ratio": 2.5,
                },
            ]
        )

    def test_half_price_corporate_action_does_not_create_false_minus_fifty_percent_return(self) -> None:
        result = build_forward_adjusted_bars(self._split_bars())
        bars = result.bars.sort_values("trade_date").reset_index(drop=True)

        self.assertTrue(bars["adjustment_valid"].all())
        self.assertAlmostEqual(float(bars.loc[0, "adjustment_factor"]), 0.5)
        self.assertAlmostEqual(float(bars.loc[0, "adj_close"]), 5.0)
        self.assertAlmostEqual(float(bars.loc[1, "adj_close"]), 5.1)
        self.assertAlmostEqual(float(bars.loc[1, "adj_close"] / bars.loc[0, "adj_close"] - 1.0), 0.02)
        self.assertAlmostEqual(float(bars.loc[1, "adj_close"]), float(bars.loc[1, "close"]))

    def test_volume_amount_and_turnover_remain_raw_facts(self) -> None:
        source = self._split_bars()
        result = build_forward_adjusted_bars(source)

        for column in ("volume", "amount", "turnover_ratio"):
            self.assertListEqual(result.bars[column].tolist(), source[column].tolist())

    def test_invalid_adjustment_edge_isolates_one_row_and_starts_a_new_segment(self) -> None:
        source = self._split_bars()
        source.loc[1, "pre_close"] = 0.01
        source.loc[2] = {
            **source.loc[1].to_dict(),
            "trade_date": "2026-01-04",
            "open": 5.2,
            "high": 5.4,
            "low": 5.1,
            "close": 5.3,
            "pre_close": 5.1,
        }

        result = build_forward_adjusted_bars(source)

        self.assertEqual(result.invalid_code_reasons["600001"], ["adjustment_ratio_out_of_range"])
        self.assertEqual(result.skipped_reason_counts["adjustment_ratio_out_of_range"], 1)
        bars = result.bars.sort_values("trade_date").reset_index(drop=True)
        self.assertListEqual(bars["adjustment_valid"].tolist(), [True, False, True])
        self.assertTrue(pd.isna(bars.loc[1, "adj_close"]))
        self.assertEqual(bars.loc[2, "latest_valid_segment_start"], "2026-01-04")

    def test_tick_sized_pre_close_difference_does_not_create_a_fake_adjustment(self) -> None:
        source = self._split_bars()
        source.loc[1, "pre_close"] = 9.995

        result = build_forward_adjusted_bars(source)

        self.assertAlmostEqual(float(result.bars.sort_values("trade_date").iloc[0]["adjustment_factor"]), 1.0)

    def test_missing_pre_close_uses_previous_valid_close_and_records_the_fallback(self) -> None:
        source = self._split_bars()
        source.loc[1, "pre_close"] = float("nan")

        result = build_forward_adjusted_bars(source)
        latest = result.bars.sort_values("trade_date").iloc[-1]

        self.assertTrue(result.bars["adjustment_valid"].all())
        self.assertEqual(result.fallback_counts["missing_pre_close_used_previous_valid_close"], 1)
        self.assertEqual(latest["adjustment_pre_close_source"], "previous_valid_close")
        self.assertAlmostEqual(float(latest["adj_pre_close"]), 10.0)

    def test_provider_halt_keeps_the_previous_adjusted_close_without_a_new_edge(self) -> None:
        source = self._split_bars()
        source.loc[1, ["open", "high", "low", "close", "pre_close", "volume", "amount"]] = [10, 10, 10, 10, 10, 0, 0]

        result = build_forward_adjusted_bars(source)
        bars = result.bars.sort_values("trade_date").reset_index(drop=True)

        self.assertTrue(bars["adjustment_valid"].all())
        self.assertEqual(bars.loc[1, "row_status"], "provider_halt_placeholder")
        self.assertEqual(bars.loc[1, "adjustment_pre_close_source"], "halt_previous_valid_close")
        self.assertAlmostEqual(float(bars.loc[1, "adj_close"]), float(bars.loc[0, "adj_close"]))


if __name__ == "__main__":
    unittest.main()
