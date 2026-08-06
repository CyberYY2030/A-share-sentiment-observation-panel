from __future__ import annotations

import unittest

import pandas as pd

from mining.scanners.counter_trend_rs import (
    PRIMARY_BENCHMARK,
    SENSITIVITY_BENCHMARKS,
    evaluate_counter_trend_rs,
)
from mining.selection_context import SelectionContext


def _rs_context() -> tuple[SelectionContext, pd.Series, dict[str, pd.Series]]:
    dates = pd.bdate_range("2025-01-02", periods=145).strftime("%Y-%m-%d").tolist()
    benchmark_values: list[float] = []
    value = 1000.0
    for index in range(len(dates)):
        if index >= len(dates) - 21:
            value *= 0.992
        benchmark_values.append(value)
    rows: list[dict[str, object]] = []
    for code, name, daily_return in (("600001", "AbsoluteUp", 0.006), ("600002", "OnlyLessDown", -0.0025)):
        close = 10.0
        for index, trade_date in enumerate(dates):
            pre_close = close
            if index:
                close = close * ((1.0 + daily_return) if index >= len(dates) - 21 else 1.004)
            rows.append(
                {
                    "sec_code": code,
                    "trade_date": trade_date,
                    "adj_close": close,
                    "change_pct": (close / pre_close - 1.0) * 100.0 if index else 0.0,
                }
            )
    context = SelectionContext(
        bars=pd.DataFrame(rows),
        universe=pd.DataFrame(
            [
                {"sec_code": "600001", "sec_name": "AbsoluteUp", "board": "main"},
                {"sec_code": "600002", "sec_name": "OnlyLessDown", "board": "main"},
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
    primary = pd.Series(benchmark_values, index=dates)
    sensitivity = {code: primary * multiplier for code, multiplier in zip(SENSITIVITY_BENCHMARKS, (1.0, 1.1, 0.9), strict=True)}
    return context, primary, sensitivity


class CounterTrendRsTests(unittest.TestCase):
    def test_fixed_weak_primary_accepts_absolute_up_and_reports_sensitivities(self) -> None:
        context, primary, sensitivity = _rs_context()
        rows, diagnostics = evaluate_counter_trend_rs(
            context,
            primary_benchmark=primary,
            sensitivity_benchmarks=sensitivity,
        )
        self.assertEqual(rows["sec_code"].tolist(), ["600001"])
        self.assertGreater(float(rows.iloc[0]["rs_breakout_pct"]), 0.001)
        self.assertEqual(rows.iloc[0]["primary_benchmark"], PRIMARY_BENCHMARK)
        self.assertEqual(set(rows.iloc[0]["sensitivity_returns_20"]), set(SENSITIVITY_BENCHMARKS))
        self.assertLess(diagnostics["primary_benchmark_return_20"], 0)

    def test_primary_benchmark_up_means_empty_board(self) -> None:
        context, primary, sensitivity = _rs_context()
        rising = primary.copy()
        rising.iloc[-21:] = pd.Series(range(1000, 1021), index=rising.index[-21:])
        rows, diagnostics = evaluate_counter_trend_rs(context, primary_benchmark=rising, sensitivity_benchmarks=sensitivity)
        self.assertTrue(rows.empty)
        self.assertEqual(diagnostics["skipped_reason_counts"], {"primary_benchmark_not_weak": 2})

    def test_stock_that_only_falls_less_is_rejected(self) -> None:
        context, primary, sensitivity = _rs_context()
        rows, diagnostics = evaluate_counter_trend_rs(context, primary_benchmark=primary, sensitivity_benchmarks=sensitivity)
        self.assertNotIn("600002", set(rows["sec_code"]))
        self.assertEqual(diagnostics["skipped_reason_counts"]["stock_not_absolutely_up"], 1)

    def test_missing_primary_benchmark_day_is_not_forward_filled(self) -> None:
        context, primary, sensitivity = _rs_context()
        missing = primary.drop(primary.index[-10])
        rows, diagnostics = evaluate_counter_trend_rs(context, primary_benchmark=missing, sensitivity_benchmarks=sensitivity)
        self.assertTrue(rows.empty)
        self.assertEqual(diagnostics["skipped_reason_counts"], {"primary_benchmark_missing_or_invalid": 2})
