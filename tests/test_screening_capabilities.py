from __future__ import annotations

import json
import sqlite3
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from mining.capabilities import CAPABILITY_REGISTRY, formal_strategy_ids
from mining.db import SCHEMA_SQL
from mining.db import connect
from mining.reports import load_formal_capability_candidates
from mining.selection_context import SelectionContext
from mining.selection_runtime import MODE_INTRADAY, SelectionRuntime
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

    def test_panel_and_report_consume_the_same_formal_strategy_rows(self) -> None:
        conn = _conn_with_formal_rows()
        try:
            report_rows = load_formal_capability_candidates(conn, "2026-08-06")
            panel_rows = _load_formal_capability_candidates(conn, "2026-08-06")
            self.assertEqual(set(report_rows["strategy_id"]), set(panel_rows["strategy_id"]))
            self.assertEqual(set(panel_rows["capability"]), {"A", "B", "C"})
            self.assertEqual(panel_rows.loc[panel_rows["strategy_id"].eq("second_launch"), "reference_price"].iloc[0], 9.5)
        finally:
            conn.close()

    def test_pullback_strength_by_phase_filter_and_empty_run_status_are_explicit(self) -> None:
        conn = _conn_with_formal_rows()
        try:
            rows = _load_formal_capability_candidates(conn, "2026-08-06")
            pullback = rows[rows["strategy_id"].eq("second_launch")]
            filtered = _filter_pullback_strength_phase(pullback, "强趋势", "回调到位")
            self.assertEqual(filtered["sec_code"].tolist(), ["600002"])
            status = _capability_run_status(conn, "2026-08-06")
            self.assertEqual(status.loc[status["strategy_id"].eq("base_breakout"), "availability"].iloc[0], "未运行")
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

    def test_snapshot_view_recomputes_all_capabilities_without_writing_official_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base)
            try:
                snapshot = pd.read_sql_query(
                    """
                    SELECT k.sec_code, s.name AS sec_name, k.open, k.high, k.low, k.close,
                           k.pre_close, k.volume, k.amount, k.turnover_ratio
                    FROM ash.kline_daily k
                    LEFT JOIN ash.stock_info s ON s.sec_code=k.sec_code
                    WHERE k.sec_type='stock' AND k.trade_date=?
                    """,
                    conn,
                    params=[dates["target_trade_date"]],
                )
                before = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                rows, status, evidence = _formal_capability_view(
                    conn,
                    "2026-04-20",
                    snapshot_loader=lambda: snapshot,
                    now=dt.datetime(2026, 4, 20, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    runtime=SelectionRuntime(),
                )
                after = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(evidence["mode"], MODE_INTRADAY)
        self.assertEqual(set(status["strategy_id"]), set(formal_strategy_ids()))
        self.assertEqual(before, after)
        self.assertTrue(rows.empty or set(rows["strategy_id"]).issubset(set(formal_strategy_ids())))
