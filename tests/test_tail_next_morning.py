from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pandas as pd


def make_bars(*, amount: float = 10_000_000.0, volume: float = 1_000_000.0) -> pd.DataFrame:
    rows = []
    for index in range(240):
        close = 10.0 + index * 0.01
        rows.append(
            {
                "open": close - 0.005,
                "high": close + 0.01,
                "low": close - 0.01,
                "close": close,
                "amount": amount,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows)


def listing() -> dict[str, object]:
    return {
        "listing_age_source": "daily_k_first_date",
        "listing_history_sessions": 20,
        "daily_amount": 2_400_000_000.0,
        "daily_columns": ["date", "amount"],
    }


class TailNextMorningTests(unittest.TestCase):
    _PREFLIGHT_DATES = ("20260105", "20260106", "20260107", "20260108", "20260109")

    def _preflight_row(self, *, mutate=None, omit_next_day_code: bool = False, truncate_next_day_code: bool = False):
        from mining.tail_next_morning import PREFLIGHT_CODES, _preflight_sample, rank_scored_rows

        sample = {
            "sample_id": "causal_loader_regression",
            "target_date": self._PREFLIGHT_DATES[3],
            "dates": self._PREFLIGHT_DATES,
            "expected_source_kind": "zip",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for trade_date in self._PREFLIGHT_DATES:
                archive_path = root / trade_date[:4] / trade_date[4:6] / f"{trade_date}.zip"
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for code in PREFLIGHT_CODES:
                        if omit_next_day_code and trade_date == self._PREFLIGHT_DATES[-1] and code == "000001":
                            continue
                        bars = make_bars()
                        if mutate is not None and code == "000001":
                            mutate(bars, trade_date)
                        if truncate_next_day_code and trade_date == self._PREFLIGHT_DATES[-1] and code == "000001":
                            bars = bars.iloc[:-1]
                        archive.writestr(f"sz/{code}.csv", bars.to_csv(index=False).encode("utf-8"))
            with mock.patch("mining.tail_next_morning.listing_evidence", return_value=listing()):
                preflight = _preflight_sample(root, root, sample)
        rows = {row["sec_code"]: row for row in preflight["rows"]}
        ranking = rank_scored_rows(
            {"sec_code": code, "score": row["raw_features"].get("tail_return", -1.0)}
            for code, row in rows.items()
            if row["feature_status"] == "ready"
        )
        return rows["000001"], ranking

    @staticmethod
    def _feature_signature(row: dict[str, object]) -> dict[str, object]:
        return {
            key: row[key]
            for key in ("feature_status", "feature_reason", "raw_features", "features", "d1_amount")
        }

    def test_frozen_windows_have_exact_bar_counts_and_vwap(self) -> None:
        from mining.tail_next_morning import vwap_for_window, window_bars

        bars = make_bars()
        self.assertEqual(len(window_bars(bars, "14:20", "14:50")), 30)
        self.assertEqual(len(window_bars(bars, "14:51", "14:56")), 5)
        self.assertEqual(len(window_bars(bars, "10:00", "10:05")), 5)
        vwap = vwap_for_window(bars, "14:51", "14:56")
        self.assertEqual(vwap["status"], "ready")
        self.assertAlmostEqual(vwap["vwap"], 10.0)

    def test_feature_path_ignores_1450_and_later_bars_and_rank(self) -> None:
        from mining.tail_next_morning import feature_snapshot, rank_scored_rows

        day = make_bars()
        prior = [make_bars() for _ in range(3)]
        before = feature_snapshot("000001", day, prior, listing())
        changed = day.copy()
        changed.loc[230:, ["open", "high", "low", "close", "amount", "volume"]] = [99.0, 100.0, 98.0, 99.0, 999_000_000.0, 9_000_000.0]
        after = feature_snapshot("000001", changed, prior, listing())
        self.assertEqual(before, after)
        ranked_before = rank_scored_rows([{"sec_code": "000001", "score": before["raw_features"]["tail_return"]}, {"sec_code": "600000", "score": 0.0}])
        ranked_after = rank_scored_rows([{"sec_code": "000001", "score": after["raw_features"]["tail_return"]}, {"sec_code": "600000", "score": 0.0}])
        self.assertEqual(ranked_before, ranked_after)

    def test_preflight_loader_isolates_future_windows_without_changing_signal(self) -> None:
        baseline, baseline_rank = self._preflight_row()
        baseline_feature = self._feature_signature(baseline)
        baseline_outcome = baseline["outcome"]
        self.assertEqual(baseline["outcome"]["outcome_status"], "ready")

        cases = (
            ("d_1450_buffer_non_numeric", "20260108", 230, "not-a-number", "feature_and_outcome_unchanged"),
            ("d_buy_nan", "20260108", 231, float("nan"), "unavailable_buy_window_invalid"),
            ("d_after_buy_inf", "20260108", 236, float("inf"), "feature_and_outcome_unchanged"),
            ("dplus1_outcome_non_numeric", "20260109", 30, "not-a-number", "unavailable_next_day_window_invalid"),
            ("dplus1_after_outcome_nan", "20260109", 35, float("nan"), "feature_and_outcome_unchanged"),
        )
        for name, trade_date, index, value, expected in cases:
            with self.subTest(name=name):
                def mutate(bars, date, *, target=trade_date, bar_index=index, changed_value=value):
                    if date == target:
                        if isinstance(changed_value, str):
                            bars["close"] = bars["close"].astype(object)
                        bars.loc[bar_index, "close"] = changed_value

                row, ranking = self._preflight_row(mutate=mutate)
                self.assertEqual(self._feature_signature(row), baseline_feature)
                self.assertEqual(ranking, baseline_rank)
                if expected == "feature_and_outcome_unchanged":
                    self.assertEqual(row["outcome"], baseline_outcome)
                else:
                    self.assertEqual(row["outcome"]["outcome_status"], expected)

        row, ranking = self._preflight_row(omit_next_day_code=True)
        self.assertEqual(self._feature_signature(row), baseline_feature)
        self.assertEqual(ranking, baseline_rank)
        self.assertEqual(row["outcome"]["outcome_status"], "unavailable_next_day_session")

        row, ranking = self._preflight_row(truncate_next_day_code=True)
        self.assertEqual(self._feature_signature(row), baseline_feature)
        self.assertEqual(ranking, baseline_rank)
        self.assertEqual(row["outcome"]["outcome_status"], "unavailable_next_day_session")

    def test_preflight_loader_keeps_signal_window_fail_closed(self) -> None:
        def mutate(bars, date):
            if date == "20260108":
                bars.loc[229, "close"] = 0.0

        row, ranking = self._preflight_row(mutate=mutate)
        self.assertEqual(row["feature_status"], "isolated")
        self.assertEqual(row["feature_reason"], "non_positive_ohlc")
        self.assertNotIn("000001", [ranked["sec_code"] for ranked in ranking])

    def test_dplus1_changes_outcome_only(self) -> None:
        from mining.tail_next_morning import feature_snapshot, outcome_snapshot

        day = make_bars()
        prior = [make_bars() for _ in range(3)]
        features = feature_snapshot("000001", day, prior, listing())
        first = outcome_snapshot(day, make_bars())
        next_day = make_bars()
        next_day.loc[30:34, ["open", "high", "low", "close", "amount", "volume"]] = [20.0, 20.1, 19.9, 20.0, 20_000_000.0, 1_000_000.0]
        second = outcome_snapshot(day, next_day)
        self.assertNotEqual(first["gross_return"], second["gross_return"])
        self.assertEqual(features, feature_snapshot("000001", day, prior, listing()))

    def test_six_features_and_strict_d1_amount_threshold(self) -> None:
        from mining.tail_next_morning import add_market_relative_features, feature_snapshot

        day = make_bars()
        exactly_threshold = [make_bars(amount=MIN_D1_AMOUNT / 240.0) for _ in range(3)]
        isolated = feature_snapshot("000001", day, exactly_threshold, listing())
        self.assertEqual(isolated["feature_reason"], "d1_amount_not_strictly_above_500m")
        ready = feature_snapshot("000001", day, [make_bars() for _ in range(3)], listing())
        self.assertEqual(ready["feature_status"], "ready")
        self.assertEqual(
            set(ready["raw_features"]),
            {"tail_return", "tail_end_location", "tail_amount_accel", "pre_tail_return", "activity_ratio", "recent_3date_return"},
        )
        finalized = add_market_relative_features([ready])[0]
        self.assertEqual(
            set(finalized["features"]),
            {"tail_return_rel", "tail_end_location", "tail_amount_accel", "pre_tail_return_rel", "activity_ratio", "recent_3date_return_rel"},
        )

    def test_vwap_zero_volume_and_invalid_prices_fail_closed(self) -> None:
        from mining.tail_next_morning import TailDataError, normalize_minute_frame, vwap_for_window

        zero = make_bars()
        zero.loc[231:235, ["amount", "volume"]] = 0.0
        self.assertEqual(vwap_for_window(zero, "14:51", "14:56")["status"], "zero_volume_window")
        invalid = make_bars()
        invalid.loc[4, "close"] = 0.0
        with self.assertRaisesRegex(TailDataError, "non_positive_ohlc"):
            normalize_minute_frame(invalid)

    def test_duplicate_timestamp_missing_bar_zip_directory_and_prefix_drift(self) -> None:
        from mining.tail_next_morning import TailDataError, load_minute_session, locate_day_source, normalize_minute_frame

        frame = make_bars()
        labels = [f"09:{30 + index:02d}:00" if index < 30 else "13:00:00" for index in range(240)]
        frame["time"] = labels
        frame.loc[1, "time"] = frame.loc[0, "time"]
        with self.assertRaisesRegex(TailDataError, "duplicate_minute_timestamp"):
            normalize_minute_frame(frame)
        with self.assertRaisesRegex(TailDataError, "session_bar_count"):
            normalize_minute_frame(make_bars().iloc[:-1])

        payload = make_bars().to_csv(index=False).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "day.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("20260108/sz/000001.csv", payload)
            directory = root / "day" / "sh"
            directory.mkdir(parents=True)
            (directory / "600000.csv").write_bytes(payload)
            plain = root / "plain"
            plain.mkdir()
            (plain / "sz300750.csv").write_bytes(payload)
            self.assertEqual(len(load_minute_session(archive, "000001")), 240)
            self.assertEqual(len(load_minute_session(root / "day", "600000")), 240)
            self.assertEqual(len(load_minute_session(plain, "300750")), 240)
            dated = root / "2026" / "01"
            dated.mkdir(parents=True)
            (dated / "20260108").mkdir()
            with zipfile.ZipFile(dated / "20260108.zip", "w") as handle:
                handle.writestr("000001.csv", payload)
            source, kind = locate_day_source(root, "20260108")
            self.assertEqual((source.name, kind), ("20260108.zip", "zip"))

    def test_daily_crosscheck_degraded_metadata_and_capacity_do_not_change_rank(self) -> None:
        from mining.tail_next_morning import capacity_diagnostic, daily_minute_crosscheck, daily_minute_amount_ratio, feature_snapshot, rank_scored_rows

        bars = make_bars()
        self.assertAlmostEqual(daily_minute_amount_ratio(float(bars["amount"].sum()), bars), 1.0)
        self.assertEqual(daily_minute_crosscheck(float(bars["amount"].sum()), bars)["status"], "ready")
        degraded = feature_snapshot("000001", bars, [make_bars() for _ in range(3)], {"listing_age_source": "minute_source_visible_history", "listing_history_sessions": 3})
        self.assertEqual(degraded["feature_reason"], "listing_history_under_20")
        self.assertFalse(degraded["st_filter_applied"])
        self.assertEqual(degraded["st_status"], "unavailable_not_filtered")
        self.assertEqual(degraded["corporate_action_filter"], "unproven_not_applied")

        ranked = rank_scored_rows([{"sec_code": f"6000{index:02d}", "score": 20 - index} for index in range(10)])
        before = [row.copy() for row in ranked]
        result = capacity_diagnostic(ranked)
        self.assertEqual(result["capacity_verdict_5m"], "capacity_unproven")
        self.assertEqual(ranked, before)


MIN_D1_AMOUNT = 500_000_000.0


if __name__ == "__main__":
    unittest.main()
