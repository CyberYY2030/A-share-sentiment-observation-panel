import unittest


class BackfillRuleTests(unittest.TestCase):
    def test_initial_build_uses_full_history_window(self) -> None:
        from app_panel_auto_update_v44 import calc_backfill_days

        self.assertEqual(calc_backfill_days(None, "2026-04-10", cap=15), 60)


if __name__ == "__main__":
    unittest.main()
