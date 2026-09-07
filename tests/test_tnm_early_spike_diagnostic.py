from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from mining.tnm_early_spike_diagnostic import (
    _common_complete_dates,
    _finalize_manifest,
    _fit_score,
    _gate_summary,
    _load_requested_frames,
    _rolling_folds,
    _select_top_n,
    _spearman,
    _verify_frozen_run_inputs,
    label_early_spike,
)
from mining.tail_next_morning import TailDataError


def minute_frame(price: float = 10.0, volume: float = 100.0) -> pd.DataFrame:
    values = np.full(240, price, dtype=float)
    volumes = np.full(240, volume, dtype=float)
    return pd.DataFrame({
        "open": values.copy(), "high": values.copy(), "low": values.copy(), "close": values.copy(),
        "amount": values * volumes, "volume": volumes,
    })


def set_vwap(frame: pd.DataFrame, start: int, end: int, price: float, volume: float = 100.0) -> None:
    frame.loc[start:end - 1, ["open", "high", "low", "close"]] = price
    frame.loc[start:end - 1, "volume"] = volume
    frame.loc[start:end - 1, "amount"] = price * volume


class EarlySpikeLabelTests(unittest.TestCase):
    def test_exact_boundary_gap_and_vwap_hit(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        set_vwap(next_day, 0, 5, 10.3)
        result = label_early_spike(day, next_day)
        self.assertTrue(result["gap_hit3"])
        self.assertTrue(result["high_touch3"])
        self.assertTrue(result["vwap5_hit3"])
        self.assertEqual(0, result["first_vwap5_hit_block"])

    def test_high_touch_uses_only_next_completed_block_for_exit(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        next_day.loc[0, "high"] = 10.4
        set_vwap(next_day, 5, 10, 10.1)
        result = label_early_spike(day, next_day)
        self.assertTrue(result["high_touch3"])
        self.assertFalse(result["vwap5_hit3"])
        self.assertEqual("09:35-09:40", result["causal_exit_window"])
        self.assertAlmostEqual(10.1, result["causal_exit_vwap"])
        self.assertAlmostEqual(.007, result["net30"])

    def test_last_block_trigger_exits_at_1030(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        next_day.loc[59, "high"] = 10.4
        set_vwap(next_day, 60, 65, 10.2)
        result = label_early_spike(day, next_day)
        self.assertEqual(11, result["first_high_touch_block"])
        self.assertEqual("10:30-10:35", result["causal_exit_window"])
        self.assertAlmostEqual(10.2, result["causal_exit_vwap"])

    def test_failure_exits_at_1030(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        set_vwap(next_day, 60, 65, 9.8)
        result = label_early_spike(day, next_day)
        self.assertFalse(result["high_touch3"])
        self.assertEqual("10:30-10:35", result["causal_exit_window"])
        self.assertAlmostEqual(-.023, result["net30"])

    def test_zero_volume_buy_is_unavailable(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        day.loc[231:235, ["volume", "amount"]] = 0.0
        result = label_early_spike(day, next_day)
        self.assertEqual("zero_volume_window", result["buy_status"])
        self.assertEqual("unavailable_buy", result["label_status"])
        self.assertIsNone(result["high_touch3"])

    def test_zero_volume_exit_is_not_filled_from_same_block(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        next_day.loc[0, "high"] = 10.4
        next_day.loc[5:9, ["volume", "amount"]] = 0.0
        result = label_early_spike(day, next_day)
        self.assertEqual("09:35-09:40", result["causal_exit_window"])
        self.assertEqual("unavailable_exit:zero_volume_window", result["label_status"])
        self.assertIsNone(result["net30"])

    def test_missing_next_day_preserves_buy(self) -> None:
        result = label_early_spike(minute_frame(), None)
        self.assertEqual("ready", result["buy_status"])
        self.assertEqual("unavailable_next_day", result["label_status"])
        self.assertIsNone(result["gap_hit3"])

    def test_invalid_first_hour_isolated(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        next_day.loc[30, "high"] = np.nan
        result = label_early_spike(day, next_day)
        self.assertTrue(result["label_status"].startswith("invalid_first_hour:"))
        self.assertIsNone(result["high_touch3"])

    def test_future_d_rows_do_not_change_frozen_label(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        next_day.loc[20, "high"] = 10.4
        before = label_early_spike(day, next_day)
        mutated = day.copy()
        mutated.loc[236:239, ["open", "high", "low", "close", "amount"]] *= 7
        after = label_early_spike(mutated, next_day)
        self.assertEqual(before, after)

    def test_future_leakage_red_light_then_guard(self) -> None:
        day, next_day = minute_frame(), minute_frame()
        baseline = label_early_spike(day, next_day)
        future_mutated = next_day.copy()
        future_mutated.loc[100:110, ["open", "high", "low", "close", "amount"]] *= 2
        guarded = label_early_spike(day, future_mutated)
        leaky_before = float(next_day.iloc[100:111]["close"].mean())
        leaky_after = float(future_mutated.iloc[100:111]["close"].mean())
        self.assertEqual(baseline, guarded)
        # The deliberately leaky fingerprint changes, while the production label stays fixed.
        self.assertNotEqual(leaky_before, leaky_after)


class EarlySpikeAnalysisTests(unittest.TestCase):
    def test_constant_daily_label_has_no_ic(self) -> None:
        self.assertIsNone(_spearman(pd.Series([1.0, 2.0]), pd.Series([0.0, 0.0])))

    def test_rolling_folds_require_six_complete_calendar_months(self) -> None:
        dates = [value.strftime("%Y%m%d") for value in pd.bdate_range("2023-01-17", "2024-12-31")]
        folds = _rolling_folds(dates)
        self.assertEqual("202309-202310", folds[0][0])
        self.assertGreaterEqual(pd.Timestamp(folds[0][2][0]), pd.Timestamp(dates[0]) + pd.DateOffset(months=6))
        self.assertEqual(8, len(folds))

    def test_fit_preprocessing_is_train_only(self) -> None:
        train = pd.DataFrame({"feature": [0.0, 1.0, 2.0, 3.0], "label": [0, 0, 1, 1]})
        one = pd.DataFrame({"feature": [1.5]})
        with_hostile_peer = pd.DataFrame({"feature": [1.5, 1_000_000.0]})
        score_one = _fit_score(train, one, ("feature",), "label")[0]
        score_with_peer = _fit_score(train, with_hostile_peer, ("feature",), "label")[0]
        self.assertAlmostEqual(score_one, score_with_peer)

    def test_same_n_selection_and_common_complete_days(self) -> None:
        frame = pd.DataFrame({
            "trade_date": ["20240102"] * 4 + ["20240103"] * 4,
            "sec_code": [f"00000{i}" for i in range(8)],
            "gap_hit3": [False, True, False, True] * 2,
            "net30": [.01] * 8,
        })
        _, left = _select_top_n(frame, np.arange(8, dtype=float), "gap_hit3", top_n=2)
        _, right = _select_top_n(frame, np.arange(8, dtype=float)[::-1], "gap_hit3", top_n=2)
        self.assertEqual({"20240102": 2, "20240103": 2}, left.groupby("trade_date").size().to_dict())
        right.loc[right["trade_date"] == "20240102", "net30"] = np.nan
        self.assertEqual({"20240103"}, _common_complete_dates({"left": left, "right": right}, top_n=2))

    def test_gate_uses_daily_equal_net_and_fold_daily_ic_direction(self) -> None:
        folds = pd.DataFrame([
            {"fold": "f1", "label": "gap_hit3", "model": "base_all", "direction": 1, "daily_net30": .09},
            {"fold": "f2", "label": "gap_hit3", "model": "base_all", "direction": 1, "daily_net30": .01},
            {"fold": "f1", "label": "gap_hit3", "model": "control_ret1450", "direction": 1, "daily_net30": .02},
            {"fold": "f2", "label": "gap_hit3", "model": "control_ret1450", "direction": 1, "daily_net30": .01},
        ])
        daily = pd.DataFrame([
            {"fold": "f1", "trade_date": "d1", "label": "gap_hit3", "model": "base_all", "common_complete_day": True, "top_n_net30": .09, "daily_ic": .2},
            {"fold": "f2", "trade_date": "d2", "label": "gap_hit3", "model": "base_all", "common_complete_day": True, "top_n_net30": 0.0, "daily_ic": .1},
            {"fold": "f2", "trade_date": "d3", "label": "gap_hit3", "model": "base_all", "common_complete_day": True, "top_n_net30": 0.0, "daily_ic": .1},
            {"fold": "f1", "trade_date": "d1", "label": "gap_hit3", "model": "control_ret1450", "common_complete_day": True, "top_n_net30": .02, "daily_ic": .1},
            {"fold": "f2", "trade_date": "d2", "label": "gap_hit3", "model": "control_ret1450", "common_complete_day": True, "top_n_net30": .01, "daily_ic": .1},
            {"fold": "f2", "trade_date": "d3", "label": "gap_hit3", "model": "control_ret1450", "common_complete_day": True, "top_n_net30": .01, "daily_ic": .1},
        ])
        gate = _gate_summary(folds, daily, "gap_hit3")
        self.assertAlmostEqual(.03, gate["daily_net30"])
        self.assertEqual(1.0, gate["direction_consistency"])
        self.assertTrue(gate["passes"])

    def test_manifest_verifier_checks_hash_size_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            checkpoint = run / "checkpoints" / "market_20240102.json"
            checkpoint.parent.mkdir()
            checkpoint.write_text('{"a":1}', encoding="utf-8")
            relative = checkpoint.relative_to(run).as_posix()
            evidence = {relative: {"size_bytes": checkpoint.stat().st_size, "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}}
            (run / "artifact_manifest.json").write_text(json.dumps(evidence), encoding="utf-8")
            self.assertEqual(1, _verify_frozen_run_inputs(run, expected_count=1)["checkpoint_count"])
            checkpoint.write_text('{"a":2}', encoding="utf-8")
            with self.assertRaisesRegex(TailDataError, "source_checkpoint_sha256_mismatch"):
                _verify_frozen_run_inputs(run, expected_count=1)

    def test_year_guard_runs_before_source_access(self) -> None:
        with self.assertRaisesRegex(TailDataError, "diagnostic_year_guard:20250102"):
            _load_requested_frames(Path("does-not-exist"), "20250102", [])

    def test_final_manifest_covers_root_evidence_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            (root / "daily_results.csv").write_text("a\n1\n", encoding="utf-8")
            (root / "future_leakage_red.log").write_text("FAIL\n", encoding="utf-8")
            _finalize_manifest(root)
            artifacts = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["artifacts"]
            self.assertEqual({"daily_results.csv", "future_leakage_red.log"}, set(artifacts))


if __name__ == "__main__":
    unittest.main()
