from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests._mining_test_helpers import create_sample_market_dbs


class CandidatePersistenceTests(unittest.TestCase):
    @staticmethod
    def _context(fingerprint: str, *, mode: str = "close_final") -> SimpleNamespace:
        return SimpleNamespace(
            trade_date="2026-08-05",
            mode=mode,
            as_of="2026-08-05 15:10:00",
            price_as_of="2026-08-05",
            input_fingerprint=fingerprint,
        )

    @staticmethod
    def _results(*, code_suffix: str = "1", include_all: bool = True):
        from mining.candidate_persistence import CapabilityResult
        from mining.capabilities import formal_definitions
        from mining.scanners import Candidate

        results = []
        for index, definition in enumerate(formal_definitions(), start=1):
            candidates = ()
            if include_all or index == 1:
                candidates = (
                    Candidate(
                        strategy_id=definition.strategy_id,
                        version="v2.0",
                        trade_date="2026-08-05",
                        sec_type="stock",
                        sec_code=f"{600000 + index + (10 if code_suffix != '1' else 0):06d}",
                        sec_name=f"测试{index}",
                        entry_price=10.0 + index,
                        features={"reference_price": 10.0 + index, "score": index},
                        rank=1,
                    ),
                )
            results.append(CapabilityResult(definition.strategy_id, candidates, 10, "ok" if candidates else "empty"))
        return tuple(results)

    @staticmethod
    def _conn() -> sqlite3.Connection:
        from mining.db import SCHEMA_SQL

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_SQL)
        return conn

    def test_migration_is_idempotent_and_adds_batch_columns(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema, selection_batch_schema_ready

        conn = self._conn()
        try:
            self.assertFalse(selection_batch_schema_ready(conn))
            self.assertNotIn("batch_id", [row[1] for row in conn.execute("PRAGMA table_info(strategy_runs)")])
            migrate_selection_batch_schema(conn)
            migrate_selection_batch_schema(conn)
            self.assertTrue(selection_batch_schema_ready(conn))
            self.assertEqual(
                {"batch_id", "mode", "input_fingerprint"},
                {row[1] for row in conn.execute("PRAGMA table_info(strategy_runs)")} & {"batch_id", "mode", "input_fingerprint"},
            )
        finally:
            conn.close()

    def test_same_input_is_idempotent_and_new_fingerprint_replaces_stale_rows(self) -> None:
        from mining.candidate_persistence import (
            migrate_selection_batch_schema,
            persist_close_final_batch,
        )
        from mining.capabilities import SCREENING_DEFINITION_VERSION

        conn = self._conn()
        try:
            migrate_selection_batch_schema(conn)
            first = persist_close_final_batch(conn, self._context("first"), self._results(include_all=True))
            same = persist_close_final_batch(conn, self._context("first"), self._results(include_all=True))
            second = persist_close_final_batch(conn, self._context("second"), self._results(code_suffix="2", include_all=False))
            self.assertEqual(first.status, "complete")
            self.assertTrue(same.reused)
            self.assertEqual(second.status, "complete")
            self.assertEqual(
                12,
                conn.execute(
                    "SELECT COUNT(*) FROM strategy_runs WHERE version=? AND mode='close_final'",
                    (SCREENING_DEFINITION_VERSION,),
                ).fetchone()[0],
            )
            rows = conn.execute(
                "SELECT strategy_id, sec_code FROM candidates WHERE version=? ORDER BY strategy_id",
                (SCREENING_DEFINITION_VERSION,),
            ).fetchall()
            self.assertEqual([("strong_trend", "600011")], [tuple(row) for row in rows])
        finally:
            conn.close()

    def test_failed_first_middle_and_last_capability_leave_old_complete_batch_visible(self) -> None:
        from mining.candidate_persistence import CapabilityResult, migrate_selection_batch_schema, persist_close_final_batch
        from mining.capabilities import formal_definitions
        from mining.reports import load_formal_capability_candidates

        for failing_index in (0, 3, 5):
            with self.subTest(failing_index=failing_index):
                conn = self._conn()
                try:
                    migrate_selection_batch_schema(conn)
                    seed = persist_close_final_batch(conn, self._context("seed"), self._results(include_all=True))
                    failed_results = list(self._results(code_suffix="9", include_all=False))
                    definition = formal_definitions()[failing_index]
                    failed_results[failing_index] = CapabilityResult(
                        definition.strategy_id, (), 10, "failed", "deliberate test failure"
                    )
                    failed = persist_close_final_batch(conn, self._context(f"failed-{failing_index}"), failed_results)
                    visible = load_formal_capability_candidates(conn, "2026-08-05")
                    self.assertEqual(seed.status, "complete")
                    self.assertEqual(failed.status, "failed")
                    self.assertEqual(6, len(visible))
                    self.assertEqual(
                        6,
                        conn.execute(
                            "SELECT COUNT(*) FROM candidates WHERE version='v2.5'"
                        ).fetchone()[0],
                    )
                    self.assertEqual(
                        1,
                        conn.execute(
                            "SELECT COUNT(*) FROM selection_batches WHERE status='failed'"
                        ).fetchone()[0],
                    )
                finally:
                    conn.close()

    def test_intraday_mode_cannot_create_formal_candidate_rows(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema, persist_close_final_batch

        conn = self._conn()
        try:
            migrate_selection_batch_schema(conn)
            result = persist_close_final_batch(conn, self._context("snapshot", mode="intraday_snapshot"), self._results())
            self.assertEqual(result.status, "failed")
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
        finally:
            conn.close()

    def test_backfill_outcomes_excludes_current_formal_version(self) -> None:
        from mining.backtest import backfill_outcomes
        from mining.candidate_persistence import migrate_selection_batch_schema, persist_close_final_batch

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            from mining.db import connect

            conn = connect(base_dir=base)
            try:
                migrate_selection_batch_schema(conn)
                persisted = persist_close_final_batch(conn, self._context("outcomes"), self._results(include_all=False))
                result = backfill_outcomes(conn)
                self.assertEqual(persisted.status, "complete")
                self.assertEqual(result["processed"], 0)
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
