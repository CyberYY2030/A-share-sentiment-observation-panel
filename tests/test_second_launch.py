from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

from mining.scanners.second_launch import SecondLaunchScanner, select_candidates_from_pullback_support
from mining.selection_context import SelectionContext
from mining.watchlist import PULLBACK_STATE_READY, PULLBACK_STATE_RETRIGGER, PullbackSupportEvaluation


class SecondLaunchTests(unittest.TestCase):
    def _evaluation(self) -> PullbackSupportEvaluation:
        return PullbackSupportEvaluation(
            rows=pd.DataFrame(
                [
                    {
                        "sec_code": "600001",
                        "sec_name": "Restart",
                        "reference_price": 12.5,
                        "state": PULLBACK_STATE_RETRIGGER,
                        "strength_tier": "强趋势",
                        "first_structure_date": "2026-01-05",
                        "a_qualified_date": "2026-01-08",
                        "main_rise_base": 8.0,
                        "main_rise_base_date": "2025-12-01",
                        "peak_close": 14.0,
                        "peak_date": "2026-01-20",
                        "pullback_pct": -0.11,
                        "ma10": 12.4,
                        "ma20": 12.0,
                        "ma_long": 10.0,
                        "dist_ma10": 0.008,
                        "dist_ma20": -0.015,
                        "shrink_ratio": 0.6,
                        "pullback_negative_days": 4,
                        "made_new_low_recent": False,
                        "reclaim_ma10": True,
                        "activity_expand": True,
                    },
                    {
                        "sec_code": "600002",
                        "sec_name": "ReadyOnly",
                        "reference_price": 10.0,
                        "state": PULLBACK_STATE_READY,
                    },
                ]
            ),
            trend_profile=None,
            diagnostics={},
        )

    def test_second_launch_selects_only_shared_retrigger_rows(self) -> None:
        candidates = select_candidates_from_pullback_support(
            self._evaluation(),
            trade_date="2026-02-03",
            strategy_id="second_launch",
            version="v2.0",
            top_n=20,
        )
        self.assertEqual([candidate.sec_code for candidate in candidates], ["600001"])
        self.assertEqual(candidates[0].features["state"], PULLBACK_STATE_RETRIGGER)
        self.assertEqual(candidates[0].features["reference_price"], 12.5)
        self.assertAlmostEqual(candidates[0].features["ma_proximity"], 0.008)
        self.assertTrue(candidates[0].features["stop_signal"])
        self.assertEqual(candidates[0].features["flag_strategies"], "strong_trend")

    def test_scanner_consumes_one_evaluation_and_persists_that_same_final_state(self) -> None:
        context = SelectionContext(
            bars=pd.DataFrame(),
            universe=pd.DataFrame([{"sec_code": "600001"}]),
            trade_date="2026-02-03",
            mode="close_final",
            as_of="2026-02-03T15:05:00+08:00",
            price_as_of="2026-02-03",
            metadata_as_of=None,
            trend_profile="P120",
            data_status="ready",
        )
        evaluation = self._evaluation()
        with (
            mock.patch("mining.scanners.second_launch.build_selection_context", return_value=context),
            mock.patch("mining.scanners.second_launch.build_pullback_support", return_value=evaluation) as build,
            mock.patch("mining.scanners.second_launch.persist_pullback_support_states", return_value=1) as persist,
        ):
            candidates = SecondLaunchScanner().run(mock.sentinel.connection, "2026-02-03")
        build.assert_called_once_with(mock.sentinel.connection, "2026-02-03", context=context)
        persist.assert_called_once_with(mock.sentinel.connection, context, evaluation)
        self.assertEqual([candidate.sec_code for candidate in candidates], ["600001"])
