from __future__ import annotations

import math
import unittest

import pandas as pd

from mining.scanners.base_breakout import evaluate_base_breakout, select_base_breakout_from_context
from mining.selection_context import SelectionContext


def _base_context() -> SelectionContext:
    dates = pd.bdate_range("2025-01-02", periods=145).strftime("%Y-%m-%d").tolist()
    rows: list[dict[str, object]] = []
    for code, name, false_shadow in (("600001", "TrueBreakout", False), ("600002", "FalseShadow", True)):
        closes: list[float] = []
        for index in range(len(dates)):
            if index < 84:
                close = 10.0 + index * 0.06
            elif index < 134:
                close = 15.0 + (0.45 if index % 2 else -0.45)
            elif index < 144:
                close = 15.0 + (0.02 if index % 2 else -0.02)
            else:
                close = 16.4
            closes.append(close)
        for index, trade_date in enumerate(dates):
            close = closes[index]
            pre_close = closes[index - 1] if index else close * 0.99
            high = close * 1.01
            low = close * 0.99
            if index == len(dates) - 1:
                high = 19.0 if false_shadow else close * 1.002
                low = close * 0.98
            rows.append(
                {
                    "sec_code": code,
                    "trade_date": trade_date,
                    "open": pre_close,
                    "high": high,
                    "low": low,
                    "close": close,
                    "pre_close": pre_close,
                    "adj_open": pre_close,
                    "adj_high": high,
                    "adj_low": low,
                    "adj_close": close,
                    "change_pct": (close / pre_close - 1.0) * 100.0,
                    "turnover_ratio": 2.0 + (0.2 if code == "600001" and index == len(dates) - 1 else 0.0),
                    "amount": 100_000_000.0,
                }
            )
    return SelectionContext(
        bars=pd.DataFrame(rows),
        universe=pd.DataFrame(
            [
                {"sec_code": "600001", "sec_name": "TrueBreakout", "board": "main"},
                {"sec_code": "600002", "sec_name": "FalseShadow", "board": "main"},
            ]
        ),
        trade_date=dates[-1],
        mode="close_final",
        as_of=f"{dates[-1]}T15:05:00+08:00",
        price_as_of=dates[-1],
        metadata_as_of=dates[-1],
        trend_profile="P120",
        data_status="ready",
        diagnostics={"clean_dates": dates},
    )


class BaseBreakoutTests(unittest.TestCase):
    def test_t_is_excluded_from_box_and_true_breakout_passes(self) -> None:
        rows, diagnostics = evaluate_base_breakout(_base_context())
        self.assertEqual(rows["sec_code"].tolist(), ["600001"])
        self.assertGreater(float(rows.iloc[0]["breakout_pct"]), 0.005)
        self.assertEqual(diagnostics["result_count"], 1)

    def test_long_upper_shadow_confirmation_fails_without_dividing_by_zero(self) -> None:
        rows, diagnostics = evaluate_base_breakout(_base_context())
        self.assertNotIn("600002", set(rows["sec_code"]))
        self.assertGreaterEqual(diagnostics["skipped_reason_counts"]["base_breakout_gate_failed"], 1)

    def test_output_order_is_the_documented_sort_order(self) -> None:
        context = _base_context()
        extra = context.bars[context.bars["sec_code"].eq("600001")].copy()
        extra["sec_code"] = "600003"
        extra.loc[extra.index[-1], "turnover_ratio"] = 5.0
        context.bars = pd.concat([context.bars, extra], ignore_index=True)
        context.universe = pd.concat(
            [context.universe, pd.DataFrame([{"sec_code": "600003", "sec_name": "Second", "board": "main"}])],
            ignore_index=True,
        )
        rows, _ = evaluate_base_breakout(context)
        self.assertEqual(rows["sec_code"].tolist(), ["600003", "600001"])
        candidates = select_base_breakout_from_context(context)
        self.assertEqual([candidate.rank for candidate in candidates], [1, 2])

    def test_change_pct_null_does_not_block_adjusted_price_breakout(self) -> None:
        context = _base_context()
        context.bars["change_pct"] = pd.NA
        rows, diagnostics = evaluate_base_breakout(context)
        self.assertEqual(rows["sec_code"].tolist(), ["600001"])
        self.assertEqual(diagnostics["result_count"], 1)
