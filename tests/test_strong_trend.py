from __future__ import annotations

import unittest

import pandas as pd

from mining.scanners import get_registered_scanners
from mining.scanners.strong_trend import evaluate_strong_trend, select_from_context
from mining.selection_context import SelectionContext


class StrongTrendTests(unittest.TestCase):
    def _context(self, *, liquidity: dict[str, float | None] | None = None) -> tuple[SelectionContext, pd.Series]:
        dates = pd.date_range("2025-01-01", periods=140, freq="B").strftime("%Y-%m-%d").tolist()
        rates = {"600001": 0.0030, "600002": 0.0025, "600003": 0.0020, "600004": 0.0040}
        rows = []
        for code, rate in rates.items():
            for index, trade_date in enumerate(dates):
                close = 10 * ((1 + rate) ** index)
                rows.append(
                    {
                        "sec_code": code,
                        "trade_date": trade_date,
                        "adj_close": close,
                        "close": close,
                        "change_pct": rate * 100.0,
                        "amount": 1_000_000_000,
                        "volume": 1_000_000,
                    }
                )
        liquidity = liquidity or {"600001": 0.9, "600002": 0.8, "600003": 0.7, "600004": 0.2}
        universe = pd.DataFrame(
            [
                {
                    "sec_code": code,
                    "sec_name": code,
                    "close": 10 * ((1 + rate) ** 139),
                    "liquidity_pct": liquidity[code],
                }
                for code, rate in rates.items()
            ]
        )
        benchmark = pd.Series(
            [-0.01 if index % 7 == 0 else 0.001 for index in range(140)],
            index=dates,
            dtype="float64",
        )
        return (
            SelectionContext(
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
            ),
            benchmark,
        )

    def test_hard_liquidity_gate_cannot_be_overridden_by_a_higher_score(self) -> None:
        context, benchmark = self._context()

        evaluation = evaluate_strong_trend(context, benchmark_returns=benchmark, min_down_days=3)

        self.assertEqual(evaluation.trend_profile.profile_id, "P120")
        self.assertNotIn("600004", set(evaluation.rows["sec_code"]))
        self.assertIn("liquidity_failed", evaluation.diagnostics["skipped_reason_counts"])
        self.assertTrue((evaluation.rows["near_high_ratio"] >= 0.85).all())

    def test_optional_separation_is_removed_for_everyone_when_benchmark_is_unavailable(self) -> None:
        context, _ = self._context()

        evaluation = evaluate_strong_trend(context, benchmark_returns=None)

        self.assertFalse(evaluation.diagnostics["separation_available"])
        self.assertTrue(evaluation.rows["score_components_used"].eq("rps_recent_pct,rps_prior_pct,near_high_pct").all())
        self.assertTrue(evaluation.rows["separation_raw"].isna().all())

    def test_candidates_are_ranked_after_gates_and_include_path_labels(self) -> None:
        context, benchmark = self._context()

        candidates = select_from_context(context, benchmark_returns=benchmark)

        self.assertTrue(candidates)
        self.assertEqual([candidate.rank for candidate in candidates], list(range(1, len(candidates) + 1)))
        self.assertNotIn("600004", {candidate.sec_code for candidate in candidates})
        self.assertTrue(all("path_context" in candidate.features for candidate in candidates))
        self.assertTrue(all(candidate.features["strength_tier"] in {"强趋势", "趋势成型中"} for candidate in candidates))

    def test_strong_trend_scanner_is_registered(self) -> None:
        strategy_ids = {scanner.strategy_id for scanner in get_registered_scanners()}
        self.assertIn("strong_trend", strategy_ids)


if __name__ == "__main__":
    unittest.main()
