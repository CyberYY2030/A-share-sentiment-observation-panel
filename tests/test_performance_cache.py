"""Minute series refresh at the same 60-second cadence as other live feeds."""
import unittest
from unittest import mock
import pandas as pd
import panel


class MinuteCacheTests(unittest.TestCase):
    def setUp(self):
        panel.ak_fetch_500etf_qvix_min.clear()
        panel.ak_fetch_index_min_em.clear()
        self.addCleanup(panel.ak_fetch_500etf_qvix_min.clear)
        self.addCleanup(panel.ak_fetch_index_min_em.clear)

    def test_qvix_repeated_reads_are_cached_and_values_are_unchanged(self):
        expected = pd.DataFrame({'time': ['14:20:21'], 'qvix': [27.79]})
        ak = mock.Mock()
        ak.index_option_500etf_min_qvix.return_value = expected.copy()
        with mock.patch.object(panel, 'try_import_akshare', return_value=(ak, None)):
            first = panel.ak_fetch_500etf_qvix_min()
            second = panel.ak_fetch_500etf_qvix_min()
            first.loc[0, 'qvix'] = -1  # a consumer must not mutate the cached payload
            third = panel.ak_fetch_500etf_qvix_min()
        self.assertEqual(ak.index_option_500etf_min_qvix.call_count, 1)
        pd.testing.assert_frame_equal(second, expected, check_exact=True)
        pd.testing.assert_frame_equal(third, expected, check_exact=True)

    def test_qvix_failures_are_not_cached(self):
        expected = pd.DataFrame({'qvix': [27.79]})
        ak = mock.Mock()
        ak.index_option_500etf_min_qvix.side_effect = [RuntimeError('temporary outage'), expected]
        with mock.patch.object(panel, 'try_import_akshare', return_value=(ak, None)):
            with self.assertRaisesRegex(RuntimeError, 'temporary outage'):
                panel.ak_fetch_500etf_qvix_min()
            actual = panel.ak_fetch_500etf_qvix_min()
        self.assertEqual(ak.index_option_500etf_min_qvix.call_count, 2)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)

    def test_index_minute_cache_retains_code_keys_and_signature_fallback(self):
        expected = pd.DataFrame({'close': [3901.23]})
        def fetch(symbol, **kwargs):
            if 'period' not in kwargs:
                raise TypeError('period required')
            return expected
        ak = mock.Mock()
        ak.index_zh_a_hist_min_em.side_effect = fetch
        with mock.patch.object(panel, 'try_import_akshare', return_value=(ak, None)):
            pd.testing.assert_frame_equal(panel.ak_fetch_index_min_em('000300'), expected)
            pd.testing.assert_frame_equal(panel.ak_fetch_index_min_em('000300'), expected)
            pd.testing.assert_frame_equal(panel.ak_fetch_index_min_em('000852'), expected)
        self.assertEqual(ak.index_zh_a_hist_min_em.call_count, 4)
