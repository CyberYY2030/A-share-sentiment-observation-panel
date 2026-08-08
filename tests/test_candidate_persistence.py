from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

    def test_latest_batch_only_reuses_same_input_and_aba_finishes_on_a(self) -> None:
        from mining.candidate_persistence import (
            migrate_selection_batch_schema,
            persist_close_final_batch,
        )
        from mining.capabilities import SCREENING_DEFINITION_VERSION
        from mining.reports import load_formal_capability_candidates
        from mining.streamlit_tabs.tab_scanner import _load_formal_capability_candidates

        conn = self._conn()
        try:
            migrate_selection_batch_schema(conn)
            first = persist_close_final_batch(conn, self._context("first"), self._results(include_all=True))
            second = persist_close_final_batch(conn, self._context("second"), self._results(code_suffix="2", include_all=False))
            third = persist_close_final_batch(conn, self._context("first"), self._results(include_all=True))
            visible = load_formal_capability_candidates(conn, "2026-08-05")
            panel_visible = _load_formal_capability_candidates(conn, "2026-08-05")
            self.assertEqual(first.status, "complete")
            self.assertEqual(second.status, "complete")
            self.assertEqual(third.status, "complete")
            self.assertFalse(third.reused)
            self.assertNotEqual(first.batch_id, third.batch_id)
            self.assertEqual(
                18,
                conn.execute(
                    "SELECT COUNT(*) FROM strategy_runs WHERE version=? AND mode='close_final'",
                    (SCREENING_DEFINITION_VERSION,),
                ).fetchone()[0],
            )
            rows = conn.execute(
                "SELECT strategy_id, sec_code FROM candidates WHERE version=? ORDER BY strategy_id",
                (SCREENING_DEFINITION_VERSION,),
            ).fetchall()
            self.assertEqual(6, len(visible))
            self.assertEqual({"600001", "600002", "600003", "600004", "600005", "600006"}, set(visible["sec_code"]))
            self.assertEqual(set(visible["sec_code"]), set(panel_visible["sec_code"]))
            self.assertEqual(6, len(rows))
            self.assertIn(("strong_trend", "600001"), [tuple(row) for row in rows])
        finally:
            conn.close()

    def test_dual_connections_reuse_the_latest_batch_under_immediate_lock(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema, persist_close_final_batch
        from mining.db import SCHEMA_SQL

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.db"
            setup = sqlite3.connect(db_path)
            try:
                setup.executescript(SCHEMA_SQL)
                migrate_selection_batch_schema(setup)
            finally:
                setup.close()

            barrier = threading.Barrier(2)

            def persist_from_connection():
                conn = sqlite3.connect(db_path, timeout=5.0)
                try:
                    barrier.wait(timeout=5.0)
                    return persist_close_final_batch(conn, self._context("same"), self._results())
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = [future.result(timeout=10.0) for future in [pool.submit(persist_from_connection), pool.submit(persist_from_connection)]]

            self.assertEqual({"complete"}, {outcome.status for outcome in outcomes})
            self.assertEqual(1, sum(outcome.reused for outcome in outcomes))
            verify = sqlite3.connect(db_path)
            try:
                self.assertEqual(1, verify.execute("SELECT COUNT(*) FROM selection_batches WHERE status='complete'").fetchone()[0])
                self.assertEqual(6, verify.execute("SELECT COUNT(*) FROM strategy_runs WHERE mode='close_final'").fetchone()[0])
            finally:
                verify.close()

    def test_missing_or_invalid_schema_never_reads_unbatched_formal_rows(self) -> None:
        from mining.reports import load_formal_capability_candidates
        from mining.streamlit_tabs.tab_scanner import (
            _capability_run_status,
            _load_formal_capability_candidates,
            _load_persisted_candidates,
        )

        for schema_kind in ("missing", "invalid"):
            with self.subTest(schema_kind=schema_kind):
                conn = self._conn()
                try:
                    if schema_kind == "invalid":
                        conn.execute("CREATE TABLE selection_batches (batch_id INTEGER PRIMARY KEY)")
                    run_id = conn.execute(
                        """
                        INSERT INTO strategy_runs (
                          strategy_id, version, trade_date, run_at, universe_size, n_candidates, status
                        ) VALUES ('strong_trend', 'v2.0', '2026-08-05T15:00:00', '2026-08-05', 1, 1, 'ok')
                        """
                    ).lastrowid
                    for version, code in (("v2.0", "000001"), ("v2.5", "000002")):
                        conn.execute(
                            """
                            INSERT INTO candidates (
                              run_id, strategy_id, version, trade_date, sec_type, sec_code,
                              sec_name, entry_price, features_json, rank
                            ) VALUES (?, 'strong_trend', ?, '2026-08-05', 'stock', ?, 'legacy', 10.0, '{}', 1)
                            """,
                            (run_id, version, code),
                        )
                    conn.commit()
                    report_rows = load_formal_capability_candidates(conn, "2026-08-05")
                    panel_rows = _load_formal_capability_candidates(conn, "2026-08-05")
                    persisted_rows = _load_persisted_candidates(conn, "2026-08-05")
                    status = _capability_run_status(conn, "2026-08-05")
                finally:
                    conn.close()

                expected = "migration_required" if schema_kind == "missing" else "schema_invalid"
                self.assertTrue(report_rows.empty)
                self.assertTrue(panel_rows.empty)
                self.assertTrue(persisted_rows.empty)
                self.assertEqual({expected}, set(status["availability"]))

    def test_sql_error_is_structured_and_never_falls_back_to_legacy_formal_rows(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema, persist_close_final_batch
        from mining.reports import load_formal_capability_candidates
        from mining.streamlit_tabs.tab_scanner import _load_formal_capability_candidates

        conn = self._conn()
        try:
            migrate_selection_batch_schema(conn)
            persisted = persist_close_final_batch(conn, self._context("sql-error"), self._results())
            self.assertEqual("complete", persisted.status)
            conn.execute("DROP TABLE candidates")
            report_rows = load_formal_capability_candidates(conn, "2026-08-05")
            panel_rows = _load_formal_capability_candidates(conn, "2026-08-05")
            self.assertTrue(report_rows.empty)
            self.assertTrue(panel_rows.empty)
            self.assertEqual("schema_error", report_rows.attrs["formal_batch_status"])
            self.assertEqual("schema_error", panel_rows.attrs["formal_batch_status"])
        finally:
            conn.close()

    def test_close_loader_never_creates_a_formal_batch_on_read(self) -> None:
        from mining.streamlit_tabs import tab_scanner

        conn = self._conn()
        try:
            with mock.patch.object(
                tab_scanner,
                "_run_display_scanners",
                side_effect=AssertionError("close loader must not rescan"),
            ):
                rows = tab_scanner._load_close_opportunities(conn, "2026-08-05")
            self.assertTrue(rows.empty)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='selection_batches'"
                ).fetchone()
            )
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
