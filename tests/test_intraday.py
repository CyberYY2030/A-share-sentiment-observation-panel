from __future__ import annotations

import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tests._mining_test_helpers import create_sample_market_dbs
from tests.test_launch_burst import _insert_history, _synthetic_history


def _spot_from_history(history: pd.DataFrame) -> pd.DataFrame:
    row = history.iloc[-1]
    return pd.DataFrame(
        [
            {
                "sec_code": row.sec_code,
                "sec_name": row.sec_name,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "pre_close": row.pre_close,
                "volume": row.volume,
                "amount": row.amount,
            }
        ]
    )


def _intraday_history(code: str = "601999", target_date: str = "2026-06-18") -> pd.DataFrame:
    return _synthetic_history(
        code,
        target_date=target_date,
        target_volume=30_000_000,
        target_amount=318_000_000,
    )


def _tencent_test_codes(count: int = 5_203) -> tuple[str, ...]:
    return tuple(
        [f"{index:06d}" for index in range(min(count, 4_000))]
        + [f"{300_000 + index:06d}" for index in range(max(0, count - 4_000))]
    )


def _tencent_test_payload(codes: tuple[str, ...]) -> dict[str, dict[str, object]]:
    observed = dt.datetime(2026, 8, 10, 10, 15)
    return {
        code: {
            "code": code,
            "name": f"Stock {code}",
            "now": 10.0,
            "close": 9.8,
            "open": 9.9,
            "high": 10.1,
            "low": 9.7,
            "volume": 12_000_000,
            "成交额(万)": 120_000_000,
            "datetime": observed,
        }
        for code in codes
    }


def _structured_frame_worker(result_queue) -> None:
    frame = pd.DataFrame([{"sec_code": "600001"}])
    frame.attrs["tencent_batch_diagnostics"] = {"success_batches": 1, "timeout_batches": 1}
    result_queue.put(("ok", frame))


class IntradayLaunchTests(unittest.TestCase):
    def test_parent_process_receives_structured_partial_frame(self) -> None:
        from mining.intraday import _call_frame_worker_with_timeout

        frame = _call_frame_worker_with_timeout(
            _structured_frame_worker,
            (),
            "structured_partial",
            3.0,
        )

        self.assertEqual(list(frame["sec_code"]), ["600001"])
        self.assertEqual(frame.attrs["tencent_batch_diagnostics"]["success_batches"], 1)
        self.assertEqual(frame.attrs["tencent_batch_diagnostics"]["timeout_batches"], 1)

    def test_tencent_87_batch_executor_limits_concurrency_and_keeps_partial_results(self) -> None:
        from mining.intraday import _fetch_tencent_batches

        codes = _tencent_test_codes()
        lock = threading.Lock()
        active = 0
        observed_max = 0
        failed_batch_first = codes[10 * 60]

        def fetch(batch: tuple[str, ...], _timeout: float, _prefix: bool):
            nonlocal active, observed_max
            with lock:
                active += 1
                observed_max = max(observed_max, active)
            try:
                time.sleep(0.002)
                if batch[0] == failed_batch_first:
                    raise TimeoutError("one slow batch")
                return _tencent_test_payload(batch)
            finally:
                with lock:
                    active -= 1

        frame, diagnostics = _fetch_tencent_batches(
            codes,
            batch_fetcher=fetch,
            max_workers=8,
            request_timeout_seconds=0.05,
            collection_budget_seconds=0.5,
        )

        self.assertEqual(diagnostics["total_batches"], 87)
        self.assertEqual(diagnostics["success_batches"], 86)
        self.assertEqual(diagnostics["timeout_batches"], 1)
        self.assertEqual(diagnostics["error_batches"], 0)
        self.assertLessEqual(observed_max, 8)
        self.assertLessEqual(diagnostics["max_concurrency"], 8)
        self.assertEqual(len(frame), len(codes) - 60)
        self.assertGreater(diagnostics["received_codes"], 0.80 * len(codes))
        self.assertEqual(diagnostics["missing_codes"], 60)

    def test_tencent_batch_deadline_returns_completed_rows_before_parent_budget(self) -> None:
        from mining.intraday import _fetch_tencent_batches

        codes = _tencent_test_codes(600)

        def fetch(batch: tuple[str, ...], _timeout: float, _prefix: bool):
            if batch[0] == codes[60]:
                time.sleep(0.25)
            else:
                time.sleep(0.002)
            return _tencent_test_payload(batch)

        started = time.monotonic()
        frame, diagnostics = _fetch_tencent_batches(
            codes,
            batch_fetcher=fetch,
            max_workers=2,
            request_timeout_seconds=0.2,
            collection_budget_seconds=0.06,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.20)
        self.assertGreater(len(frame), 0)
        self.assertGreaterEqual(diagnostics["timeout_batches"], 1)
        self.assertEqual(diagnostics["success_batches"] + diagnostics["timeout_batches"] + diagnostics["error_batches"], 10)

    def test_tencent_batch_merge_deduplicates_and_excludes_beijing_codes(self) -> None:
        from mining.intraday import _fetch_tencent_batches

        requested = ("600001", "600001", "000001", "920001")

        def fetch(batch: tuple[str, ...], _timeout: float, _prefix: bool):
            return _tencent_test_payload(batch)

        frame, diagnostics = _fetch_tencent_batches(
            requested,
            batch_fetcher=fetch,
            max_workers=4,
            request_timeout_seconds=0.2,
            collection_budget_seconds=0.5,
        )

        self.assertEqual(list(frame["sec_code"]), ["000001", "600001"])
        self.assertEqual(diagnostics["requested_codes"], 3)
        self.assertEqual(diagnostics["received_codes"], 2)
        self.assertEqual(diagnostics["missing_codes"], 1)
        self.assertEqual(diagnostics["provider_timestamp_min"], "2026-08-10T10:15:00")
        self.assertEqual(diagnostics["provider_timestamp_max"], "2026-08-10T10:15:00")
    def test_intraday_matches_eod_when_synthetic_bar_matches_eod_bar(self) -> None:
        from mining.db import connect
        from mining.intraday import scan_intraday
        from mining.scanners.launch_burst import LaunchBurstScanner, select_candidates_from_history

        history = _intraday_history("601999", target_date="2026-06-18")
        scanner = LaunchBurstScanner()
        expected = select_candidates_from_history(
            history,
            "2026-06-18",
            scanner.params,
            scanner.strategy_id,
            scanner.version,
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _insert_history(conn, history)
                before = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                result = scan_intraday(
                    conn,
                    now=dt.datetime(2026, 6, 18, 14, 30),
                    spot_df=_spot_from_history(history),
                    out_dir=base / "output" / "intraday",
                )
                after = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                report_exists = Path(result["report"]).exists()
            finally:
                conn.close()

        self.assertEqual([item.sec_code for item in result["candidates"]], [item.sec_code for item in expected])
        self.assertEqual(result["candidates"][0].features, expected[0].features)
        self.assertEqual(before, after)
        self.assertTrue(report_exists)

    def test_existing_today_bar_is_replaced_without_duplicate(self) -> None:
        from mining.db import connect
        from mining.intraday import build_synthetic_history

        history = _intraday_history("601999", target_date="2026-06-18")
        spot = _spot_from_history(history)
        spot.loc[0, "close"] = 10.8
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _insert_history(conn, history)
                combined = build_synthetic_history(conn, spot, "2026-06-18")
            finally:
                conn.close()

        today = combined[
            combined["sec_code"].astype(str).eq("601999")
            & combined["trade_date"].astype(str).eq("2026-06-18")
        ]
        self.assertEqual(len(today), 1)
        self.assertAlmostEqual(today.iloc[0]["close"], 10.8)

    def test_non_trading_window_fails_before_fetch(self) -> None:
        from mining.db import connect
        from mining.intraday import scan_intraday

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                with patch("mining.intraday.fetch_spot_frame") as fetch:
                    with self.assertRaisesRegex(RuntimeError, "only allowed"):
                        scan_intraday(conn, now=dt.datetime(2026, 6, 18, 13, 59), out_dir=base / "output")
                    fetch.assert_not_called()
            finally:
                conn.close()

    def test_both_spot_endpoints_fail_closed_after_bounded_retries(self) -> None:
        from mining.intraday import fetch_spot_frame

        with patch(
            "mining.intraday._call_endpoint_with_timeout",
            side_effect=TimeoutError("unit timeout"),
        ) as call:
            with self.assertRaisesRegex(RuntimeError, "All A-share spot endpoints failed"):
                fetch_spot_frame(timeout_seconds=0.01, retries=2)

        self.assertEqual(call.call_count, 4)
        self.assertEqual(
            [item.args[0] for item in call.call_args_list],
            ["stock_zh_a_spot_em", "stock_zh_a_spot_em", "stock_zh_a_spot", "stock_zh_a_spot"],
        )

    def test_spot_normalization_keeps_share_volume_when_amount_implies_same_unit(self) -> None:
        from mining.intraday import normalize_spot_frame

        normalized = normalize_spot_frame(
            pd.DataFrame(
                [
                    {
                        "code": "sh600001",
                        "name": "Alpha",
                        "price": 10.0,
                        "pre_close": 9.8,
                        "open": 9.9,
                        "high": 10.1,
                        "low": 9.7,
                        "volume": 12_000_000,
                        "amount": 120_000_000,
                    }
                ]
            )
        )

        self.assertEqual(normalized.iloc[0]["sec_code"], "600001")
        self.assertEqual(normalized.iloc[0]["volume"], 12_000_000)
        self.assertEqual(normalized.iloc[0]["amount"], 120_000_000)

    def test_spot_normalization_converts_lot_volume_when_amount_implies_lots(self) -> None:
        from mining.intraday import normalize_spot_frame

        normalized = normalize_spot_frame(
            pd.DataFrame(
                [
                    {
                        "code": "sh600001",
                        "name": "Alpha",
                        "price": 10.0,
                        "pre_close": 9.8,
                        "open": 9.9,
                        "high": 10.1,
                        "low": 9.7,
                        "volume": 120_000,
                        "amount": 120_000_000,
                    }
                ]
            )
        )

        self.assertEqual(normalized.iloc[0]["volume"], 12_000_000)
        self.assertEqual(normalized.iloc[0]["amount"], 120_000_000)

    def test_spot_normalization_rejects_ambiguous_volume_amount_ratio(self) -> None:
        from mining.intraday import normalize_spot_frame

        with self.assertRaisesRegex(RuntimeError, "volume unit"):
            normalize_spot_frame(
                pd.DataFrame(
                    [
                        {
                            "code": "sh600001",
                            "name": "Alpha",
                            "price": 10.0,
                            "pre_close": 9.8,
                            "open": 9.9,
                            "high": 10.1,
                            "low": 9.7,
                            "volume": 1_000_000,
                            "amount": 100_000_000,
                        }
                    ]
                )
            )

    def test_frozen_snapshot_matching_latest_history_fails_closed(self) -> None:
        from mining.db import connect
        from mining.intraday import build_synthetic_history

        history = _intraday_history("601999", target_date="2026-06-18")
        spot = _spot_from_history(history)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _insert_history(conn, history)
                with self.assertRaisesRegex(RuntimeError, "frozen"):
                    build_synthetic_history(conn, spot, "2026-06-19")
            finally:
                conn.close()

    def test_stale_local_history_fails_closed(self) -> None:
        from mining.db import connect
        from mining.intraday import build_synthetic_history

        history = _intraday_history("601999", target_date="2026-06-18")
        spot = _spot_from_history(history)
        spot.loc[0, "close"] = 10.9
        spot.loc[0, "volume"] = 18_000_000
        spot.loc[0, "amount"] = spot.loc[0, "close"] * spot.loc[0, "volume"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _insert_history(conn, history)
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    build_synthetic_history(conn, spot, "2026-06-22")
            finally:
                conn.close()

    def test_stale_local_history_can_be_overridden_and_report_shows_baseline(self) -> None:
        from mining.db import connect
        from mining.intraday import scan_intraday

        history = _intraday_history("601999", target_date="2026-06-18")
        spot = _spot_from_history(history)
        spot.loc[0, "close"] = 10.9
        spot.loc[0, "volume"] = 18_000_000
        spot.loc[0, "amount"] = spot.loc[0, "close"] * spot.loc[0, "volume"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _insert_history(conn, history)
                with patch.dict(os.environ, {"INTRADAY_ALLOW_DB_LAG": "1"}):
                    result = scan_intraday(
                        conn,
                        now=dt.datetime(2026, 6, 22, 14, 30),
                        spot_df=spot,
                        out_dir=base / "output" / "intraday",
                    )
                body = Path(result["report"]).read_text(encoding="utf-8")
            finally:
                conn.close()

        self.assertIn("T-1=2026-06-18", body)
        self.assertTrue(result["db_lag_allowed"])

if __name__ == "__main__":
    unittest.main()
