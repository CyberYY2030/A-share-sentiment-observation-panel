"""Render the whole panel on synthetic data with every network call stubbed out."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

import panel
from tests.synthetic_db import make_concept_db, make_etf_db, make_stock_db, trade_days

DAYS = trade_days("2026-10-13", 70)


def _metric(at: AppTest, label_part: str):
    hits = [m for m in at.metric if label_part in m.label]
    assert len(hits) == 1, [m.label for m in at.metric]
    return hits[0]


class PanelSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        (base / "data").mkdir()
        make_stock_db(base / "data" / "a_share_mvp.db", DAYS)
        make_concept_db(base / "data" / "ths_concept.db", DAYS)
        make_etf_db(base / "data" / "etf_mvp.db", DAYS)

        last, prev = DAYS[-1], DAYS[-2]
        bundle = {
            "calendar_last_trade_day": last,
            "remote_latest_stock_close_day": last,
            "remote_latest_concept_close_day": last,
            "remote_latest_etf_close_day": last,
            "t1_close_day": last,
            "t2_close_day": prev,
        }
        calendar = pd.Series(pd.to_datetime(DAYS + ["2026-10-14"]))

        def fake_backfill(stock_db, concept_db, etf_db, force=False):
            return panel.get_local_last_dates(stock_db, concept_db, etf_db), ["stubbed"]

        def no_network(*args, **kwargs):
            raise RuntimeError("network disabled in tests")

        empty = pd.DataFrame()
        for name, kwargs in {
            "app_dir": {"return_value": str(base)},
            "_cn_trade_dates_sina": {"return_value": calendar},
            "discover_data_dates_bundle": {"return_value": bundle},
            "maybe_backfill_all": {"side_effect": fake_backfill},
            "validate_db_latest_day": {"return_value": {"ok": True, "expected_day": last, "issues": [], "details": {}}},
            "ak_fetch_500etf_qvix_min": {"return_value": empty},
            "ak_fetch_500etf_qvix_daily": {"return_value": empty},
            "adata_fetch_concept_current_ths": {"side_effect": no_network},
            "adata_fetch_index_current": {"side_effect": no_network},
            "ak_fetch_index_min_em": {"side_effect": no_network},
            "ak_fetch_etf_spot_em": {"side_effect": no_network},
            "em_fetch_index_pct": {"side_effect": no_network},
        }.items():
            patcher = mock.patch.object(panel, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, now: dt.datetime) -> AppTest:
        with mock.patch.object(panel, "_now_with_tz_offset", return_value=now):
            at = AppTest.from_string("import panel\npanel.main()", default_timeout=120)
            at.run()
        self.assertEqual(list(at.exception), [])
        self.assertEqual([e.value for e in at.error], [])
        return at

    def test_close_view_renders_scores_and_theme_table(self) -> None:
        at = self._run(dt.datetime(2026, 10, 13, 20, 0))  # after close

        self.assertEqual(at.title[0].value, "A股情绪观察面板")
        self.assertEqual(len(at.metric), 10)
        score = float(_metric(at, "情绪总分").value)
        self.assertTrue(0.0 <= score <= 100.0)

        theme = at.dataframe[-1].value
        self.assertEqual(len(theme), 30)  # default TopK
        self.assertIn("概念名称", theme.columns)

    def test_diagnostics_hidden_until_debug_is_on(self) -> None:
        at = self._run(dt.datetime(2026, 10, 13, 20, 0))
        main_expanders = [e.label for e in at.main.expander]
        self.assertNotIn("QVIX 调试", main_expanders)
        self.assertNotIn("数据核验（成交额/索引）", main_expanders)

        debug = [c for c in at.checkbox if c.label == "显示调试信息"]
        self.assertEqual(len(debug), 1)
        debug[0].check().run()
        self.assertEqual(list(at.exception), [])
        main_expanders = [e.label for e in at.main.expander]
        self.assertIn("QVIX 调试", main_expanders)
        self.assertIn("数据核验（成交额/索引）", main_expanders)

    def test_snapshot_view_keeps_small_spread_small(self) -> None:
        rng = np.random.default_rng(7)
        spot = pd.DataFrame({
            "sec_code": [f"{600000 + i:06d}" for i in range(300)],
            "change_pct": rng.normal(0, 2, 300),
            "amount": rng.lognormal(19, 1, 300),
        })
        index_spot = pd.DataFrame({"sec_code": ["000300", "000852"], "change_pct": [0.50, 0.53]})

        with mock.patch.object(panel, "ak_fetch_a_spot_any", return_value=(spot, "synthetic")), \
             mock.patch.object(panel, "ak_fetch_index_spot_any", return_value=(index_spot, "synthetic")):
            at = self._run(dt.datetime(2026, 10, 14, 10, 30))  # trading hours

        # CSI 1000 +0.53% vs CSI 300 +0.50%: the spread is 0.03%, not 3%.
        self.assertEqual(_metric(at, "小盘-大盘相对").value, "0.03%")
        self.assertIn("🟢", _metric(at, "动量Z").label)


if __name__ == "__main__":
    unittest.main()
