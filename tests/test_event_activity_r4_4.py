from __future__ import annotations

import math
import unittest

import pandas as pd

from mining.event_activity import build_event_activity
from mining.scanners.base_breakout import evaluate_base_breakout
from mining.scanners.launch_burst import evaluate_compression_launch
from mining.scanners.momentum_breakout import evaluate_momentum_anomaly
from mining.selection_context import SelectionContext


def _mixed_context(*, mode: str = "close_final") -> SelectionContext:
    dates = pd.bdate_range("2026-01-05", periods=130).strftime("%Y-%m-%d").tolist()
    codes = ["000001", "000002", "000003", "000004"]
    rows: list[dict[str, object]] = []
    for code_index, code in enumerate(codes):
        for day_index, day in enumerate(dates):
            close = 10.0 + code_index + day_index * 0.01
            pre_close = close - 0.01
            amount = 100_000_000.0 + code_index * 1_000_000.0 + day_index
            turnover = 1.0 + code_index * 0.1 + day_index * 0.001
            if code == "000002" and day == dates[-30]:
                amount = math.nan
            if code == "000003" and day == dates[-10]:
                amount = math.nan
                turnover = math.nan
            if code == "000004" and day == dates[-30]:
                amount = math.nan
                turnover = math.nan
            rows.append(
                {
                    "sec_code": code,
                    "trade_date": day,
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": pre_close,
                    "change_pct": (close / pre_close - 1.0) * 100.0,
                    "adj_open": close * 0.999,
                    "adj_high": close * 1.01,
                    "adj_low": close * 0.99,
                    "adj_close": close,
                    "volume": 1_000_000.0,
                    "amount": amount,
                    "turnover_ratio": turnover,
                }
            )
    universe = pd.DataFrame(
        {
            "sec_code": codes,
            "sec_name": ["A", "B", "C", "D"],
            "board": ["main", "main", "main", "main"],
        }
    )
    return SelectionContext(
        bars=pd.DataFrame(rows),
        universe=universe,
        trade_date=dates[-1],
        mode=mode,
        as_of=dates[-1],
        price_as_of=dates[-1],
        metadata_as_of=dates[-1],
        trend_profile="P60",
        data_status="ready",
        diagnostics={"clean_dates": dates},
    )


class EventActivityR44Tests(unittest.TestCase):
    def test_required_window_must_include_t_and_the_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete baseline"):
            build_event_activity(
                _mixed_context(), baseline_days=20, required_window_days=20
            )

    def test_46_day_source_is_selected_per_stock(self) -> None:
        activity = build_event_activity(
            _mixed_context(), baseline_days=20, required_window_days=46
        )

        sources = activity.rows.set_index("sec_code")["activity_source"].to_dict()
        self.assertEqual(sources, {"000001": "amount", "000002": "turnover_ratio"})
        self.assertEqual(
            activity.skipped_reason_counts,
            {"activity_missing": 1, "activity_window_missing": 1},
        )

    def test_cache_key_separates_21_and_46_day_consumers(self) -> None:
        context = _mixed_context()

        short = build_event_activity(
            context, baseline_days=20, required_window_days=21
        )
        long = build_event_activity(
            context, baseline_days=20, required_window_days=46
        )

        short_sources = short.rows.set_index("sec_code")["activity_source"].to_dict()
        long_sources = long.rows.set_index("sec_code")["activity_source"].to_dict()
        self.assertEqual(
            short_sources,
            {"000001": "amount", "000002": "amount", "000004": "amount"},
        )
        self.assertEqual(long_sources, {"000001": "amount", "000002": "turnover_ratio"})
        self.assertEqual(short.skipped_reason_counts, {"activity_missing": 1})
        self.assertEqual(
            set(context.evaluation_cache),
            {"event_activity:20:21", "event_activity:20:46"},
        )

    def test_zero_nan_inf_and_negative_values_fail_closed_per_stock(self) -> None:
        context = _mixed_context()
        target_date = context.trade_date
        bad_values = {
            "000001": 0.0,
            "000002": math.nan,
            "000003": math.inf,
            "000004": -1.0,
        }
        for code, bad_value in bad_values.items():
            mask = context.bars["sec_code"].eq(code) & context.bars["trade_date"].eq(target_date)
            context.bars.loc[mask, ["amount", "turnover_ratio"]] = bad_value

        activity = build_event_activity(
            context, baseline_days=20, required_window_days=21
        )

        self.assertTrue(activity.rows.empty)
        self.assertEqual(activity.skipped_reason_counts, {"activity_missing": 4})

    def test_suspension_and_new_listing_gaps_do_not_remove_eligible_peers(self) -> None:
        context = _mixed_context()
        current = context.bars["trade_date"].eq(context.trade_date)
        suspended = context.bars["sec_code"].eq("000001") & current
        context.bars.loc[suspended, ["amount", "turnover_ratio"]] = 0.0
        new_listing_old_rows = (
            context.bars["sec_code"].eq("000002")
            & ~context.bars["trade_date"].isin(context.diagnostics["clean_dates"][-10:])
        )
        context.bars = context.bars.loc[~new_listing_old_rows].copy()

        activity = build_event_activity(
            context, baseline_days=20, required_window_days=21
        )

        self.assertEqual(set(activity.rows["sec_code"]), {"000004"})
        self.assertEqual(activity.skipped_reason_counts, {"activity_missing": 3})

    def test_t_is_current_and_t_minus_20_to_t_minus_1_is_the_ratio_baseline(self) -> None:
        context = _mixed_context()
        code = "000001"
        code_rows = context.bars["sec_code"].eq(code)
        ordered_dates = context.diagnostics["clean_dates"]
        context.bars.loc[code_rows, "amount"] = 10.0
        context.bars.loc[
            code_rows & context.bars["trade_date"].eq(ordered_dates[-22]), "amount"
        ] = 10_000.0
        context.bars.loc[
            code_rows & context.bars["trade_date"].eq(ordered_dates[-1]), "amount"
        ] = 210.0

        row = (
            build_event_activity(context, baseline_days=20, required_window_days=46)
            .rows.set_index("sec_code")
            .loc[code]
        )

        self.assertEqual(row["activity_source"], "amount")
        self.assertAlmostEqual(float(row["activity_ratio"]), 21.0)

    def test_scanners_use_mutually_exclusive_builder_owned_missing_counts(self) -> None:
        context = _mixed_context()

        _, compression = evaluate_compression_launch(context)
        _, momentum = evaluate_momentum_anomaly(context)
        _, breakout = evaluate_base_breakout(context)

        compression_skips = compression["skipped_reason_counts"]
        self.assertEqual(compression_skips.get("activity_missing"), 1)
        self.assertEqual(compression_skips.get("activity_window_missing"), 1)
        self.assertEqual(momentum["skipped_reason_counts"].get("activity_missing"), 1)
        self.assertNotIn("activity_window_missing", momentum["skipped_reason_counts"])
        self.assertEqual(breakout["skipped_reason_counts"].get("activity_missing"), 1)
        self.assertNotIn("activity_window_missing", breakout["skipped_reason_counts"])

    def test_cached_results_are_copies_and_intraday_rows_are_provisional(self) -> None:
        context = _mixed_context(mode="intraday_snapshot")
        first = build_event_activity(context, baseline_days=20, required_window_days=21)
        first.rows.loc[:, "activity_ratio"] = -1.0

        second = build_event_activity(context, baseline_days=20, required_window_days=21)

        self.assertTrue(second.rows["activity_ratio"].gt(0).all())
        self.assertTrue(second.rows["activity_provisional"].all())


if __name__ == "__main__":
    unittest.main()
