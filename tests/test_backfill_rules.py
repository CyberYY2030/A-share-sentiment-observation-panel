import unittest

import pandas as pd


class BackfillRuleTests(unittest.TestCase):
    def test_initial_build_uses_full_history_window(self) -> None:
        from panel import calc_backfill_days

        self.assertEqual(calc_backfill_days(None, "2026-04-10"), 60)

    def test_up_to_date_needs_nothing(self) -> None:
        from panel import calc_backfill_days

        self.assertEqual(calc_backfill_days("2026-10-13", "2026-10-13"), 0)
        self.assertEqual(calc_backfill_days("2026-10-13", None), 0)

    def test_one_day_behind_still_refetches_a_few_days(self) -> None:
        from panel import calc_backfill_days

        self.assertEqual(calc_backfill_days("2026-10-12", "2026-10-13"), 5)

    def test_long_break_leaves_no_gap(self) -> None:
        from panel import calc_backfill_days

        # Five weeks away: the window (in trading days) must reach back to the local last day.
        local, remote = "2026-09-04", "2026-10-13"
        missing_trading_days = len(pd.bdate_range(local, remote)) - 1
        self.assertGreaterEqual(calc_backfill_days(local, remote), missing_trading_days)


if __name__ == "__main__":
    unittest.main()
