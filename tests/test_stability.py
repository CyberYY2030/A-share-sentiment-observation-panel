"""Offline regressions for disconnected rendering and incremental recovery."""
import unittest
from unittest import mock

import pandas as pd
import requests

import panel


class OfflineQuoteTests(unittest.TestCase):
    def test_failed_eastmoney_quote_is_cached_and_other_sources_still_work(self):
        panel._ak_fetch_a_spot_em_result.clear()
        self.addCleanup(panel._ak_fetch_a_spot_em_result.clear)
        ak = mock.Mock()
        ak.stock_zh_a_spot_em.side_effect = requests.ConnectionError("offline")
        # Enough rows to satisfy the existing all-A quote quality gate.
        fallback = pd.DataFrame({"代码": [f"{600000+i:06d}" for i in range(1000)],
                                 "涨跌幅": [1.0] * 1000})
        with mock.patch.object(panel, "try_import_akshare", return_value=(ak, None)), \
             mock.patch.object(panel, "ak_fetch_a_spot_sina", return_value=fallback):
            for _ in range(2):
                quotes, source = panel.ak_fetch_a_spot_any()
                self.assertEqual(len(quotes), 1000)
                self.assertIn("stock_zh_a_spot|", source)
            with self.assertRaisesRegex(RuntimeError, "offline"):
                panel.ak_fetch_a_spot_em()
        ak.stock_zh_a_spot_em.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
