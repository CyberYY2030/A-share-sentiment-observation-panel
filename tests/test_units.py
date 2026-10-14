import math
import unittest
from unittest import mock


class RelativeSpreadTests(unittest.TestCase):
    """zz1000 - hs300 arrives in percent points from every source."""

    def test_small_spread_is_not_inflated(self) -> None:
        from panel import infer_rel_from_pct_diff

        # 0.03pp used to come back as 0.03 (3%) because the unit was guessed from size.
        self.assertAlmostEqual(infer_rel_from_pct_diff(0.03), 0.0003)
        self.assertAlmostEqual(infer_rel_from_pct_diff(-0.02), -0.0002)
        self.assertAlmostEqual(infer_rel_from_pct_diff(0.0), 0.0)

    def test_normal_spread(self) -> None:
        from panel import infer_rel_from_pct_diff

        self.assertAlmostEqual(infer_rel_from_pct_diff(0.57), 0.0057)
        self.assertAlmostEqual(infer_rel_from_pct_diff(-1.8), -0.018)

    def test_impossible_spread_is_rejected(self) -> None:
        from panel import infer_rel_from_pct_diff

        self.assertTrue(math.isnan(infer_rel_from_pct_diff(57)))
        self.assertTrue(math.isnan(infer_rel_from_pct_diff(float("nan"))))
        self.assertTrue(math.isnan(infer_rel_from_pct_diff(None)))


class EastmoneyIndexPctTests(unittest.TestCase):
    def _fetch(self, data: dict) -> float:
        import panel

        resp = mock.Mock()
        resp.json.return_value = {"data": data}
        resp.raise_for_status.return_value = None
        with mock.patch.object(panel.requests, "get", return_value=resp) as get:
            pct = panel.em_fetch_index_pct("000300")
        self.assertEqual(get.call_args.kwargs["params"]["fltt"], "2")
        return pct

    def test_decimal_response(self) -> None:
        pct = self._fetch({"f43": 3901.23, "f60": 3890.0, "f170": 0.29})
        self.assertAlmostEqual(pct, (3901.23 / 3890.0 - 1) * 100)

    def test_scaled_response_gives_same_percent(self) -> None:
        # Without fltt=2 Eastmoney returns every field x100; the price ratio does not care.
        pct = self._fetch({"f43": 390123, "f60": 389000, "f170": 29})
        self.assertAlmostEqual(pct, (3901.23 / 3890.0 - 1) * 100)

    def test_falls_back_to_f170_without_prices(self) -> None:
        pct = self._fetch({"f43": "-", "f60": "-", "f170": 0.29})
        self.assertAlmostEqual(pct, 0.29)


class QvixCloseTests(unittest.TestCase):
    """Close views read QVIX from the daily series on the selected day."""

    def _daily(self):
        import pandas as pd

        # AkShare order and dtypes: string dates, float closes; rows after the day must be ignored.
        return pd.DataFrame({"date": ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"],
                             "open": [1.0, 1.0, 1.0, 1.0],
                             "close": [30.0, 25.0, 27.79, 10.0]})

    def test_value_and_percentile_stop_at_selected_day(self) -> None:
        import pandas as pd
        from panel import _qvix_close_on

        qv, pct = _qvix_close_on(self._daily(), pd.Timestamp("2026-10-13"))
        self.assertAlmostEqual(qv, 27.79)
        self.assertAlmostEqual(pct, 2 / 3)

    def test_missing_day_is_nan_not_a_neighbour(self) -> None:
        from panel import _qvix_close_on

        qv, pct = _qvix_close_on(self._daily(), "2026-10-10")
        self.assertTrue(math.isnan(qv))
        self.assertTrue(math.isnan(pct))

    def test_empty_source_is_nan(self) -> None:
        import pandas as pd
        from panel import _qvix_close_on

        self.assertTrue(all(math.isnan(v) for v in _qvix_close_on(pd.DataFrame(), "2026-10-13")))


if __name__ == "__main__":
    unittest.main()
