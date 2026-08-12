from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mining.capabilities import CAPABILITY_REGISTRY, formal_strategy_ids
from mining.db import SCHEMA_SQL
from mining.db import connect
from mining.reports import load_formal_capability_candidates
from mining.candidate_persistence import migrate_selection_batch_schema
from mining.selection_context import SelectionContext
from mining.streamlit_tabs.tab_scanner import (
    _capability_run_status,
    _filter_pullback_strength_phase,
    _formal_capability_view,
    _load_formal_capability_candidates,
    _selection_evidence,
)
from tests._mining_test_helpers import create_sample_market_dbs


def _conn_with_formal_rows() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    rows = [
        ("strong_trend", "600001", "Trend", {"reference_price": 10.5, "strength_tier": "强趋势", "score": 0.9}),
        ("second_launch", "600002", "Pullback", {"reference_price": 9.5, "strength_tier": "强趋势", "state": "回调到位"}),
        ("compression_launch", "300001", "Launch", {"reference_price": 8.5, "event_subtype": "compression_launch", "score": 0.8}),
    ]
    for run_id, (strategy_id, code, name, features) in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO strategy_runs (run_id, strategy_id, version, trade_date, run_at, universe_size, n_candidates, status)
            VALUES (?, ?, 'v2.0', '2026-08-06', '2026-08-06T15:05:00+08:00', 10, 1, 'ok')
            """,
            (run_id, strategy_id),
        )
        conn.execute(
            """
            INSERT INTO candidates (run_id, strategy_id, version, trade_date, sec_type, sec_code, sec_name, entry_price, features_json, rank)
            VALUES (?, ?, 'v2.0', '2026-08-06', 'stock', ?, ?, 1.0, ?, 1)
            """,
            (run_id, strategy_id, code, name, json.dumps(features, ensure_ascii=False)),
        )
    conn.commit()
    return conn


class ScreeningCapabilityRegistryTests(unittest.TestCase):
    def test_registry_declares_a_to_e_with_required_consumer_contract(self) -> None:
        self.assertEqual({item.capability for item in CAPABILITY_REGISTRY}, {"A", "B", "C", "D", "E"})
        self.assertEqual(len(formal_strategy_ids()), 6)
        for item in CAPABILITY_REGISTRY:
            self.assertTrue(item.label)
            self.assertTrue(item.fields)
            self.assertTrue(item.sort_keys)

    def test_unmigrated_panel_and_report_refuse_legacy_formal_rows(self) -> None:
        conn = _conn_with_formal_rows()
        try:
            report_rows = load_formal_capability_candidates(conn, "2026-08-06")
            panel_rows = _load_formal_capability_candidates(conn, "2026-08-06")
            status = _capability_run_status(conn, "2026-08-06")
            self.assertTrue(report_rows.empty)
            self.assertTrue(panel_rows.empty)
            self.assertEqual("migration_required", report_rows.attrs["formal_batch_status"])
            self.assertEqual("migration_required", panel_rows.attrs["formal_batch_status"])
            self.assertEqual({"migration_required"}, set(status["availability"]))
        finally:
            conn.close()

    def test_pullback_strength_by_phase_filter_and_migration_status_are_explicit(self) -> None:
        conn = _conn_with_formal_rows()
        try:
            pullback = pd.DataFrame(
                [{"sec_code": "600002", "strength_tier": "tier", "state": "state"}]
            )
            filtered = _filter_pullback_strength_phase(pullback, "tier", "state")
            self.assertEqual(filtered["sec_code"].tolist(), ["600002"])
            status = _capability_run_status(conn, "2026-08-06")
            self.assertEqual({"migration_required"}, set(status["availability"]))
        finally:
            conn.close()

    def test_evidence_contains_all_as_of_and_snapshot_fields(self) -> None:
        context = SelectionContext(
            bars=pd.DataFrame(), universe=pd.DataFrame(), trade_date="2026-08-06", mode="intraday_snapshot",
            as_of="2026-08-06T10:00:00+08:00", price_as_of="2026-08-06T10:00:00+08:00",
            metadata_as_of="2026-08-05", trend_profile="P120", data_status="ready",
        )
        evidence = _selection_evidence(context, mode="intraday_snapshot", snapshot_source="unit", snapshot_coverage=0.9)
        self.assertEqual(
            set(evidence),
            {
                "mode", "as_of", "price_as_of", "metadata_as_of", "trend_profile", "data_status",
                "snapshot_source", "snapshot_coverage", "snapshot_provider", "snapshot_status",
                "snapshot_observed_at", "snapshot_raw_rows", "snapshot_normalized_rows", "snapshot_errors",
                "snapshot_from_cache", "snapshot_retry_at",
            },
        )

    def test_missing_formal_batch_shows_waiting_state_without_recomputation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base)
            try:
                migrate_selection_batch_schema(conn)
                before = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                rows, status, evidence = _formal_capability_view(
                    conn,
                    dates["target_trade_date"],
                    selected_trade_date=dates["target_trade_date"],
                )
                after = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(evidence["formal_batch_status"], "pending")
        self.assertEqual(evidence["date_status"], "batch_pending")
        self.assertEqual({"waiting_for_formal_batch"}, set(status["availability"]))
        self.assertEqual(before, after)
        self.assertTrue(rows.empty)
