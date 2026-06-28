import math
import unittest

import pandas as pd


class MiningFeatureTests(unittest.TestCase):
    def test_change_pct_uses_percentage_units(self) -> None:
        from mining.features import change_pct

        value = change_pct({"close": 10.6, "pre_close": 10.0})
        self.assertAlmostEqual(value, 6.0)

    def test_upper_shadow_pct_uses_max_of_open_and_close(self) -> None:
        from mining.features import upper_shadow_pct

        value = upper_shadow_pct({"open": 10.0, "close": 10.5, "high": 11.025})
        self.assertAlmostEqual(value, 5.0)

    def test_high_breakout_pct_measures_from_open(self) -> None:
        from mining.features import high_breakout_from_open_pct

        value = high_breakout_from_open_pct({"open": 10.0, "high": 10.31})
        self.assertAlmostEqual(value, 3.1)

    def test_high_breakout_pct_measures_from_close(self) -> None:
        from mining.features import high_breakout_from_close_pct

        value = high_breakout_from_close_pct({"close": 10.0, "high": 10.52})
        self.assertAlmostEqual(value, 5.2)

    def test_ret_n_returns_nan_when_history_is_short(self) -> None:
        from mining.features import ret_n

        value = ret_n(pd.Series([10.0, 10.5]), 5)
        self.assertTrue(math.isnan(value))

    def test_rps_returns_percentile_scale(self) -> None:
        from mining.features import rps

        values = rps(pd.Series([1.0, 2.0, 3.0]))
        self.assertEqual(values.iloc[-1], 100.0)
        self.assertGreater(values.iloc[1], values.iloc[0])

    def test_board_kind_handles_main_gem_and_star(self) -> None:
        from mining.features import board_kind

        self.assertEqual(board_kind("600001"), "main")
        self.assertEqual(board_kind("300001"), "gem")
        self.assertEqual(board_kind("688001"), "star")


    def test_limit_up_price_uses_board_rate(self) -> None:
        from mining.features import limit_up_price

        self.assertEqual(limit_up_price(10.0, "main"), 11.0)
        self.assertEqual(limit_up_price(10.0, "gem"), 12.0)
        self.assertEqual(limit_up_price(10.0, "star"), 12.0)

    def test_is_limit_up_respects_main_board_tolerance(self) -> None:
        from mining.features import is_limit_up

        self.assertTrue(is_limit_up(10.99, 10.0, "main"))
        self.assertTrue(is_limit_up(11.001, 10.0, "main"))
        self.assertFalse(is_limit_up(10.98, 10.0, "main"))

    def test_is_limit_up_respects_gem_and_invalid_prices(self) -> None:
        from mining.features import is_limit_up

        self.assertTrue(is_limit_up(11.99, 10.0, "gem"))
        self.assertFalse(is_limit_up(11.98, 10.0, "gem"))
        self.assertFalse(is_limit_up(12.0, 0.0, "gem"))
        self.assertFalse(is_limit_up(0.0, 10.0, "gem"))

    def test_rolling_new_high_checks_latest_against_window(self) -> None:
        from mining.features import rolling_new_high

        self.assertTrue(rolling_new_high(pd.Series([10.0, 10.5, 10.4, 10.8]), 3))
        self.assertFalse(rolling_new_high(pd.Series([10.0, 10.8, 10.4, 10.7]), 3))
        self.assertTrue(rolling_new_high(pd.Series([10.0, 11.0, 10.5]), 1))
        self.assertFalse(rolling_new_high(pd.Series([], dtype=float), 3))


    def test_moving_average_requires_full_window(self) -> None:
        from mining.features import moving_average

        values = moving_average(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)

        self.assertTrue(math.isnan(values.iloc[0]))
        self.assertTrue(math.isnan(values.iloc[1]))
        self.assertAlmostEqual(values.iloc[2], 2.0)
        self.assertAlmostEqual(values.iloc[3], 3.0)

    def test_moving_average_invalid_window_returns_nan_series(self) -> None:
        from mining.features import moving_average

        values = moving_average(pd.Series([1.0, 2.0]), 0)

        self.assertTrue(values.isna().all())
    def test_volume_shrink_ratio_uses_recent_over_run_average(self) -> None:
        from mining.features import volume_shrink_ratio

        value = volume_shrink_ratio(pd.Series([5.0, 7.0]), pd.Series([10.0, 10.0]))

        self.assertAlmostEqual(value, 0.6)

    def test_volume_shrink_ratio_invalid_run_returns_nan(self) -> None:
        from mining.features import volume_shrink_ratio

        value = volume_shrink_ratio(pd.Series([5.0]), pd.Series([0.0]))

        self.assertTrue(math.isnan(value))
if __name__ == "__main__":
    unittest.main()
