from __future__ import annotations

import unittest

import pandas as pd

from mining.scanners import get_registered_scanners
from mining.scanners.strong_trend import _a_tier_history, evaluate_strong_trend, select_from_context
from mining.selection_context import SelectionContext


class StrongTrendTests(unittest.TestCase):
    def _context(self) -> tuple[SelectionContext, pd.Series]:
        dates = pd.date_range("2025-01-01", periods=80, freq="B").strftime("%Y-%m-%d").tolist()
        rows: list[dict[str, object]] = []
        codes = ["600001", "300996", "600002", "600003", "600004", "600005", "600006", "600007", "600008", "600009"]
        for code in codes:
            closes: list[float] = []
            for index in range(len(dates)):
                if code == "600001":
                    close = 10.0 * (1.005 ** index)
                elif code == "300996":
                    close = 15.0 * (1.005 ** index)
                else:
                    close = 10.0 * (0.998 ** index)
                closes.append(close)
            for index, trade_date in enumerate(dates):
                close = closes[index]
                pre_close = closes[index - 1] if index else close
                rows.append(
                    {
                        "sec_code": code,
                        "trade_date": trade_date,
                        "adj_open": pre_close,
                        "adj_high": close * 1.01,
                        "adj_low": close * 0.99,
                        "adj_close": close,
                        "close": close,
                        "pre_close": pre_close,
                        "row_status": "confirmed_halt" if code == "300996" and 10 <= index <= 24 else "valid_trade",
                        "amount": (
                            3_000_000_000.0
                            if code == "300996" and index == len(dates) - 1
                            else 1_000_000_000.0 + (100_000_000.0 if code in {"600001", "300996"} else 0.0)
                        ),
                        "volume": 1_000_000.0,
                    }
                )
        universe = pd.DataFrame(
            [{"sec_code": code, "sec_name": code} for code in codes]
        )
        benchmark = pd.Series(
            [-0.01 if index % 7 == 0 else 0.001 for index in range(len(dates))],
            index=dates,
            dtype="float64",
        )
        context = SelectionContext(
                bars=pd.DataFrame(rows),
                universe=universe,
                trade_date=dates[-1],
                mode="close_final",
                as_of=dates[-1],
                price_as_of=dates[-1],
                metadata_as_of=dates[-1],
                trend_profile=None,
                data_status="ready",
                diagnostics={"clean_dates": dates},
            )
        context.benchmark_returns = benchmark
        return context, benchmark

    def test_a_uses_absolute_gates_and_mutually_exclusive_paths(self) -> None:
        context, benchmark = self._context()

        evaluation = evaluate_strong_trend(context, min_down_days=3)
        tiers = evaluation.rows.set_index("sec_code")["strength_tier"].to_dict()

        self.assertEqual(tiers["600001"], "continuation")
        self.assertEqual(tiers["300996"], "fresh_breakout")
        self.assertFalse(set(tiers) & {"600002", "600003", "600004"})
        self.assertTrue(set(evaluation.rows["rank_band"]).issubset({"top20_pct", "remainder"}))
        self.assertNotIn("趋势成型中", set(evaluation.rows["strength_tier"]))
        self.assertTrue((evaluation.rows["ret10"] > 0).all())
        self.assertTrue((evaluation.rows["ret20"] > 0).all())
        continuation = evaluation.rows.set_index("sec_code").loc["600001"]
        self.assertGreater(continuation["strength_age"], 1)
        self.assertLess(continuation["first_seen_as_of"], context.trade_date)
        self.assertTrue(all(continuation[column] for column in evaluation.rows.columns if column.startswith("gate_")))

    def test_no_benchmark_profile_removes_separation_for_everyone(self) -> None:
        context, _ = self._context()

        context.benchmark_returns = None
        evaluation = evaluate_strong_trend(context)

        self.assertTrue(evaluation.rows["ranking_profile"].eq("no_benchmark").all())
        self.assertTrue(evaluation.rows["separation_pct"].isna().all())
        self.assertTrue((evaluation.rows["score"] > 0).all())

    def test_history_tiers_are_the_current_a_source_of_truth(self) -> None:
        context, benchmark = self._context()

        evaluation = evaluate_strong_trend(context)
        history = _a_tier_history(context, context.diagnostics["clean_dates"])
        expected = history.tiers.loc[context.trade_date].dropna().sort_index().to_dict()
        actual = evaluation.rows.set_index("sec_code")["strength_tier"].sort_index().to_dict()

        self.assertEqual(actual, expected)

    def test_fresh_breakout_rejects_a_deep_drawdown_rebound(self) -> None:
        context, benchmark = self._context()
        bars = context.bars.copy()
        target = bars["sec_code"].eq("300996")
        target_dates = sorted(bars.loc[target, "trade_date"].unique())
        for index, trade_date in enumerate(target_dates):
            position = target_dates.index(trade_date)
            close = 100.0 if position < 60 else 30.0 + (position - 60) * 0.5
            high = 100.0 if position == 60 else close * 1.01
            mask = target & bars["trade_date"].eq(trade_date)
            bars.loc[mask, ["adj_open", "adj_high", "adj_low", "adj_close", "close", "pre_close"]] = [
                close, high, close * 0.99, close, close, close,
            ]
        context.bars = bars

        evaluation = evaluate_strong_trend(context)
        history = _a_tier_history(context, context.diagnostics["clean_dates"])

        self.assertNotIn("300996", set(evaluation.rows["sec_code"]))
        self.assertGreater(history.metrics["near_high_distance_atr"].at[context.trade_date, "300996"], 2.5)
        self.assertFalse(history.gates["near_high"].at[context.trade_date, "300996"])
        failed = {row["sec_code"]: row["first_failed_gate"] for row in evaluation.diagnostics["diagnostic_rows"]}
        self.assertIn(failed["300996"], {"close_ma20_ma60", "ma20_slope_5", "near_high", "a_path"})

    def test_weak_market_can_publish_an_empty_strong_trend_list(self) -> None:
        context, benchmark = self._context()
        bars = context.bars.copy()
        for code in bars["sec_code"].unique():
            values = [20.0 * (0.995 ** index) for index in range(len(context.diagnostics["clean_dates"]))]
            mask = bars["sec_code"].eq(code)
            bars.loc[mask, ["adj_open", "adj_high", "adj_low", "adj_close", "close", "pre_close"]] = [
                [value, value * 1.01, value * 0.99, value, value, value]
                for value in values
            ]
        context.bars = bars

        evaluation = evaluate_strong_trend(context)

        self.assertTrue(evaluation.rows.empty)

    def test_candidates_are_ranked_after_gates_with_only_formal_tiers(self) -> None:
        context, benchmark = self._context()

        candidates = select_from_context(context)

        self.assertEqual([candidate.rank for candidate in candidates], list(range(1, len(candidates) + 1)))
        self.assertTrue(candidates)
        self.assertTrue(all(candidate.features["strength_tier"] in {"continuation", "fresh_breakout"} for candidate in candidates))
        self.assertTrue(all("rps_prior" not in candidate.features for candidate in candidates))

    def test_strong_trend_scanner_is_registered(self) -> None:
        strategy_ids = {scanner.strategy_id for scanner in get_registered_scanners()}
        self.assertIn("strong_trend", strategy_ids)


if __name__ == "__main__":
    unittest.main()
