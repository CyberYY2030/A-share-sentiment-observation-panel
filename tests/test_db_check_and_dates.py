import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from tests.synthetic_db import make_concept_db, make_stock_db, trade_days


class DbValidationTests(unittest.TestCase):
    """The '核验DB最新交易日数据' button used to fail every time on a NameError."""

    def _validate(self, n_concepts: int, last_day_rows: int = None) -> dict:
        import panel

        days = trade_days("2026-10-13", 5)
        with tempfile.TemporaryDirectory() as tmp:
            stock_db = Path(tmp) / "a_share_mvp.db"
            concept_db = Path(tmp) / "ths_concept.db"
            make_stock_db(stock_db, days, n_stocks=20)
            make_concept_db(concept_db, days, n_concepts=n_concepts)
            if last_day_rows is not None:
                con = sqlite3.connect(concept_db)
                con.execute(
                    "DELETE FROM concept_kline WHERE trade_date=? AND concept_code NOT IN "
                    "(SELECT concept_code FROM concept_kline WHERE trade_date=? ORDER BY concept_code LIMIT ?)",
                    (days[-1].replace("-", "/"), days[-1].replace("-", "/"), last_day_rows),
                )
                con.commit()
                con.close()

            with mock.patch.object(panel, "get_source_last_trade_day", return_value=days[-1]), \
                 mock.patch.object(panel, "get_remote_latest_close_day_stock_indexes", return_value=days[-1]), \
                 mock.patch.object(panel, "get_remote_latest_close_day_concept", return_value=days[-1]):
                return panel.validate_db_latest_day(str(stock_db), str(concept_db))

    def test_concept_checks_run_and_pass_on_clean_data(self) -> None:
        # About as many concepts as THS really lists; the old fixed "< 800" floor flagged this.
        result = self._validate(n_concepts=400)

        self.assertEqual(result["issues"], [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["details"]["concept_rows_on_expected"], 400)
        self.assertEqual(result["details"]["concept_rows_typical"], 400)
        self.assertEqual(result["details"]["concept_dup_codes_on_expected"], 0)

    def test_partial_concept_write_is_flagged(self) -> None:
        result = self._validate(n_concepts=400, last_day_rows=150)

        self.assertFalse(result["ok"])
        self.assertEqual(result["details"]["concept_rows_on_expected"], 150)
        self.assertTrue(any("too few rows" in s for s in result["issues"]), result["issues"])

    def test_sql_scalar(self) -> None:
        from panel import _sql_scalar

        con = sqlite3.connect(":memory:")
        self.assertEqual(_sql_scalar(con, "SELECT 7"), 7)
        self.assertIsNone(_sql_scalar(con, "SELECT 1 WHERE 0"))


class LastCompletedTradeDateTests(unittest.TestCase):
    """last_completed_trade_date_cn used to raise NameError and silently return 'today'."""

    CALENDAR = pd.Series(pd.to_datetime([
        "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14",
    ]))

    def _at(self, ts: str) -> pd.Timestamp:
        import panel

        with mock.patch.object(panel, "_cn_trade_dates_sina", return_value=self.CALENDAR):
            return panel.last_completed_trade_date_cn(dt.datetime.fromisoformat(ts))

    def test_trading_day_before_data_ready_uses_previous_day(self) -> None:
        self.assertEqual(self._at("2026-10-14 10:00"), pd.Timestamp("2026-10-13"))

    def test_trading_day_after_data_ready_uses_today(self) -> None:
        self.assertEqual(self._at("2026-10-14 18:00"), pd.Timestamp("2026-10-14"))

    def test_first_day_after_holiday_uses_last_day_before_holiday(self) -> None:
        self.assertEqual(self._at("2026-10-08 10:00"), pd.Timestamp("2026-09-30"))

    def test_holiday_uses_last_trading_day(self) -> None:
        self.assertEqual(self._at("2026-10-03 12:00"), pd.Timestamp("2026-09-30"))


if __name__ == "__main__":
    unittest.main()
