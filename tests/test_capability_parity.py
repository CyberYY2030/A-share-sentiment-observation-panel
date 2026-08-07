from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from mining.capabilities import formal_definitions
from mining.db import connect
from mining.scanners.strong_trend import evaluate_strong_trend
from mining.selection_context import build_selection_context
from tests._mining_test_helpers import create_sample_market_dbs


class CapabilityParityTests(unittest.TestCase):
    def test_registry_declares_runtime_dependencies(self) -> None:
        definitions = {definition.strategy_id: definition for definition in formal_definitions()}

        self.assertEqual(definitions["strong_trend"].optional_dependencies, ("benchmark",))
        self.assertEqual(definitions["compression_launch"].required_dependencies, ("price", "activity", "metadata"))
        self.assertEqual(definitions["counter_trend_rs"].required_dependencies, ("price", "benchmark", "metadata"))

    def test_same_close_input_has_a_stable_shared_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base)
            try:
                first = build_selection_context(conn, dates["target_trade_date"])
                second = build_selection_context(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.trend_profile, second.trend_profile)
        self.assertIn(first.benchmark_status, {"ready", "unavailable"})

    def test_strong_trend_evaluator_has_only_shared_context_input(self) -> None:
        signature = inspect.signature(evaluate_strong_trend)

        self.assertNotIn("benchmark_returns", signature.parameters)
        self.assertEqual(signature.parameters["context"].default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
