from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from mining.candidate_persistence import migrate_selection_batch_schema
from mining.data_quality import usable_stock_trade_dates
from mining.db import connect
from mining.scanners.strong_trend import evaluate_strong_trend
from mining.selection_context import SelectionContext, build_selection_context
from mining.watchlist import _load_a_qualified_pool, a_history_coverage
from tests._mining_test_helpers import create_sample_market_dbs


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "screening_v2"
FIXTURE_CSV = FIXTURE_DIR / "five-stock-canonical-2026-08-05.csv"
FIXTURE_MANIFEST = FIXTURE_DIR / "five-stock-canonical-2026-08-05.manifest.json"


def _fixture_context() -> SelectionContext:
    bars = pd.read_csv(FIXTURE_CSV, dtype={"sec_code": str})
    bars["sec_code"] = bars["sec_code"].str.zfill(6)
    dates = sorted(bars["trade_date"].astype(str).unique())
    peers: list[dict[str, object]] = []
    for code_index in range(20):
        code = f"600{100 + code_index:03d}"
        prior = 20.0
        for day_index, trade_date in enumerate(dates):
            close = 20.0 * (0.995 ** day_index)
            peers.append(
                {
                    "sec_code": code,
                    "trade_date": trade_date,
                    "row_status": "valid_trade",
                    "open": prior,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": prior,
                    "adj_open": prior,
                    "adj_high": close * 1.01,
                    "adj_low": close * 0.99,
                    "adj_close": close,
                    "adj_pre_close": prior,
                    "volume": 1_000_000.0,
                    "amount": 1_000_000.0,
                }
            )
            prior = close
    all_bars = pd.concat([bars, pd.DataFrame(peers)], ignore_index=True, sort=False)
    codes = sorted(all_bars["sec_code"].astype(str).str.zfill(6).unique())
    return SelectionContext(
        bars=all_bars,
        universe=pd.DataFrame({"sec_code": codes, "sec_name": codes}),
        trade_date=dates[-1],
        mode="close_final",
        as_of=dates[-1],
        price_as_of=dates[-1],
        metadata_as_of=dates[-1],
        trend_profile=None,
        data_status="ready",
        diagnostics={"clean_dates": dates, "usable_dates": dates},
    )


class R4FixtureRegressionTests(unittest.TestCase):
    def test_frozen_fixture_manifest_has_five_hashed_machine_cases(self) -> None:
        manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
        actual_hash = hashlib.sha256(FIXTURE_CSV.read_bytes()).hexdigest().upper()

        self.assertEqual(manifest["data_hash"], actual_hash)
        self.assertEqual(manifest["human_calibration"], "not-provided")
        self.assertEqual(
            {record["sec_code"] for record in manifest["records"]},
            {"300996", "601858", "688141", "688627", "301571"},
        )
        for record in manifest["records"]:
            self.assertEqual(record["as_of"], manifest["as_of"])
            self.assertEqual(record["data_hash"], actual_hash)
            self.assertTrue(record["expected_path"])
            self.assertTrue(record["key_metrics"])
            self.assertTrue(record["failure_conditions"])

    def test_fixture_replays_fresh_breakouts_with_synthetic_cross_section(self) -> None:
        rows = evaluate_strong_trend(_fixture_context()).rows
        tiers = rows.set_index("sec_code")["strength_tier"].to_dict()

        self.assertEqual(tiers["300996"], "fresh_breakout")
        self.assertEqual(tiers["601858"], "fresh_breakout")
        self.assertNotIn("688141", tiers)
        self.assertNotIn("688627", tiers)
        self.assertNotIn("301571", tiers)

    def test_deep_rebound_never_reenters_fresh_breakout(self) -> None:
        context = _fixture_context()
        bars = context.bars.copy()
        mask = bars["sec_code"].eq("300996")
        dates = sorted(bars.loc[mask, "trade_date"].astype(str).unique())
        for index, trade_date in enumerate(dates):
            close = 100.0 if index < 60 else 30.0 + (index - 60) * 0.5
            day_mask = mask & bars["trade_date"].eq(trade_date)
            bars.loc[day_mask, ["open", "high", "low", "close", "pre_close", "adj_open", "adj_high", "adj_low", "adj_close", "adj_pre_close"]] = [
                close,
                close * 1.01,
                close * 0.99,
                close,
                close,
                close,
                close * 1.01,
                close * 0.99,
                close,
                close,
            ]
        context.bars = bars

        rows = evaluate_strong_trend(context).rows
        self.assertNotIn("300996", set(rows["sec_code"]))

    def test_weak_market_allows_zero_strong_trend_rows(self) -> None:
        context = _fixture_context()
        bars = context.bars.copy()
        for code in bars["sec_code"].unique():
            mask = bars["sec_code"].eq(code)
            dates = sorted(bars.loc[mask, "trade_date"].astype(str).unique())
            for index, trade_date in enumerate(dates):
                close = 20.0 * (0.995 ** index)
                day_mask = mask & bars["trade_date"].eq(trade_date)
                bars.loc[day_mask, ["open", "high", "low", "close", "pre_close", "adj_open", "adj_high", "adj_low", "adj_close", "adj_pre_close"]] = [
                    close,
                    close * 1.01,
                    close * 0.99,
                    close,
                    close,
                    close,
                    close * 1.01,
                    close * 0.99,
                    close,
                    close,
                ]
        context.bars = bars

        self.assertTrue(evaluate_strong_trend(context).rows.empty)

    def test_provider_halt_keeps_market_day_but_excludes_current_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base)
            try:
                previous = conn.execute(
                    """
                    SELECT close FROM ash.kline_daily
                    WHERE sec_type='stock' AND sec_code='600001' AND trade_date < ?
                    ORDER BY trade_date DESC LIMIT 1
                    """,
                    (dates["target_trade_date"],),
                ).fetchone()[0]
                conn.execute(
                    """
                    UPDATE ash.kline_daily
                    SET open=0, high=0, low=0, close=0, pre_close=?, volume=0, amount=0
                    WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
                    """,
                    (previous, dates["target_trade_date"]),
                )
                conn.commit()
                context = build_selection_context(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertIn(dates["target_trade_date"], context.diagnostics["clean_dates"])
        self.assertNotIn("600001", set(context.universe["sec_code"]))


class R4HistoryAndIsolationTests(unittest.TestCase):
    @staticmethod
    def _insert_a_batch(conn, trade_date: str, *, status: str, include_candidate: bool, completed_at: str) -> None:
        batch = conn.execute(
            """
            INSERT INTO selection_batches (
                trade_date, definition_version, mode, input_fingerprint, status, created_at, completed_at
            ) VALUES (?, 'v2.5', 'close_final', ?, 'complete', ?, ?)
            """,
            (trade_date, f"fixture-{trade_date}-{completed_at}", completed_at, completed_at),
        )
        run = conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, version, trade_date, run_at, universe_size, n_candidates, status,
                batch_id, mode, input_fingerprint
            ) VALUES ('strong_trend', 'v2.5', ?, ?, 1, ?, ?, ?, 'close_final', ?)
            """,
            (trade_date, completed_at, 1 if include_candidate else 0, status, batch.lastrowid, f"fixture-{trade_date}"),
        )
        if include_candidate:
            conn.execute(
                """
                INSERT INTO candidates (
                    run_id, strategy_id, version, trade_date, sec_type, sec_code, sec_name, entry_price, features_json, rank
                ) VALUES (?, 'strong_trend', 'v2.5', ?, 'stock', '600001', 'Fixture', 10.0, ?, 1)
                """,
                (run.lastrowid, trade_date, json.dumps({"strength_tier": "fresh_breakout"})),
            )
        conn.commit()

    def test_a_history_uses_latest_batch_without_read_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base)
            try:
                migrate_selection_batch_schema(conn)
                usable_dates, _ = usable_stock_trade_dates(conn)
                stale_day, current_day = usable_dates[-2:]
                self._insert_a_batch(conn, stale_day, status="ok", include_candidate=True, completed_at=f"{stale_day}T15:01:00")
                self._insert_a_batch(conn, stale_day, status="empty", include_candidate=False, completed_at=f"{stale_day}T15:02:00")
                self._insert_a_batch(conn, current_day, status="ok", include_candidate=False, completed_at=f"{current_day}T15:01:00")
                before = conn.execute("SELECT COUNT(*) FROM selection_batches").fetchone()[0]
                coverage = a_history_coverage(conn, current_day, target_sessions=2, usable_dates=usable_dates)
                qualified, _ = _load_a_qualified_pool(conn, [stale_day, current_day])
                after = conn.execute("SELECT COUNT(*) FROM selection_batches").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["covered_sessions"], 2)
        self.assertEqual(qualified, {})
        self.assertEqual(before, after)

    def test_a_history_is_explicitly_unavailable_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base)
            try:
                coverage = a_history_coverage(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertEqual(coverage["status"], "migration_required")
        self.assertEqual(coverage["covered_sessions"], 0)

    def test_screening_base_dir_env_is_an_opt_in_seam(self) -> None:
        from app_panel import app_dir, screening_base_dir

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SCREENING_BASE_DIR": tmp}, clear=False):
                self.assertEqual(screening_base_dir(), str(Path(tmp).resolve()))
        with mock.patch.dict(os.environ, {"SCREENING_BASE_DIR": ""}, clear=False):
            self.assertEqual(screening_base_dir(), app_dir())


if __name__ == "__main__":
    unittest.main()
