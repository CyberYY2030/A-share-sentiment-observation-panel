from __future__ import annotations

import unittest

import pandas as pd

from mining.event_activity import build_event_activity
from mining.scanners.launch_burst import evaluate_compression_launch
from mining.selection_context import SelectionContext


def _event_context(*, mode: str = "close_final", activity: bool = True) -> SelectionContext:
    dates = pd.bdate_range("2026-01-02", periods=50).strftime("%Y-%m-%d").tolist()
    rows: list[dict[str, object]] = []
    universe_rows = []
    for code, name, board, launch_pct, current_turnover in (
        ("600001", "MainOverlap", "main", 0.07, 100.0),
        ("300001", "Growth", "gem", 0.08, 160.0),
    ):
        universe_rows.append({"sec_code": code, "sec_name": name, "board": board})
        previous_close = 10.0
        for index, trade_date in enumerate(dates):
            close = 10.0 if index < len(dates) - 1 else round(10.0 * (1.0 + launch_pct), 4)
            turnover = 100.0 if index < 44 else 50.0
            if index == len(dates) - 1:
                turnover = current_turnover
            rows.append(
                {
                    "sec_code": code,
                    "trade_date": trade_date,
                    "open": previous_close if index == len(dates) - 1 else close * 0.999,
                    "high": close * 1.002,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": previous_close,
                    "adj_open": previous_close if index == len(dates) - 1 else close * 0.999,
                    "adj_high": close * 1.002,
                    "adj_low": close * 0.99,
                    "adj_close": close,
                    "change_pct": (close / previous_close - 1.0) * 100.0,
                    "turnover_ratio": turnover if activity else None,
                    "amount": turnover * 1_000_000.0 if activity else None,
                    "volume": 1_000_000.0,
                }
            )
            previous_close = close
    return SelectionContext(
        bars=pd.DataFrame(rows),
        universe=pd.DataFrame(universe_rows),
        trade_date=dates[-1],
        mode=mode,
        as_of=f"{dates[-1]}T14:30:00+08:00",
        price_as_of=dates[-1],
        metadata_as_of=dates[-1],
        trend_profile="P60",
        data_status="ready",
        diagnostics={"clean_dates": dates},
    )


class EventActivityV2Tests(unittest.TestCase):
    def test_compression_launch_uses_t_minus_one_activity_baseline_and_hard_amount_gate(self) -> None:
        context = _event_context()
        activity = build_event_activity(context)
        row = activity.rows.set_index("sec_code").loc["600001"]
        self.assertAlmostEqual(float(row["activity_ratio"]), 1.0)

        rows, diagnostics = evaluate_compression_launch(context)
        self.assertEqual(set(rows["sec_code"]), {"300001"})
        self.assertTrue(rows["event_subtype"].eq("compression_launch").all())
        self.assertTrue(rows["amount_min_diagnostic"].all())
        self.assertEqual(diagnostics["result_count"], 1)

    def test_missing_activity_is_explicit_skip_reason(self) -> None:
        rows, diagnostics = evaluate_compression_launch(_event_context(activity=False))
        self.assertTrue(rows.empty)
        self.assertEqual(diagnostics["skipped_reason_counts"], {"activity_missing": 2})

    def test_intraday_activity_keeps_median_ratio_and_marks_provisional(self) -> None:
        activity = build_event_activity(_event_context(mode="intraday_snapshot"))
        values = activity.rows.set_index("sec_code")
        self.assertAlmostEqual(float(values.at["600001", "activity_ratio"]), 1.0)
        self.assertAlmostEqual(float(values.at["300001", "activity_ratio"]), 1.6)
        self.assertAlmostEqual(float(values.at["600001", "activity_pct"]), 0.5)
        self.assertAlmostEqual(float(values.at["300001", "activity_pct"]), 1.0)
        self.assertTrue(values["activity_provisional"].all())

    def test_snapshot_t_bar_is_included_but_not_in_close_baseline(self) -> None:
        context = _event_context(mode="intraday_snapshot")
        snapshot_date = "2026-03-16"
        current = context.bars[context.bars["trade_date"].eq(context.trade_date)].copy()
        current["trade_date"] = snapshot_date
        current["turnover_ratio"] = [120.0, 180.0]
        context.bars = pd.concat([context.bars, current], ignore_index=True)
        context.trade_date = snapshot_date
        activity = build_event_activity(context).rows.set_index("sec_code")
        self.assertEqual(set(activity.index), {"600001", "300001"})
        self.assertTrue(activity["activity_ratio"].notna().all())

    def test_source_falls_back_per_stock_without_mixing_amount_and_turnover(self) -> None:
        context = _event_context()
        context.bars.loc[
            (context.bars["sec_code"].eq("300001"))
            & (context.bars["trade_date"].eq(context.trade_date)),
            "amount",
        ] = None

        activity = build_event_activity(context).rows

        sources = activity.set_index("sec_code")["activity_source"].to_dict()
        self.assertEqual(sources, {"300001": "turnover_ratio", "600001": "amount"})
