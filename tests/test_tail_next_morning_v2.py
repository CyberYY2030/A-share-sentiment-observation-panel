"""Focused TNM V2 fixed-canary rule tests; no minute-source access."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mining.tail_next_morning import SESSION_LABELS
from mining.tail_next_morning_v2 import (
    MIN_D1_AMOUNT,
    _spec_hash,
    _v2_statistics_from_frame,
    build_signal_row,
    outcome_from_frames,
    rank_channels,
)


LISTED = {
    "listing_history_count_status": "at_least_threshold",
    "listing_history_sessions": 20,
    "listing_age_source": "daily_date_column",
}


def stat(*, close=10.0, high=10.5, low=9.5, amount=100_000_000.0, full_amount=300_000_000.0, open_=10.0) -> dict:
    return {
        "status": "ready",
        "history": {"status": "ready", "close": close, "high": high, "low": low, "full_amount": full_amount},
        "signal": {
            "status": "ready", "open": open_, "close": close, "high": high, "low": low,
            "amount": amount, "volume": amount / ((close + open_) / 2),
            "range1450": high / low - 1.0, "tail_return": 0.01, "tail_location": 0.7,
        },
        "buy": {"status": "ready", "vwap": close, "amount": 1_000.0, "volume": 100.0},
    }


def a_inputs(*, d1_amount=300_000_000.0) -> tuple[dict, list[dict]]:
    prior = [stat(close=10.0, high=10.6, low=9.4, amount=100_000_000.0, full_amount=300_000_000.0) for _ in range(10)]
    prior[-1]["history"]["full_amount"] = d1_amount
    day = stat(close=10.5, high=10.6, low=10.0, amount=200_000_000.0, open_=10.0)
    return day, prior


def b_inputs(*, d1_amount=300_000_000.0) -> tuple[dict, list[dict]]:
    prior = [stat(close=10.0, high=12.0, low=9.6, amount=100_000_000.0, full_amount=300_000_000.0) for _ in range(10)]
    prior[-1]["history"].update({"close": 10.8, "full_amount": d1_amount})
    day = stat(close=11.0, high=11.2, low=10.0, amount=50_000_000.0, open_=10.5)
    return day, prior


def raw_frame(*, close=10.0) -> pd.DataFrame:
    times = [f"2024-08-13 {label}:00" for label in SESSION_LABELS]
    return pd.DataFrame({
        "time": times,
        "open": [close] * 240, "high": [close * 1.01] * 240,
        "low": [close * .99] * 240, "close": [close] * 240,
        "volume": [100.0] * 240, "amount": [close * 100.0] * 240,
    })


class TailNextMorningV2Tests(unittest.TestCase):
    def test_a_shape_and_strict_d1_liquidity(self) -> None:
        day, prior = a_inputs(d1_amount=MIN_D1_AMOUNT)
        row = build_signal_row("300001", day, prior, LISTED)
        self.assertTrue(row["a_shape_pass"])
        self.assertFalse(row["liquidity_pass"])
        self.assertFalse(row["eligible_pass"])
        day, prior = a_inputs(d1_amount=MIN_D1_AMOUNT + 1)
        self.assertTrue(build_signal_row("300001", day, prior, LISTED)["eligible_pass"])

    def test_a_ret_tail_diagnostics_and_ma_gate(self) -> None:
        day, prior = a_inputs()
        baseline = build_signal_row("300004", day, prior, LISTED)
        altered = copy.deepcopy(day)
        altered["signal"].update({"tail_return": -0.25, "tail_location": 0.0})
        diagnostic_only = build_signal_row("300004", altered, prior, LISTED)
        self.assertTrue(baseline["a_shape_pass"])
        self.assertEqual(
            {key: baseline[key] for key in ("eligible_pass", "ret1450", "activity", "position1450")},
            {key: diagnostic_only[key] for key in ("eligible_pass", "ret1450", "activity", "position1450")},
        )
        day["signal"]["close"] = 10.39
        self.assertIn("ret1450<0.04", build_signal_row("300004", day, prior, LISTED)["a_failure_reasons"])
        day, prior = a_inputs()
        for value in prior:
            value["history"]["close"] = 11.0
        low_ma = build_signal_row("300004", day, prior, LISTED)
        self.assertIn("close1450<=prev_ma10", low_ma["a_failure_reasons"])
        self.assertIn("close1450<=prev_ma10", low_ma["b_failure_reasons"])

    def test_b_shape_and_its_individual_gates(self) -> None:
        day, prior = b_inputs()
        row = build_signal_row("300002", day, prior, LISTED)
        self.assertTrue(row["b_shape_pass"])
        day["signal"]["close"] = 10.4
        self.assertFalse(build_signal_row("300002", day, prior, LISTED)["b_shape_pass"])

    def test_b_open_runup_drawdown_and_contraction_gates(self) -> None:
        day, prior = b_inputs()
        day["signal"]["open"] = 11.0
        self.assertIn("close1450<=max(open_D,prev_close)", build_signal_row("300005", day, prior, LISTED)["b_failure_reasons"])
        day, prior = b_inputs()
        for value in prior:
            value["history"]["high"] = 11.0
        self.assertIn("prior10_runup<0.15", build_signal_row("300005", day, prior, LISTED)["b_failure_reasons"])
        day, prior = b_inputs()
        day["signal"]["close"] = 11.8
        self.assertIn("drawdown_outside_-0.15_to_-0.03", build_signal_row("300005", day, prior, LISTED)["b_failure_reasons"])
        day, prior = b_inputs()
        day["signal"]["amount"] = 90_000_000.0
        day["signal"]["volume"] = day["signal"]["amount"] / 10.75
        self.assertIn("pullback_volume_ratio>0.80", build_signal_row("300005", day, prior, LISTED)["b_failure_reasons"])

    def test_shape_precedes_liquidity_and_listing(self) -> None:
        day, prior = b_inputs(d1_amount=MIN_D1_AMOUNT)
        row = build_signal_row("300003", day, prior, {"listing_history_count_status": "exact_below_threshold", "listing_history_sessions": 10})
        self.assertTrue(row["b_shape_pass"])
        self.assertFalse(row["liquidity_pass"])
        self.assertFalse(row["listing_pass"])

    def test_a_and_b_pools_do_not_cross(self) -> None:
        day, prior = a_inputs()
        a = build_signal_row("300010", day, prior, LISTED)
        a.update({"b_shape_pass": True, "shape_pass": True})
        day, prior = b_inputs()
        b = build_signal_row("300011", day, prior, LISTED)
        ranked = rank_channels([a, b])
        self.assertEqual({row["sec_code"] for row in ranked["a_pool"]}, {"300010"})
        self.assertEqual({row["sec_code"] for row in ranked["b_pool"]}, {"300011"})

    def test_code_cannot_break_economic_boundary_tie(self) -> None:
        rows = []
        for code in ("300099", "300001", "300050", "300002"):
            day, prior = a_inputs()
            rows.append(build_signal_row(code, day, prior, LISTED))
        ranked = rank_channels(rows)
        self.assertEqual(4, len(ranked["a_selected"]))
        self.assertTrue(ranked["a_boundary_tie_expanded"])
        self.assertEqual(["300001", "300002", "300050", "300099"], [row["sec_code"] for row in ranked["a_selected"]])

    def test_empty_pool_is_not_filled(self) -> None:
        day, prior = a_inputs(d1_amount=MIN_D1_AMOUNT)
        ranked = rank_channels([build_signal_row("300070", day, prior, LISTED)])
        self.assertEqual([], ranked["a_selected"])
        self.assertEqual([], ranked["b_selected"])

    def test_b_pool_rank_is_available_without_economic_selection(self) -> None:
        rows = []
        for code, close in (("300101", 11.0), ("300102", 10.95), ("300103", 10.9)):
            day, prior = b_inputs()
            day["signal"]["close"] = close
            rows.append(build_signal_row(code, day, prior, LISTED))
        ranked = rank_channels(rows)
        self.assertEqual(3, len(ranked["b_pool"]))
        self.assertTrue(all("pool_rank" in row for row in ranked["b_pool"]))

    def test_post_1450_values_do_not_change_signal_statistics(self) -> None:
        baseline = raw_frame()
        future = baseline.copy()
        future.loc[230:, ["open", "high", "low", "close", "amount", "volume"]] = [0.0, 0.0, 0.0, 0.0, float("nan"), 0.0]
        before = _v2_statistics_from_frame(baseline)
        after = _v2_statistics_from_frame(future)
        self.assertEqual(before["signal"], after["signal"])
        self.assertNotEqual(before["history"]["status"], after["history"]["status"])

    def test_pre_1450_invalid_value_fails_closed(self) -> None:
        frame = raw_frame()
        frame.loc[229, "close"] = 0.0
        self.assertEqual("invalid", _v2_statistics_from_frame(frame)["signal"]["status"])

    def test_next_day_outcomes_are_separate_from_selection(self) -> None:
        day, prior = a_inputs()
        row = build_signal_row("300201", day, prior, LISTED)
        selection = {key: row[key] for key in ("eligible_pass", "ret1450", "position1450", "activity")}
        next_bars = raw_frame(close=10.5)
        next_bars.loc[:29, "high"] = 11.0
        next_bars.loc[:29, "low"] = 10.0
        outcome = outcome_from_frames(day, next_bars)
        self.assertEqual("ready", outcome["outcome_status"])
        self.assertAlmostEqual(11 / 10.5 - 1, outcome["intraday_mfe_0930_1000"])
        self.assertEqual(selection, {key: row[key] for key in selection})

    def test_future_changes_do_not_change_rank_or_membership(self) -> None:
        day_one, prior_one = a_inputs()
        day_two, prior_two = a_inputs()
        day_two["signal"]["close"] = 10.6
        rows = [build_signal_row("300206", day_one, prior_one, LISTED), build_signal_row("300207", day_two, prior_two, LISTED)]
        before = rank_channels(rows)
        selected_before = [row["sec_code"] for row in before["a_selected"]]
        future = raw_frame(close=10.5)
        future.loc[230:, ["open", "high", "low", "close", "amount", "volume"]] = [0.0, 0.0, 0.0, 0.0, float("nan"), 0.0]
        self.assertEqual("invalid", _v2_statistics_from_frame(future)["history"]["status"])
        after = rank_channels(rows)
        self.assertEqual(selected_before, [row["sec_code"] for row in after["a_selected"]])

    def test_fixed_exit_invalid_does_not_rewrite_selection(self) -> None:
        day, prior = a_inputs()
        row = build_signal_row("300202", day, prior, LISTED)
        next_bars = raw_frame(close=10.5)
        next_bars.loc[30:34, "volume"] = 0.0
        outcome = outcome_from_frames(day, next_bars)
        self.assertEqual("unavailable_fixed_exit", outcome["outcome_status"])
        self.assertTrue(row["eligible_pass"])

    def test_labels_distinguish_open_mfe_gap_and_fixed_exit(self) -> None:
        day, _ = a_inputs()
        next_bars = raw_frame(close=10.5)
        next_bars.loc[:29, "high"] = 11.0
        next_bars.loc[:29, "low"] = 9.5
        outcome = outcome_from_frames(day, next_bars)
        self.assertAlmostEqual(11 / 10.5 - 1, outcome["intraday_mfe_0930_1000"])
        self.assertAlmostEqual(10.5 / day["buy"]["vwap"] - 1, outcome["gap_return"])
        self.assertAlmostEqual(outcome["gross_fixed_exit"] - .003, outcome["net30"])

    def test_spec_hash_excludes_execution_results(self) -> None:
        original = _spec_hash()
        from mining import tail_next_morning_v2 as module
        card = module.TASK_CARD
        text = card.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "card.md"
            copied.write_text(text + "\nTNM-V2-1 test evidence\n", encoding="utf-8")
            original_card = module.TASK_CARD
            module.TASK_CARD = copied
            try:
                self.assertEqual(original, _spec_hash())
                copied.write_text(text.replace("ret1450", "ret1450_changed", 1), encoding="utf-8")
                self.assertNotEqual(original, _spec_hash())
            finally:
                module.TASK_CARD = original_card

    def test_non_a_share_is_never_economic_candidate(self) -> None:
        day, prior = a_inputs()
        row = build_signal_row("600001", day, prior, LISTED)
        # Main-board A shares are allowed; a non-A prefix is not.
        self.assertTrue(row["common_quality_pass"])
        row = build_signal_row("200001", day, prior, LISTED)
        self.assertFalse(row["eligible_pass"])

    def test_explicit_st_and_corporate_action_degradation(self) -> None:
        day, prior = a_inputs()
        row = build_signal_row("300301", day, prior, LISTED)
        self.assertFalse(row["st_filter_applied"])
        self.assertEqual("unavailable_not_filtered", row["st_status"])
        self.assertEqual("unproven_not_applied", row["corporate_action_filter"])


if __name__ == "__main__":
    unittest.main()
