from __future__ import annotations

import unittest

from mining.scanners.launch_burst import evaluate_compression_launch
from mining.scanners.momentum_breakout import evaluate_momentum_anomaly

from .test_launch_burst import _event_context


class EventFusionTests(unittest.TestCase):
    def test_two_event_subtypes_keep_independent_scores_and_may_overlap(self) -> None:
        context = _event_context()
        compression, _ = evaluate_compression_launch(context)
        momentum, _ = evaluate_momentum_anomaly(context)
        self.assertEqual(set(compression["event_subtype"]), {"compression_launch"})
        self.assertEqual(set(momentum["event_subtype"]), {"momentum_anomaly"})
        self.assertEqual(set(compression["sec_code"]), set(momentum["sec_code"]))
        self.assertIn("sigma_pct", compression.columns)
        self.assertIn("change_pct_rank", momentum.columns)
        self.assertNotIn("change_pct_rank", compression.columns)
