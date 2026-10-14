"""Fast paths must retain the original scalar parser semantics."""
import datetime as dt
import unittest
from unittest import mock
import numpy as np
import pandas as pd
import panel


class DateSeriesTests(unittest.TestCase):
    def test_formats_invalid_missing_and_dtype_match_scalar(self):
        values = pd.Series(['20260129', '2026-01-29', '2026/1/29',
                            '2026/01/29 15:30:00', '2026-01-29 15:30:00',
                            '2026/01/29 15:30', 'Jan 29, 2026', '', None,
                            'bad', '2026-02-30'] * 3, name='date')
        pd.testing.assert_series_equal(panel.normalize_date_series(values),
                                       values.apply(panel.normalize_date), check_exact=True)

    def test_datetime_units_timezones_and_objects_match_scalar(self):
        for values in [pd.Series(pd.date_range('2026-01-29 15:30', periods=3)),
                       pd.Series(pd.date_range('2026-01-29 15:30', periods=3, tz='Asia/Shanghai')),
                       pd.Series([dt.date(2026, 1, 29), '20260129', 20260129, None], dtype=object),
                       pd.Series([], dtype='str'), pd.Series([], dtype='datetime64[ns]')]:
            with self.subTest(dtype=values.dtype):
                pd.testing.assert_series_equal(panel.normalize_date_series(values),
                                               values.apply(panel.normalize_date), check_exact=True)

    def test_repeated_dates_only_parse_distinct_values(self):
        values = pd.Series(['2026-01-29', '2026/1/30'] * 100)
        calls = []
        original = panel.normalize_date
        def parse(value):
            calls.append(value)
            return original(value)
        with mock.patch.object(panel, 'normalize_date', parse):
            actual = panel.normalize_date_series(values)
        self.assertEqual(len(calls), 2)
        pd.testing.assert_series_equal(actual, values.apply(panel.normalize_date))

    def test_nat_retains_original_error(self):
        values = pd.Series([pd.Timestamp('2026-01-29'), pd.NaT])
        with self.assertRaises(AttributeError):
            panel.normalize_date_series(values)
        with self.assertRaises(AttributeError):
            values.apply(panel.normalize_date)
