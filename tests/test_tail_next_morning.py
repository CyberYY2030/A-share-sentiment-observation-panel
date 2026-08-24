from __future__ import annotations

import io
import json
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

    def _preflight_row(self, *, mutate=None, omit_next_day_code: bool = False, truncate_next_day_code: bool = False, include_preflight: bool = False):
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
        if include_preflight:
            return rows["000001"], ranking, preflight
        return rows["000001"], ranking

    @staticmethod
    def _feature_signature(row: dict[str, object]) -> dict[str, object]:
        return {
            key: row[key]
            for key in ("feature_status", "feature_reason", "raw_features", "features", "d1_amount")
        }

    @staticmethod
    def _write_development_days(root: Path, dates: list[str], codes: tuple[str, ...]) -> None:
        payload = make_bars().to_csv(index=False).encode("utf-8")
        for trade_date in dates:
            archive_path = root / trade_date[:4] / trade_date[4:6] / f"{trade_date}.zip"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "w") as archive:
                for code in codes:
                    archive.writestr(f"sz/{code}.csv", payload)
                archive.writestr("bj/920001.csv", payload)

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

    def test_preflight_amount_crosscheck_rejects_future_nan_and_inf_without_changing_signal(self) -> None:
        baseline, baseline_rank = self._preflight_row()
        baseline_feature = self._feature_signature(baseline)
        baseline_outcome = baseline["outcome"]
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                def mutate(bars, date, *, changed_value=value):
                    if date == "20260108":
                        bars.loc[236, "amount"] = changed_value

                row, ranking, preflight = self._preflight_row(mutate=mutate, include_preflight=True)
                self.assertEqual(self._feature_signature(row), baseline_feature)
                self.assertEqual(ranking, baseline_rank)
                self.assertEqual(row["outcome"], baseline_outcome)
                self.assertEqual(row["daily_minute_amount_crosscheck"]["status"], "invalid")
                self.assertEqual(row["daily_minute_amount_crosscheck"]["reason"], "minute_amount_non_finite")
                self.assertIn("daily_minute_amount_invalid:000001:minute_amount_non_finite", preflight["sample_errors"])

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
        self.assertEqual(daily_minute_crosscheck(None, bars)["status"], "unavailable")
        bars.loc[236, "amount"] = float("nan")
        self.assertIsNone(daily_minute_amount_ratio(2_400_000_000.0, bars))
        self.assertEqual(daily_minute_crosscheck(2_400_000_000.0, bars)["status"], "invalid")
        negative = make_bars()
        negative.loc[236, "amount"] = -1.0
        self.assertEqual(daily_minute_crosscheck(2_400_000_000.0, negative)["reason"], "minute_amount_negative")
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

    def test_development_statistics_match_tnm1_raw_formula_and_year_guard(self) -> None:
        from mining.tail_next_morning import (
            TailDataError,
            feature_snapshot,
            feature_snapshot_from_statistics,
            minute_sufficient_statistics,
            outcome_from_statistics,
            outcome_snapshot,
        )

        day = make_bars()
        prior = [make_bars() for _ in range(3)]
        from_statistics = feature_snapshot_from_statistics(
            "000001", minute_sufficient_statistics(day), [minute_sufficient_statistics(value) for value in prior], listing()
        )
        self.assertEqual(from_statistics, feature_snapshot("000001", day, prior, listing()))
        expected_outcome = outcome_snapshot(day, make_bars())
        actual_outcome = outcome_from_statistics(minute_sufficient_statistics(day), minute_sufficient_statistics(make_bars()), "20230109")
        for field in ("outcome_status", "gross_return", "net_return_15bps", "net_return_30bps", "mfe_0930_1000", "mae_0930_1000"):
            self.assertEqual(actual_outcome[field], expected_outcome[field])
        with tempfile.TemporaryDirectory() as tmp:
            from mining.tail_next_morning import load_day_statistics

            with self.assertRaisesRegex(TailDataError, "development_year_guard:20250102"):
                load_day_statistics(tmp, "20250102", allowed_years=("2023",))

    def test_dplus1_diagnostic_and_sell_quality_are_independent(self) -> None:
        from mining.tail_next_morning import minute_sufficient_statistics, outcome_from_statistics

        day = minute_sufficient_statistics(make_bars())
        diagnostic_bad = make_bars()
        diagnostic_bad.loc[5, "close"] = 0.0
        completed = outcome_from_statistics(day, minute_sufficient_statistics(diagnostic_bad), "20230109")
        self.assertEqual(completed["outcome_status"], "ready")
        self.assertIsNone(completed["mfe_0930_1000"])
        self.assertIsNone(completed["mae_0930_1000"])

        sell_bad = make_bars()
        sell_bad.loc[30, "close"] = 0.0
        delayed = outcome_from_statistics(day, minute_sufficient_statistics(sell_bad), "20230109")
        self.assertEqual(delayed["outcome_status"], "delayed_exit_required")

    def test_listing_threshold_short_circuits_after_twenty_valid_dates(self) -> None:
        from mining.tail_next_morning import development_listing_evidence

        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily"
            daily.mkdir()
            values = list(pd.date_range("2022-01-01", periods=20, freq="D"))
            values.extend(["not-a-date", *pd.date_range("2024-01-01", periods=500, freq="D")])
            pd.DataFrame({"date": values}).to_excel(daily / "000001.xlsx", index=False)
            cache: dict[str, dict[str, object]] = {}
            evidence = development_listing_evidence(daily, "000001", "20230106", 0, cache)
            self.assertEqual(evidence["listing_age_source"], "daily_k_date_threshold_proof")
            self.assertEqual(evidence["listing_history_sessions_capped"], 20)
            self.assertEqual(evidence["listing_history_count_status"], "at_least_threshold")
            self.assertTrue(evidence["listing_date_short_circuited"])
            self.assertEqual(evidence["listing_date_rows_scanned"], 20)
            self.assertIsNone(cache["000001"]["all_dates"])

    def test_listing_threshold_is_unordered_deduplicated_and_exact_below_threshold(self) -> None:
        from mining.tail_next_morning import development_listing_evidence, feature_snapshot_from_statistics, minute_sufficient_statistics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "daily"
            daily.mkdir()
            unordered = ["bad", "2023-03-20", "2023-01-01", "2023-01-01", None, "2023-02-01", "2023-01-15", "2023-03-20"]
            pd.DataFrame({"date": unordered}).to_excel(daily / "000001.xlsx", index=False)
            evidence = development_listing_evidence(daily, "000001", "20230401", 1, {})
            self.assertEqual(evidence["listing_history_sessions_capped"], 4)
            self.assertEqual(evidence["listing_history_count_status"], "exact_below_threshold")
            self.assertFalse(evidence["listing_date_short_circuited"])

            sparse_sessions = pd.date_range("2023-01-01", periods=10, freq="10D")
            pd.DataFrame({"date": sparse_sessions}).to_excel(daily / "600000.xlsx", index=False)
            sparse = development_listing_evidence(daily, "600000", "20230430", 99, {})
            self.assertEqual(sparse["listing_history_sessions"], 10)
            self.assertEqual(sparse["listing_history_count_status"], "exact_below_threshold")
            isolated = feature_snapshot_from_statistics(
                "600000",
                minute_sufficient_statistics(make_bars()),
                [minute_sufficient_statistics(make_bars()) for _ in range(3)],
                sparse,
            )
            self.assertEqual(isolated["feature_reason"], "listing_history_under_20")

    def test_listing_below_threshold_cache_recomputes_for_later_target(self) -> None:
        from mining.tail_next_morning import development_listing_evidence

        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily"
            daily.mkdir()
            initial = list(pd.date_range("2023-01-01", periods=10, freq="D"))
            later = list(pd.date_range("2023-03-01", periods=10, freq="D"))
            pd.DataFrame({"date": initial + later}).to_excel(daily / "000001.xlsx", index=False)
            cache: dict[str, dict[str, object]] = {}
            early = development_listing_evidence(daily, "000001", "20230201", 0, cache)
            self.assertEqual((early["listing_history_sessions_capped"], early["listing_history_count_status"]), (10, "exact_below_threshold"))
            later_evidence = development_listing_evidence(daily, "000001", "20230401", 0, cache)
            self.assertEqual((later_evidence["listing_history_sessions_capped"], later_evidence["listing_history_count_status"]), (20, "at_least_threshold"))
            self.assertEqual(later_evidence["listing_date_rows_scanned"], 0)

    def test_development_loader_opens_container_once_and_keeps_minute_universe_without_daily_k(self) -> None:
        from mining.tail_next_morning import development_listing_evidence, feature_snapshot_from_statistics, load_day_statistics, minute_sufficient_statistics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_development_days(root, ["20230103"], ("000001", "600000"))
            with mock.patch("mining.tail_next_morning.zipfile.ZipFile", wraps=zipfile.ZipFile) as opened:
                statistics, record = load_day_statistics(root, "20230103", allowed_years=("2023",))
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(record["container_open_count"], 1)
            self.assertEqual(set(statistics), {"000001", "600000"})
            directory = root / "2023" / "01" / "20230104" / "sh"
            directory.mkdir(parents=True)
            (directory / "600000.csv").write_bytes(make_bars().to_csv(index=False).encode("utf-8"))
            directory_statistics, directory_record = load_day_statistics(root, "20230104", allowed_years=("2023",))
            self.assertEqual((directory_record["source_kind"], set(directory_statistics)), ("directory", {"600000"}))
            with zipfile.ZipFile(root / "2023" / "01" / "20230104.zip", "w") as archive:
                archive.writestr("sz/000001.csv", make_bars().to_csv(index=False).encode("utf-8"))
            zip_statistics, zip_record = load_day_statistics(root, "20230104", allowed_years=("2023",))
            self.assertEqual((zip_record["source_kind"], set(zip_statistics)), ("zip", {"000001"}))
            evidence = development_listing_evidence(root / "daily_missing", "600000", "20230106", 20, {})
            self.assertEqual(evidence["listing_age_source"], "minute_source_visible_history")
            self.assertEqual((evidence["listing_history_sessions_capped"], evidence["listing_history_count_status"]), (20, "at_least_threshold"))
            ready = feature_snapshot_from_statistics(
                "600000", minute_sufficient_statistics(make_bars()), [minute_sufficient_statistics(make_bars()) for _ in range(3)], evidence
            )
            self.assertEqual(ready["feature_status"], "ready")

    def test_fixed_slots_random_sha_and_2023_only_group_order(self) -> None:
        from mining.tail_next_morning import _fixed_ten_slots, _random_top10, select_development_features

        one = {"sec_code": "000001", "outcome": {"outcome_status": "cash_unfilled_buy"}}
        slots = _fixed_ten_slots([one])
        self.assertEqual(len(slots["slots"]), 10)
        self.assertEqual(slots["net30"], 0.0)
        random_rows = [{"sec_code": f"6000{index:02d}"} for index in range(12)]
        expected = sorted(
            random_rows,
            key=lambda row: (__import__("hashlib").sha256(f"20260824|20230106|{row['sec_code']}".encode()).hexdigest(), row["sec_code"]),
        )[:10]
        self.assertEqual([row["sec_code"] for row in _random_top10(random_rows, "20230106")], [row["sec_code"] for row in expected])

        def rows_for_year(year: str) -> list[dict[str, object]]:
            rows = []
            for index in range(10):
                net = 0.0
                if index == 9:
                    net = 0.03 if year == "2023" else 0.001
                if index == 8:
                    net = 0.01 if year == "2023" else 0.99
                percentiles = {
                    "tail_return_rel": (index + 1) / 10,
                    "tail_end_location": 1.0 if index == 8 else (index + 1) / 20,
                    "tail_amount_accel": (index + 1) / 10,
                    "pre_tail_return_rel": (index + 1) / 10,
                    "activity_ratio": (index + 1) / 10,
                    "recent_3date_return_rel": (index + 1) / 10,
                }
                rows.append({"sec_code": f"6000{index:02d}", "feature_status": "ready", "feature_percentiles": percentiles, "outcome": {"outcome_status": "ready", "net_return_30bps": net}})
            return rows

        selection = select_development_features({"20230106": rows_for_year("2023"), "20240105": rows_for_year("2024")})
        tail_price = next(row for row in selection["selected_features"] if row["group"] == "tail_price")
        self.assertEqual(tail_price["feature"], "tail_return_rel")

    def test_delayed_exit_grid_year_boundary_and_unresolved_block(self) -> None:
        from mining.tail_next_morning import _blocked_outcome, minute_sufficient_statistics, outcome_from_statistics, resolve_delayed_exit

        day = make_bars()
        next_day = make_bars()
        next_day.loc[30:34, ["amount", "volume"]] = 0.0
        delayed = outcome_from_statistics(minute_sufficient_statistics(day), minute_sufficient_statistics(next_day), "20231229")
        self.assertEqual(delayed["outcome_status"], "delayed_exit_required")
        resolved = resolve_delayed_exit(delayed, minute_sufficient_statistics(next_day), "20231229", "20231228", first_delayed_day=True)
        self.assertEqual(resolved["outcome_status"], "ready")
        next_day.loc[35:, ["amount", "volume"]] = 0.0
        still_delayed = outcome_from_statistics(minute_sufficient_statistics(day), minute_sufficient_statistics(next_day), "20231229")
        later = resolve_delayed_exit(still_delayed, minute_sufficient_statistics(make_bars()), "20231230", "20231228", first_delayed_day=False)
        self.assertEqual(later["outcome_status"], "ready")
        self.assertIsNone(resolve_delayed_exit(delayed, minute_sufficient_statistics(next_day), "20240102", "20231228", first_delayed_day=False))
        self.assertEqual(_blocked_outcome(delayed)["outcome_status"], "unresolved_exit_at_phase_end")

    def test_checkpoint_hash_conflict_and_canary_no_frozen_rule(self) -> None:
        from mining.tail_next_morning import TailDataError, _prepare_development_run, run_dev_preflight

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "minute_containers": [{"trade_date": "20230103", "path": "a", "source_kind": "zip", "size_bytes": 1, "mtime_ns": 1, "member_count": 1, "central_directory_digest": "a"}],
                "daily_k_root": {"root": "daily", "candidate_count": 0, "total_bytes": 0, "tree_digest": "b", "files": []},
            }
            _prepare_development_run(root, mode="dev-preflight", input_manifest=manifest, resume_run_id="resume-target")
            changed = {**manifest, "minute_containers": [{**manifest["minute_containers"][0], "size_bytes": 2}]}
            with self.assertRaisesRegex(TailDataError, "resume_hash_mismatch"):
                _prepare_development_run(root, mode="dev-preflight", input_manifest=changed, resume_run_id="resume-target")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = [f"202301{day:02d}" for day in range(3, 17)]
            codes = tuple(f"600{index:03d}" for index in range(12))
            self._write_development_days(root, dates, codes)
            with mock.patch("mining.tail_next_morning.development_listing_evidence", return_value=listing()):
                result, run_dir = run_dev_preflight(root, root / "daily", root / "out")
            self.assertEqual(len(result["target_dates"]), 10)
            self.assertFalse((run_dir / "frozen_rule.json").exists())
            self.assertEqual(json.loads((run_dir / "completion.json").read_text(encoding="utf-8"))["status"], "SUCCEEDED")

    def test_input_identity_lock_takeover_and_incremental_resume(self) -> None:
        from mining.tail_next_morning import (
            TailDataError,
            _acquire_run_lock,
            _input_manifest,
            _prepare_development_run,
            load_day_statistics,
            run_dev_preflight,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = [f"202301{day:02d}" for day in range(3, 17)]
            codes = tuple(f"600{index:03d}" for index in range(12))
            self._write_development_days(root, dates, codes)
            daily = root / "daily"
            daily.mkdir()
            pd.DataFrame({"date": ["2022-01-01"]}).to_excel(daily / "600000.xlsx", index=False)
            identity = _input_manifest(root, daily, [dates[0]], ("2023",))
            container = identity["minute_containers"][0]
            self.assertIn("central_directory_digest", container)
            self.assertEqual(container["member_count"], 13)
            self.assertIn("tree_digest", identity["daily_k_root"])
            directory_source = root / "2023" / "01" / "20230117" / "sh"
            directory_source.mkdir(parents=True)
            (directory_source / "600000.csv").write_bytes(make_bars().to_csv(index=False).encode("utf-8"))
            directory_identity = _input_manifest(root, daily, ["20230117"], ("2023",))["minute_containers"][0]
            self.assertEqual(directory_identity["source_kind"], "directory")
            self.assertEqual(directory_identity["candidate_count"], 1)
            self.assertIn("tree_digest", directory_identity)

            lock_dir, lock_manifest = _prepare_development_run(root / "locks", mode="dev-preflight", input_manifest=identity)
            stale = {"pid": 99999999, "host": __import__("socket").gethostname(), "run_id": lock_manifest["run_id"], "run_hash": lock_manifest["run_hash"], "started_at": "old"}
            (lock_dir / "run.lock").write_text(json.dumps(stale), encoding="utf-8")
            with self.assertRaisesRegex(TailDataError, "single_writer_lock_exists"):
                _acquire_run_lock(lock_dir, lock_manifest, explicit_resume=False)
            lock = _acquire_run_lock(lock_dir, lock_manifest, explicit_resume=True)
            self.assertTrue(any((lock_dir / "lock_evidence").iterdir()))
            lock.unlink()

            real_load = load_day_statistics
            first_calls: list[str] = []

            def interrupting_load(*args, **kwargs):
                first_calls.append(str(args[1]))
                if str(args[1]) == "20230108":
                    raise KeyboardInterrupt()
                return real_load(*args, **kwargs)

            with mock.patch("mining.tail_next_morning.development_listing_evidence", return_value=listing()), mock.patch(
                "mining.tail_next_morning.load_day_statistics", side_effect=interrupting_load
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_dev_preflight(root, daily, root / "out")
            run_id = next((root / "out").iterdir()).name
            self.assertEqual(json.loads(((root / "out" / run_id) / "completion.json").read_text(encoding="utf-8"))["status"], "CANCELLED")

            resumed_calls: list[str] = []

            def recording_load(*args, **kwargs):
                resumed_calls.append(str(args[1]))
                return real_load(*args, **kwargs)

            with mock.patch("mining.tail_next_morning.development_listing_evidence", return_value=listing()), mock.patch(
                "mining.tail_next_morning.load_day_statistics", side_effect=recording_load
            ):
                result, run_dir = run_dev_preflight(root, daily, root / "out", resume_run_id=run_id)
            self.assertEqual(result["selection_status"], "canary_probe_not_frozen")
            self.assertEqual(resumed_calls[0], "20230108")
            self.assertNotIn("20230103", resumed_calls)
            self.assertTrue((run_dir / "sha256_manifest.json").exists())

    def test_full_development_freeze_requires_positive_both_years_and_complete_contract(self) -> None:
        from mining.tail_next_morning import _finalize_economic_outputs

        def rows_for_year(year: str, offset: float) -> list[dict[str, object]]:
            rows = []
            for index in range(10):
                value = offset + index * 0.001
                percentiles = {
                    "tail_return_rel": (index + 1) / 10,
                    "tail_end_location": (index + 1) / 10,
                    "tail_amount_accel": (index + 1) / 10,
                    "pre_tail_return_rel": (index + 1) / 10,
                    "activity_ratio": (index + 1) / 10,
                    "recent_3date_return_rel": (index + 1) / 10,
                }
                rows.append(
                    {
                        "sec_code": f"600{index:03d}",
                        "feature_status": "ready",
                        "feature_percentiles": percentiles,
                        "features": percentiles,
                        "outcome": {
                            "outcome_status": "ready",
                            "gross_return": value + 0.003,
                            "net_return_15bps": value + 0.0015,
                            "net_return_30bps": value,
                            "buy": {"status": "ready", "amount": 10_000_000.0},
                            "sell": {"status": "ready", "amount": 10_000_000.0},
                            "exit_date": f"{year}0109",
                        },
                    }
                )
            return rows

        manifest = {"spec_hash": "spec", "runner_source_hash": "runner", "input_manifest_hash": "input", "run_id": "run", "run_hash": "hash"}
        with tempfile.TemporaryDirectory() as tmp:
            ready_rows = {"20230106": rows_for_year("2023", 0.001), "20240105": rows_for_year("2024", 0.001)}
            economic, _ = _finalize_economic_outputs(Path(tmp), mode="dev-run", canary=False, target_dates=sorted(ready_rows), daily_rows=ready_rows, run_manifest=manifest, approved_commit="approved")
            self.assertEqual(economic["selection_status"], "ready")
            rule = json.loads((Path(tmp) / "frozen_rule.json").read_text(encoding="utf-8"))
            self.assertIn("execution_contract", rule)
            self.assertEqual(rule["binding"]["approved_commit"], "approved")

            blocked_rows = {"20230106": rows_for_year("2023", 0.001), "20240105": rows_for_year("2024", -0.020)}
            economic, _ = _finalize_economic_outputs(Path(tmp), mode="dev-run", canary=False, target_dates=sorted(blocked_rows), daily_rows=blocked_rows, run_manifest=manifest, approved_commit="approved")
            self.assertEqual(economic["selection_status"], "no_stable_development_signal")
            self.assertFalse((Path(tmp) / "frozen_rule.json").exists())

    def test_dev_run_requires_explicit_approved_commit(self) -> None:
        from mining.tail_next_morning import TailDataError, _validate_approved_development_commit

        with self.assertRaisesRegex(TailDataError, "dev_run_requires_approved_commit"):
            _validate_approved_development_commit(None)

    def test_spec_hash_excludes_execution_results_but_covers_frozen_definition(self) -> None:
        from mining.tail_next_morning import _frozen_contract_hash

        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "card.md"
            card.write_bytes("# contract\r\n## 4. frozen rule\r\nvalue=one\r\n## 11. 执行结果\r\ninitial\r\n".encode("utf-8"))
            baseline = _frozen_contract_hash(card)
            with card.open("ab") as handle:
                handle.write("TNM-2A evidence\r\n".encode("utf-8"))
            self.assertEqual(_frozen_contract_hash(card), baseline)
            card.write_bytes(card.read_bytes().replace(b"value=one", b"value=two"))
            self.assertNotEqual(_frozen_contract_hash(card), baseline)


MIN_D1_AMOUNT = 500_000_000.0


if __name__ == "__main__":
    unittest.main()
