from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _raw_quotes(codes: tuple[str, ...] = ("600001", "600002", "600003")) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "代码": code,
                "名称": f"Stock {code}",
                "最新价": 10.0,
                "昨收": 9.8,
                "今开": 9.9,
                "最高": 10.1,
                "最低": 9.7,
                "成交量": 12_000_000,
                "成交额": 120_000_000,
            }
            for code in codes
        ]
    )


class QuoteSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 8, 7, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.codes = {"600001", "600002", "600003"}
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name) / "cache"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _adapter(self, responses: dict[str, object]):
        from mining.quote_snapshot import QuoteSnapshotAdapter

        calls: list[str] = []

        def fetcher(provider: str, _timeout_seconds: float) -> pd.DataFrame:
            calls.append(provider)
            response = responses[provider]
            if isinstance(response, BaseException):
                raise response
            return pd.DataFrame(response)

        return QuoteSnapshotAdapter(
            cache_dir=self.cache_dir,
            provider_names=("first", "second"),
            provider_fetcher=fetcher,
            timeout_seconds=5.0,
        ), calls

    def test_first_provider_failure_uses_second_provider_once(self) -> None:
        adapter, calls = self._adapter({"first": RuntimeError("first down"), "second": _raw_quotes()})

        result = adapter.load("2026-08-07", expected_codes=self.codes, now=self.now)

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result.status, "snapshot_usable")
        self.assertEqual(result.provider, "second")
        self.assertEqual(result.raw_rows, 3)
        self.assertEqual(result.normalized_rows, 3)
        self.assertEqual(result.coverage, 1.0)
        self.assertFalse(result.from_cache)
        self.assertEqual(len(result.errors), 1)

    def test_all_failures_are_structured_and_cool_down_without_refetch(self) -> None:
        adapter, calls = self._adapter({"first": TimeoutError("slow"), "second": RuntimeError("down")})

        first = adapter.load("2026-08-07", expected_codes=self.codes, now=self.now)
        repeated = adapter.load("2026-08-07", expected_codes=self.codes, now=self.now + dt.timedelta(seconds=30))

        self.assertEqual(first.status, "provider_failed")
        self.assertEqual(repeated.status, "provider_failed")
        self.assertEqual(calls, ["first", "second"])
        self.assertIsNotNone(repeated.retry_at)

    def test_empty_normalized_and_low_coverage_have_distinct_statuses(self) -> None:
        empty_adapter, _ = self._adapter({"first": pd.DataFrame(), "second": pd.DataFrame()})
        empty = empty_adapter.load("2026-08-07", expected_codes=self.codes, now=self.now)

        low_adapter, _ = self._adapter({"first": _raw_quotes(("600001",)), "second": _raw_quotes(("600001",))})
        low = low_adapter.load("2026-08-07", expected_codes=self.codes, now=self.now)

        self.assertEqual(empty.status, "empty_payload")
        self.assertEqual(low.status, "coverage_below_threshold")

    def test_same_day_cache_is_only_a_fresh_nonfinal_fallback(self) -> None:
        successful, _ = self._adapter({"first": _raw_quotes(), "second": _raw_quotes()})
        usable = successful.load("2026-08-07", expected_codes=self.codes, now=self.now)
        self.assertEqual(usable.status, "snapshot_usable")

        failing, _ = self._adapter({"first": RuntimeError("down"), "second": RuntimeError("down")})
        fallback = failing.load("2026-08-07", expected_codes=self.codes, now=self.now + dt.timedelta(minutes=9))
        stale = failing.load("2026-08-07", expected_codes=self.codes, now=self.now + dt.timedelta(minutes=11))
        next_day = failing.load("2026-08-10", expected_codes=self.codes, now=self.now + dt.timedelta(days=3))
        final = failing.load("2026-08-07", expected_codes=self.codes, now=self.now + dt.timedelta(minutes=9), close_final=True)

        self.assertEqual(fallback.status, "snapshot_cache_fallback")
        self.assertTrue(fallback.from_cache)
        self.assertEqual(stale.status, "snapshot_stale")
        self.assertEqual(next_day.status, "provider_failed")
        self.assertEqual(final.status, "provider_failed")

    def test_successful_snapshot_refreshes_and_only_falls_back_after_a_failure(self) -> None:
        responses: dict[str, object] = {"first": _raw_quotes(), "second": _raw_quotes()}
        adapter, calls = self._adapter(responses)

        first = adapter.load("2026-08-07", expected_codes=self.codes, now=self.now)
        responses["first"] = RuntimeError("first down")
        responses["second"] = RuntimeError("second down")
        fallback = adapter.load("2026-08-07", expected_codes=self.codes, now=self.now + dt.timedelta(minutes=1))
        refreshed_quotes = _raw_quotes()
        refreshed_quotes.iloc[0, 2] = 11.0
        responses["first"] = refreshed_quotes
        refreshed = adapter.load("2026-08-07", expected_codes=self.codes, now=self.now + dt.timedelta(minutes=3))

        self.assertEqual(first.status, "snapshot_usable")
        self.assertEqual(fallback.status, "snapshot_cache_fallback")
        self.assertTrue(fallback.from_cache)
        self.assertEqual(refreshed.status, "snapshot_usable")
        self.assertFalse(refreshed.from_cache)
        self.assertEqual(float(refreshed.frame.iloc[0]["close"]), 11.0)
        self.assertEqual(calls, ["first", "first", "second", "first"])


if __name__ == "__main__":
    unittest.main()
