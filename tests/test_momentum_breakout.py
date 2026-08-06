from __future__ import annotations

import unittest

from mining.scanners.momentum_breakout import evaluate_momentum_anomaly, select_momentum_anomaly_from_context

from .test_launch_burst import _event_context


class MomentumBreakoutV2Tests(unittest.TestCase):
    def test_board_aware_momentum_candidates_are_not_filtered_by_a_or_rps_membership(self) -> None:
        context = _event_context()
        context.diagnostics["simulated_a_members"] = {"600001"}
        rows, diagnostics = evaluate_momentum_anomaly(context)
        self.assertEqual(set(rows["sec_code"]), {"600001", "300001"})
        self.assertTrue(rows["event_subtype"].eq("momentum_anomaly").all())
        self.assertTrue(rows["event_path"].str.contains("gain").all())
        self.assertEqual(diagnostics["result_count"], 2)
        self.assertEqual({candidate.sec_code for candidate in select_momentum_anomaly_from_context(context)}, {"600001", "300001"})
