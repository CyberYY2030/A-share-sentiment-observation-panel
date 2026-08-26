"""Focused TNM V2 fixed-canary rule tests; no minute-source access."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

import pandas as pd

from mining.tail_next_morning import SESSION_LABELS, canonical_code, is_a_share_code, locate_day_source
from mining.tail_next_morning_v2 import (
    A_RET1450_MIN,
    MIN_D1_AMOUNT,
    _checkpoint,
    _content_identity,
    _gate_counts,
    _prepare_canary_run,
    _spec_hash,
    _standard_limit_evidence,
    _unit_minute_members,
    _unit_consumed_input,
    _verify_checkpoint_consumed_input,
    _v2_statistics_from_frame,
    build_signal_row,
    load_day_v2_statistics,
    necessary_preselection,
    outcome_from_frames,
    outcome_from_statistics,
    rank_channels,
    run_canary,
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
    prior = [stat(close=10.0, high=10.1, low=9.9, amount=100_000_000.0, full_amount=300_000_000.0) for _ in range(10)]
    prior[-1]["history"]["full_amount"] = d1_amount
    day = stat(close=10.5, high=10.9, low=10.0, amount=200_000_000.0, open_=10.0)
    return day, prior


def b_inputs(*, d1_amount=300_000_000.0) -> tuple[dict, list[dict]]:
    prior = [stat(close=10.0, high=12.0, low=9.6, amount=100_000_000.0, full_amount=300_000_000.0) for _ in range(10)]
    for value in prior:
        value["signal"].update({"high": 10.2, "low": 9.8, "range1450": 10.2 / 9.8 - 1.0})
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
    def test_standard_limit_fen_touch_and_post_1450_isolation(self) -> None:
        self.assertEqual(11.0, _standard_limit_evidence("600001", 10.0, 10.995)["limit_up_price"])
        self.assertTrue(_standard_limit_evidence("600001", 10.0, 10.995)["tail_limit_touch"])
        self.assertEqual(12.0, _standard_limit_evidence("300001", 10.0, 11.995)["limit_up_price"])
        self.assertFalse(_standard_limit_evidence("300001", 10.0, 11.994)["tail_limit_touch"])
        before = _v2_statistics_from_frame(raw_frame(close=10.0))
        future = raw_frame(close=10.0)
        future.loc[230, "high"] = 20.0
        after = _v2_statistics_from_frame(future)
        self.assertEqual(before["signal"], after["signal"])

    def test_tail_boundary_and_new_a_gates_and_score(self) -> None:
        day, prior = a_inputs()
        row = build_signal_row("300801", day, prior, LISTED)
        self.assertTrue(row["a_shape_pass"])
        exact = copy.deepcopy(day)
        exact["signal"]["tail_return"] = -0.03
        self.assertTrue(build_signal_row("300801", exact, prior, LISTED)["a_shape_pass"])
        below = copy.deepcopy(exact)
        below["signal"]["tail_return"] = math.nextafter(-.03, -math.inf)
        self.assertIn("tail_return<-0.03", build_signal_row("300801", below, prior, LISTED)["a_failure_reasons"])
        for key, value, reason in (("open", 10.5, "close1450<=open_D"), ("range1450", .01, "range_expansion<1.50")):
            altered = copy.deepcopy(day)
            altered["signal"][key] = value
            self.assertIn(reason, build_signal_row("300801", altered, prior, LISTED)["a_failure_reasons"])
        no_structure = copy.deepcopy(day)
        no_structure["signal"]["close"] = 10.4
        no_structure["signal"]["high"] = 10.9
        no_structure_prior = copy.deepcopy(prior)
        for value in no_structure_prior:
            value["history"]["high"] = 10.5
        self.assertIn("trend_structure_failed", build_signal_row("300801", no_structure, no_structure_prior, LISTED)["a_failure_reasons"])
        rows = [build_signal_row(f"3008{index:02d}", *a_inputs(), LISTED) for index in range(3)]
        rows[1]["position1450"], rows[1]["vwap_dist"] = .99, .99
        ranked = rank_channels(rows)
        self.assertEqual(ranked["a_pool"][0]["score_A"], ranked["a_pool"][1]["score_A"])

    def test_b_new_gates_pool_top10_and_no_code_economic_tie(self) -> None:
        day, prior = b_inputs()
        self.assertTrue(build_signal_row("300901", day, prior, LISTED)["b_shape_pass"])
        for key, value, reason in (("open", 10.95, "body1450<0.01"), ("range1450", .01, "range_expansion<1.00")):
            altered = copy.deepcopy(day)
            altered["signal"][key] = value
            self.assertIn(reason, build_signal_row("300901", altered, prior, LISTED)["b_failure_reasons"])
        rows = [build_signal_row(f"3009{index:02d}", *b_inputs(), LISTED) for index in range(12)]
        ranked = rank_channels(rows, b_limit=10)
        self.assertEqual(12, len(ranked["b_pool"]))
        self.assertEqual(12, len(ranked["b_selected"]))
        self.assertTrue(ranked["b_boundary_tie_expanded"])

    def test_outcome_windows_degrade_independently(self) -> None:
        day, _ = a_inputs()
        next_stat = {
            "morning_label": {"status": "invalid"},
            "fixed_sell": {"status": "ready", "vwap": 11.0},
        }
        morning_bad = outcome_from_statistics(day, next_stat)
        self.assertEqual("unavailable_morning_label", morning_bad["outcome_status"])
        self.assertAlmostEqual(11.0 / day["buy"]["vwap"] - 1, morning_bad["gross_fixed_exit"])
        next_stat = {
            "morning_label": {"status": "ready", "next_open": 10.5, "high": 11.0, "low": 10.0},
            "fixed_sell": {"status": "invalid_window"},
        }
        sell_bad = outcome_from_statistics(day, next_stat)
        self.assertEqual("unavailable_fixed_exit", sell_bad["outcome_status"])
        self.assertIsNotNone(sell_bad["intraday_mfe_0930_1000"])

    def test_consumed_content_identity_changes_with_member_bytes(self) -> None:
        before = _content_identity({"minute_members": {"sha256": hashlib.sha256(b"before").hexdigest()}})
        after = _content_identity({"minute_members": {"sha256": hashlib.sha256(b"after").hexdigest()}})
        self.assertNotEqual(before, after)

    def test_checkpoint_content_identity_rejects_equal_size_member_and_daily_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            minute_root, daily_root = root / "minute", root / "daily"
            day_dir = minute_root / "2024" / "08" / "20240813"
            day_dir.mkdir(parents=True)
            member = day_dir / "sz300001.csv"
            raw_frame().to_csv(member, index=False)
            daily_root.mkdir()
            workbook = daily_root / "300001.xlsx"
            workbook.write_bytes(b"daily-A")
            _, record = load_day_v2_statistics(minute_root, "20240813", {"300001"})
            expected = _unit_consumed_input({"20240813": record["consumed_member_sha256"]}, {"300001": {"path": str(workbook), "sha256": hashlib.sha256(b"daily-A").hexdigest()}})
            result = {"unit_consumed_input": expected}
            self.assertEqual(expected["unit_consumed_input_identity"], _verify_checkpoint_consumed_input("market:20240813", result, {"20240813": record}))
            member_stat, directory_stat = member.stat(), day_dir.stat()
            member.write_bytes(member.read_bytes().replace(b"10.0", b"11.0", 1))
            os.utime(member, ns=(member_stat.st_atime_ns, member_stat.st_mtime_ns))
            os.utime(day_dir, ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns))
            _, rewritten_record = load_day_v2_statistics(minute_root, "20240813", {"300001"})
            with self.assertRaisesRegex(Exception, "consumed_input_identity_mismatch:market:20240813"):
                _verify_checkpoint_consumed_input("market:20240813", result, {"20240813": rewritten_record})
            workbook_stat = workbook.stat()
            workbook.write_bytes(b"daily-B")
            os.utime(workbook, ns=(workbook_stat.st_atime_ns, workbook_stat.st_mtime_ns))
            with self.assertRaisesRegex(Exception, "consumed_input_identity_mismatch:market:20240813"):
                _verify_checkpoint_consumed_input("market:20240813", result, {"20240813": record})

    def test_missing_selective_member_is_stable_checkpoint_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            minute_root = root / "minute"
            day_dir = minute_root / "2024" / "08" / "20240813"
            day_dir.mkdir(parents=True)
            raw_frame().to_csv(day_dir / "sz300001.csv", index=False)
            requested = {"300001", "300002", "300003"}
            (day_dir / "sz300003.csv").write_text("unparseable", encoding="utf-8")
            _, record = load_day_v2_statistics(minute_root, "20240813", requested)
            minute_members = _unit_minute_members({"20240813": record}, {"20240813": requested})
            self.assertEqual("missing:300002", minute_members["20240813"]["300002"])
            self.assertEqual("missing:300003", minute_members["20240813"]["300003"])
            result = {"unit_consumed_input": _unit_consumed_input(minute_members, {})}
            _checkpoint(root, "run", "fixture:20240813|300002", result)
            saved = json.loads((root / "checkpoints" / "fixture_20240813_300002.json").read_text(encoding="utf-8"))
            self.assertEqual(
                result["unit_consumed_input"]["unit_consumed_input_identity"],
                _verify_checkpoint_consumed_input(saved["unit"], saved["result"], {"20240813": record}),
            )
            raw_frame(close=11).to_csv(day_dir / "sz300002.csv", index=False)
            _, appeared_record = load_day_v2_statistics(minute_root, "20240813", requested)
            with self.assertRaisesRegex(Exception, "consumed_input_identity_mismatch:fixture:20240813\\|300002"):
                _verify_checkpoint_consumed_input(saved["unit"], saved["result"], {"20240813": appeared_record})

    def test_runtime_gate_counts_are_overlapping_and_first_failures_exclusive(self) -> None:
        good_day, good_prior = a_inputs()
        liquidity_day, liquidity_prior = a_inputs(d1_amount=MIN_D1_AMOUNT)
        failed_day, failed_prior = a_inputs()
        failed_day["signal"].update({"tail_return": -0.04, "range1450": .01})
        rows = [
            build_signal_row("300701", good_day, good_prior, LISTED),
            build_signal_row("300702", liquidity_day, liquidity_prior, LISTED),
            build_signal_row("300703", failed_day, failed_prior, LISTED),
        ]
        ranked = rank_channels(rows)
        counts = _gate_counts(rows, ranked, {"universe": 10, "survivors": 3})
        self.assertEqual(3, counts["evaluated_final_gate_rows"])
        self.assertEqual(3, counts["first_failure_total"])
        self.assertEqual(1, counts["A_eligible"])
        self.assertEqual(1, counts["predicate_fail_counts"]["d1_liquidity_failed"])
        self.assertEqual(1, counts["predicate_fail_counts"]["A"]["tail_return<-0.03"])
        self.assertEqual(1, counts["predicate_fail_counts"]["A"]["range_expansion<1.50"])

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
        self.assertTrue(baseline["eligible_pass"])
        self.assertFalse(diagnostic_only["eligible_pass"])
        self.assertIn("tail_return<-0.03", diagnostic_only["a_failure_reasons"])
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
        ranked = rank_channels(rows, a_limit=3)
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
        ranked = rank_channels(rows, b_limit=2)
        self.assertEqual(3, len(ranked["b_pool"]))
        self.assertTrue(all("pool_rank" in row for row in ranked["b_pool"]))

    def test_b_top2_structure_has_only_valid_monotone_members(self) -> None:
        rows = []
        for index, close in enumerate((11.0, 10.98, 10.96, 10.94)):
            day, prior = b_inputs()
            day["signal"]["close"] = close
            day["signal"]["high"] = 11.2
            rows.append(build_signal_row(f"30012{index}", day, prior, LISTED))
        ranked = rank_channels(rows, b_limit=2)
        members = ranked["b_selected"]
        self.assertLessEqual(len(members), 2)
        self.assertTrue(all(row["b_shape_pass"] and row["eligible_pass"] and math.isfinite(row["score_B"]) for row in members))
        self.assertEqual(sorted(row["pool_rank"] for row in members), [row["pool_rank"] for row in members])

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
            copied.write_text(text + "\nTNM-V2-R1 test evidence\n", encoding="utf-8")
            original_card = module.TASK_CARD
            module.TASK_CARD = copied
            try:
                self.assertEqual(original, _spec_hash())
                copied.write_text(text.replace("checkpoint", "checkpoint_changed", 1), encoding="utf-8")
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

    def test_preselection_thresholds_invalid_inputs_and_no_false_negative(self) -> None:
        day, prior = a_inputs()
        d1 = prior[-1]
        exact = copy.deepcopy(day)
        exact["signal"]["close"] = d1["history"]["close"] * (1 + A_RET1450_MIN)
        exact["signal"]["high"] = exact["signal"]["close"] + .1
        self.assertTrue(necessary_preselection("300401", exact, d1)["a_local_necessary"])
        below = copy.deepcopy(exact)
        below["signal"]["close"] = math.nextafter(exact["signal"]["close"], -math.inf)
        self.assertFalse(necessary_preselection("300401", below, d1)["a_local_necessary"])
        above = copy.deepcopy(exact)
        above["signal"]["close"] = math.nextafter(exact["signal"]["close"], math.inf)
        self.assertTrue(necessary_preselection("300401", above, d1)["a_local_necessary"])
        nan_day = copy.deepcopy(day)
        nan_day["signal"]["amount"] = float("nan")
        self.assertFalse(necessary_preselection("300401", nan_day, d1)["survives"])
        zero_range = copy.deepcopy(day)
        zero_range["signal"]["high"] = zero_range["signal"]["low"]
        self.assertFalse(necessary_preselection("300401", zero_range, d1)["survives"])
        self.assertFalse(necessary_preselection("300401", day, None)["survives"])

    def test_300x12_preselection_is_economically_equivalent(self) -> None:
        generator = random.Random(20260825)
        full_rows, selected_rows = [], []
        for index in range(300):
            code = f"300{index:03d}"
            if index % 3 == 0:
                day, prior = a_inputs()
                day["signal"]["close"] += generator.random() * .03
                day["signal"]["high"] = day["signal"]["close"] + .1
            elif index % 3 == 1:
                day, prior = b_inputs()
                day["signal"]["close"] -= generator.random() * .02
            else:
                day, prior = a_inputs(d1_amount=MIN_D1_AMOUNT)
                day["signal"]["close"] = 10.1
            row = build_signal_row(code, day, prior, LISTED)
            full_rows.append(row)
            if necessary_preselection(code, day, prior[-1])["survives"]:
                selected_rows.append(build_signal_row(code, day, prior, LISTED))
        full_ranked, selective_ranked = rank_channels(full_rows), rank_channels(selected_rows)
        self.assertEqual(
            {row["sec_code"] for row in full_rows if row["eligible_pass"]},
            {row["sec_code"] for row in selected_rows if row["eligible_pass"]},
        )
        for channel in ("a_pool", "b_pool", "a_selected", "b_selected"):
            self.assertEqual(
                [(row["sec_code"], row.get("channel"), row.get("score_A"), row.get("score_B"), row.get("pool_rank")) for row in full_ranked[channel]],
                [(row["sec_code"], row.get("channel"), row.get("score_A"), row.get("score_B"), row.get("pool_rank")) for row in selective_ranked[channel]],
            )

    def test_selective_loader_opens_one_container_and_matches_small_full_universe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "minute"
            day_dir = root / "2024" / "08" / "20240813"
            day_dir.mkdir(parents=True)
            raw_frame().to_csv(day_dir / "sz300501.csv", index=False)
            raw_frame(close=11).to_csv(day_dir / "sz300502.csv", index=False)
            full, full_record = load_day_v2_statistics(root, "20240813")
            selective, selective_record = load_day_v2_statistics(root, "20240813", {"300501", "300502"})
            self.assertEqual(1, full_record["container_open_count"])
            self.assertEqual(1, selective_record["container_open_count"])
            self.assertEqual(full, selective)

    def test_manifest_checkpoint_and_identity_resume_guard(self) -> None:
        manifest = {"minute_containers": [], "daily_root": "synthetic", "targets": [], "market_targets": []}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run_dir, run_manifest = _prepare_canary_run(base, manifest, resume_run_id=None)
            self.assertTrue((run_dir / "run_manifest.json").exists())
            _checkpoint(run_dir, run_manifest["run_hash"], "fixture:20240813|300328", {"ok": True})
            saved = (run_dir / "checkpoints" / "fixture_20240813_300328.json").read_text(encoding="utf-8")
            self.assertIn(run_manifest["run_hash"], saved)
            resumed, _ = _prepare_canary_run(base, manifest, resume_run_id=run_manifest["run_id"])
            self.assertEqual(run_dir, resumed)
            with self.assertRaisesRegex(Exception, "resume_identity_mismatch"):
                _prepare_canary_run(base, manifest, resume_run_id="canary-old-identity")

    def test_shared_dplus1_and_history_container_is_opened_once(self) -> None:
        from mining import tail_next_morning_v2 as module
        calendar = [f"2024F{index:04d}" for index in range(60)]
        positions = {"20240813": 10, "20240826": 22, "20240827": 34, "20240923": 46, "20240926": 49}
        for target, index in positions.items():
            calendar[index] = target
        calendar[47] = "20240924"  # 20240923 D+1 and 20240926 D-2
        calendar[48] = "20240925"
        calls: dict[str, int] = {}

        def fake_load(_root, trade_date, codes=None):
            calls[trade_date] = calls.get(trade_date, 0) + 1
            requested = {"300085", "300339", "300972", "300328"} if codes is None else set(codes)
            digests = {code: hashlib.sha256(f"{trade_date}|{code}".encode()).hexdigest() for code in requested}
            return {code: copy.deepcopy(stat()) for code in requested}, {"trade_date": trade_date, "container_open_count": 1, "invalid_file_count": 0, "consumed_member_sha256": digests}

        manifest = {"minute_containers": [], "daily_root": "synthetic", "targets": list(positions), "market_targets": ["20240923", "20240926"]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(module, "_calendar_for_targets", return_value=(calendar, positions)), patch.object(module, "_canary_input_manifest", return_value=manifest), patch.object(module, "load_day_v2_statistics", side_effect=fake_load), patch.object(module, "development_listing_evidence", return_value=LISTED):
            summary, run_dir = run_canary("synthetic", "synthetic", temporary)
            with patch.object(module, "rank_channels", side_effect=AssertionError("SUCCEEDED fast path recalculated rankings")):
                resumed, resumed_dir = run_canary("synthetic", "synthetic", temporary, resume_run_id=run_dir.name)
            self.assertEqual(summary, resumed)
            self.assertEqual(run_dir, resumed_dir)
        self.assertEqual(2, calls["20240924"])

    @unittest.skipUnless(Path(r"E:\分钟数据").exists(), "real minute source unavailable")
    def test_fixed_64_code_real_small_universe_full_vs_selective(self) -> None:
        source, kind = locate_day_source(r"E:\分钟数据", "20240923")
        available: dict[str, tuple[str, bytes]] = {}
        if kind == "zip":
            with zipfile.ZipFile(source) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(".csv"):
                        code = canonical_code(Path(name).stem)
                        if is_a_share_code(code):
                            available.setdefault(code, (Path(name).name, archive.read(name)))
        else:
            for item in source.rglob("*.csv"):
                code = canonical_code(item.stem)
                if is_a_share_code(code):
                    available.setdefault(code, (item.name, item.read_bytes()))
        must = {"300085", "300339", "300972", "300328"}
        self.assertTrue(must.issubset(available))
        others = sorted((code for code in available if code not in must), key=lambda code: hashlib.sha256(f"TNM-V2-1F|{code}".encode()).hexdigest())[: 64 - len(must)]
        codes = sorted(must | set(others))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "minute"
            day_dir = root / "2024" / "09" / "20240923"
            day_dir.mkdir(parents=True)
            for code in codes:
                name, payload = available[code]
                (day_dir / name).write_bytes(payload)
            full, _ = load_day_v2_statistics(root, "20240923")
            selective, _ = load_day_v2_statistics(root, "20240923", codes)
            self.assertEqual(full, selective)
            full_rows = [build_signal_row(code, full[code], [full[code]] * 10, LISTED) for code in codes]
            selective_rows = [build_signal_row(code, selective[code], [selective[code]] * 10, LISTED) for code in codes if necessary_preselection(code, selective[code], selective[code])["survives"]]
            full_ranked, selective_ranked = rank_channels(full_rows), rank_channels(selective_rows)
            self.assertEqual([row["sec_code"] for row in full_ranked["a_selected"]], [row["sec_code"] for row in selective_ranked["a_selected"]])
            self.assertEqual([row["sec_code"] for row in full_ranked["b_selected"]], [row["sec_code"] for row in selective_ranked["b_selected"]])


if __name__ == "__main__":
    unittest.main()
