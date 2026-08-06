from __future__ import annotations

import unittest

import pandas as pd

from mining.trend_factors import evaluate_trend_structure, resolve_trend_profile


class TrendFactorsTests(unittest.TestCase):
    def test_profile_selection_steps_down_only_at_market_wide_thresholds(self) -> None:
        self.assertEqual(resolve_trend_profile(220).profile_id, "P200")
        self.assertEqual(resolve_trend_profile(140).profile_id, "P120")
        self.assertEqual(resolve_trend_profile(139).profile_id, "P90")
        self.assertEqual(resolve_trend_profile(110).profile_id, "P90")
        self.assertEqual(resolve_trend_profile(75).profile_id, "P60")
        self.assertIsNone(resolve_trend_profile(74))

    def test_p120_requires_140_bars_per_stock_and_strict_bullish_alignment(self) -> None:
        profile = resolve_trend_profile(140)
        dates = pd.date_range("2025-01-01", periods=140, freq="B").strftime("%Y-%m-%d")
        strong = pd.DataFrame(
            {
                "sec_code": "600001",
                "trade_date": dates,
                "adj_close": [10 * (1.003**index) for index in range(140)],
            }
        )
        tangled = pd.DataFrame(
            {
                "sec_code": "600002",
                "trade_date": dates,
                "adj_close": [10.0] * 140,
            }
        )
        short = strong.iloc[:-1].assign(sec_code="600003")
        gapped = strong.drop(strong.index[-10]).assign(sec_code="600004")
        result = evaluate_trend_structure(pd.concat([strong, tangled, short, gapped], ignore_index=True), profile).set_index("sec_code")

        self.assertTrue(result.loc["600001", "trend_structure_pass"])
        self.assertFalse(result.loc["600002", "trend_structure_pass"])
        self.assertFalse(result.loc["600003", "history_sufficient"])
        self.assertFalse(result.loc["600004", "history_sufficient"])


if __name__ == "__main__":
    unittest.main()
