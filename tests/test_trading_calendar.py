from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from unittest import mock

from mining.trading_calendar import calendar_path, previous_trade_date, write_trade_calendar


class TradingCalendarTests(unittest.TestCase):
    def test_holiday_uses_provider_open_dates_not_weekdays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_trade_calendar(
                tmp,
                source="akshare",
                open_dates=["2026-02-13", "2026-02-24"],
                retrieved_at=dt.datetime(2026, 2, 24, tzinfo=dt.timezone.utc),
            )
            resolved = previous_trade_date(
                tmp,
                current_date="2026-02-24",
                now=dt.datetime(2026, 2, 24, tzinfo=dt.timezone.utc),
            )
        self.assertEqual(resolved.trade_date, "2026-02-13")
        self.assertIsNone(resolved.reason)

    def test_stale_calendar_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_trade_calendar(
                tmp,
                source="baostock",
                open_dates=["2026-08-18", "2026-08-19"],
                retrieved_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
            )
            resolved = previous_trade_date(
                tmp,
                current_date="2026-08-20",
                now=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
            )
        self.assertIsNone(resolved.trade_date)
        self.assertEqual(resolved.reason, "expected_trade_calendar_stale")

    def test_invalid_calendar_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = calendar_path(tmp)
            path.parent.mkdir()
            path.write_text("{not json", encoding="utf-8")
            resolved = previous_trade_date(tmp, current_date="2026-08-20")
        self.assertIsNone(resolved.trade_date)
        self.assertEqual(resolved.reason, "expected_trade_calendar_invalid")

    def test_calendar_without_prior_open_date_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_trade_calendar(
                tmp,
                source="akshare",
                open_dates=["2026-08-20"],
                retrieved_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
            )
            resolved = previous_trade_date(
                tmp,
                current_date="2026-08-20",
                now=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
            )
        self.assertIsNone(resolved.trade_date)
        self.assertEqual(resolved.reason, "expected_trade_calendar_missing_prior")

    def test_valid_calendar_returns_expected_prior_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_trade_calendar(
                tmp,
                source="akshare",
                open_dates=["2026-08-18", "2026-08-19", "2026-08-20"],
                retrieved_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
            )
            payload = json.loads(calendar_path(tmp).read_text(encoding="utf-8"))
            resolved = previous_trade_date(
                tmp,
                current_date="2026-08-20",
                now=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
            )
        self.assertEqual(payload["coverage_end"], "2026-08-20")
        self.assertEqual(resolved.trade_date, "2026-08-19")
        self.assertIsNone(resolved.reason)

    def test_offline_calendar_refresh_prefers_akshare_and_is_atomic(self) -> None:
        from offline_daily_update import _refresh_local_trade_calendar

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "offline_daily_update._akshare_trade_days", return_value=["2026-08-18", "2026-08-19"]
        ), mock.patch("offline_daily_update._baostock_trade_days", side_effect=AssertionError("fallback not needed")):
            result = _refresh_local_trade_calendar(tmp, asof="2026-08-20")
            payload = json.loads(calendar_path(tmp).read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "updated")
        self.assertEqual(payload["source"], "akshare")
        self.assertEqual(payload["coverage_end"], "2026-08-19")

    def test_offline_calendar_refresh_keeps_existing_file_when_providers_fail(self) -> None:
        from offline_daily_update import _refresh_local_trade_calendar

        with tempfile.TemporaryDirectory() as tmp:
            write_trade_calendar(tmp, source="akshare", open_dates=["2026-08-18"])
            before = calendar_path(tmp).read_text(encoding="utf-8")
            with mock.patch("offline_daily_update._akshare_trade_days", return_value=[]), mock.patch(
                "offline_daily_update._baostock_trade_days", return_value=[]
            ):
                result = _refresh_local_trade_calendar(tmp, asof="2026-08-20")
            after = calendar_path(tmp).read_text(encoding="utf-8")

        self.assertEqual(result, {"status": "retained", "reason": "provider_calendar_unavailable"})
        self.assertEqual(after, before)
