"""Regressions for the first real ETF backfill and official endpoint responses."""
from __future__ import annotations

import datetime as dt
import io
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

import backfill_etf as etf
from tests.synthetic_db import make_stock_db


class RealETFBackfillTests(unittest.TestCase):
    def test_default_asof_excludes_intraday(self):
        self.assertEqual(etf.default_asof(dt.datetime(2026, 10, 14, 13)), "2026-10-13")
        self.assertEqual(etf.default_asof(dt.datetime(2026, 10, 14, 15)), "2026-10-14")

    def test_ref_calendar_respects_asof_even_with_short_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "stocks.db"
            make_stock_db(db, ["2026-10-12", "2026-10-13", "2026-10-14"], n_stocks=2)
            self.assertEqual(etf.load_trade_dates_from_ref_db(str(db), 2, "2026-10-13"),
                             ["2026-10-12", "2026-10-13"])
            self.assertEqual(etf.load_trade_dates_from_ref_db(str(db), 60, "2026-10-13"), [])

    def test_empty_scale_fails_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "empty ETF scale"):
            etf._filter_equity_etf(pd.DataFrame(), "SSE")

    def test_szse_official_main_domain_adapter_preserves_shares_and_date(self):
        raw = pd.DataFrame({"基金代码": ["159001"], "基金简称": ["测试ETF"],
                            "投资类别": ["股票基金"], "当前规模(份)": ["127,524,400"]})
        data = io.BytesIO()
        raw.to_excel(data, index=False)
        workbook = Mock(content=data.getvalue())
        metadata = Mock()
        metadata.json.return_value = [{"metadata": {"tabkey": "tab1", "subname": "2026-10-14",
                                        "cols": {"dqgm": "当前规模<br>（万份）<br>（2026-10-13）"}}}]
        def unavailable():
            raise requests.exceptions.SSLError("TLS EOF")
        ak = types.SimpleNamespace(fund_etf_scale_szse=unavailable)
        with patch("requests.get", side_effect=[requests.exceptions.SSLError("TLS EOF"), workbook, metadata]) as get, \
             patch.object(etf.time, "sleep"):
            df = etf.fetch_scale_szse(ak, "20261013")
        self.assertEqual(df.iloc[0]["基金份额"], 127524400)
        self.assertEqual(df.attrs["trade_date"], "2026-10-13")
        self.assertEqual(get.call_args_list[0].kwargs["timeout"], 20)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["User-Agent"], "Mozilla/5.0")
        self.assertEqual(df.iloc[0]["基金代码"], "159001")

    def test_szse_excludes_lof_even_when_name_contains_etf(self):
        raw = pd.DataFrame({
            "基金代码": ["159001", "160119", "160105", "159002"],
            "基金简称": ["测试ETF", "500ETF联接LOF", "南方积配LOF", "债券ETF"],
            "基金类别": ["ETF", "LOF", "LOF", "ETF"],
            "投资类别": ["股票基金", "股票基金", "混合基金", "债券型"],
        })
        filtered = etf._filter_equity_etf(raw, "SZSE")
        self.assertEqual(filtered["基金代码"].tolist(), ["159001"])

    def test_safe_call_stops_after_three_attempts(self):
        source = Mock(side_effect=RuntimeError("source unavailable"))
        with patch.object(etf.time, "sleep"), self.assertRaisesRegex(RuntimeError, "source unavailable"):
            etf._safe_call(source)
        self.assertEqual(source.call_count, 3)

    def test_sse_failure_stops_batch_before_szse_can_mask_it(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "SSE backfill stopped"):
            self._run_main(tmp, "2026-10-13", sse_failure=True)

    def _run_main(self, tmp, snapshot_date, row_count=None, sse_failure=False):
        db = Path(tmp) / "etf.db"
        ref = Path(tmp) / "stocks.db"
        make_stock_db(ref, ["2026-10-12", "2026-10-13"], n_stocks=2)
        sse = pd.DataFrame({"基金代码": ["510010"], "基金简称": ["测试ETF"],
                            "ETF类型": ["单市"], "基金份额": [100.]})
        sz = pd.DataFrame({"基金代码": ["159001"], "基金简称": ["测试ETF"],
                           "投资类别": ["股票基金"], "基金份额": [200.]})
        if snapshot_date:
            sz.attrs["trade_date"] = snapshot_date
        fake_ak = types.SimpleNamespace(fund_etf_scale_szse=lambda: sz)
        args = ["backfill_etf.py", "--db", str(db), "--ref-db", str(ref), "--asof",
                "2026-10-13", "--days", "2", "--out-dir", str(Path(tmp) / "csv")]
        with patch.object(sys, "argv", args), patch.dict(sys.modules, {"akshare": fake_ak}), \
             patch.object(etf, "fetch_scale_sse", return_value=sse,
                          side_effect=[sse, RuntimeError("source unavailable")] if sse_failure else None), \
             patch.object(etf, "fetch_scale_szse", return_value=sz):
            if row_count is None:
                etf.main()
            else:
                with patch.object(etf, "upsert_scale", return_value=row_count):
                    etf.main()
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT trade_date,share FROM etf_scale WHERE exchange='SZSE'").fetchall()

    def test_szse_latest_saved_on_its_actual_date_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run_main(tmp, "2026-10-12"), [("2026-10-12", 200.)])

    def test_szse_unverified_or_out_of_range_snapshot_not_misdated(self):
        for snapshot_date in [None, "2026-10-14"]:
            with self.subTest(snapshot_date=snapshot_date), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(self._run_main(tmp, snapshot_date), [])

    def test_zero_saved_rows_fails_main(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "saved zero rows"):
            self._run_main(tmp, "2026-10-13", row_count=0)


if __name__ == "__main__":
    unittest.main()
