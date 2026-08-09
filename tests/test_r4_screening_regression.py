from __future__ import annotations

import hashlib
import json
import os
import datetime as dt
import numbers
import sqlite3
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
from mining.watchlist import _load_a_qualified_pool, a_history_coverage, prior_usable_dates
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
        self.assertIn("source_metrics", manifest["metric_context"])
        self.assertIn("replay_metrics", manifest["metric_context"])
        self.assertEqual(manifest["tolerance"]["boolean"], "exact")
        self.assertEqual(manifest["tolerance"]["first_failed_gate"], "exact")

        evaluation = evaluate_strong_trend(_fixture_context())
        replayed_rows = evaluation.rows.set_index("sec_code")
        rejected_rows = {
            row["sec_code"]: row
            for row in evaluation.diagnostics["diagnostic_rows"]
            if row["sec_code"] in {"688141", "688627", "301571"}
        }
        numeric_tolerance = float(manifest["tolerance"]["numeric_absolute"])
        for record in manifest["records"]:
            self.assertEqual(record["as_of"], manifest["as_of"])
            self.assertEqual(record["data_hash"], actual_hash)
            self.assertTrue(record["expected_path"])
            self.assertTrue(record["source_metrics"])
            self.assertTrue(record["failure_conditions"])
            for value in record["source_metrics"].values():
                self.assertIsInstance(value, (numbers.Real, bool))

            code = record["sec_code"]
            if record["expected_path"] == "rejected":
                actual = rejected_rows[code]
            else:
                actual = replayed_rows.loc[code].to_dict()
                self.assertEqual(actual["strength_tier"], record["expected_path"])
            for metric, expected in record["replay_metrics"].items():
                self.assertAlmostEqual(float(actual[metric]), float(expected), delta=numeric_tolerance)
            for gate, expected in record["replay_gates"].items():
                self.assertEqual(bool(actual[gate]), expected)
            self.assertEqual(actual.get("first_failed_gate"), record["first_failed_gate"])

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
    def test_expected_snapshot_codes_skip_known_bad_partial_latest_day(self) -> None:
        from mining.streamlit_tabs.tab_scanner import _expected_snapshot_codes

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stock_db = base / "a_share_mvp.db"
            conn = sqlite3.connect(stock_db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE kline_daily (
                        sec_type TEXT, sec_code TEXT, trade_date TEXT,
                        open REAL, high REAL, low REAL, close REAL, pre_close REAL,
                        volume REAL, amount REAL
                    );
                    CREATE TABLE selection_session_diagnostics (
                        trade_date TEXT PRIMARY KEY, status TEXT NOT NULL,
                        reason TEXT, source_errors TEXT, updated_at TEXT NOT NULL
                    );
                    """
                )
                full_codes = [f"{600000 + index:06d}" for index in range(5_203)]
                full_dates = (
                    "2026-07-30", "2026-07-31", "2026-08-03",
                    "2026-08-04", "2026-08-05", "2026-08-06",
                )
                clean_rows = [
                    ("stock", code, trade_date, 10.0, 10.2, 9.8, 10.1, 10.0, 1_000_000, 10_100_000)
                    for trade_date in full_dates
                    for code in full_codes
                ]
                bad_rows = [
                    ("stock", code, "2026-08-07", 10.0, 10.2, 9.8, 10.1, 10.0, 1_000_000, 10_100_000)
                    for code in full_codes[:397]
                ]
                conn.executemany("INSERT INTO kline_daily VALUES (?,?,?,?,?,?,?,?,?,?)", clean_rows + bad_rows)
                conn.execute(
                    "INSERT INTO selection_session_diagnostics VALUES (?,?,?,?,?)",
                    ("2026-08-07", "known_bad_session", "partial provider day", "fixture", "2026-08-07T18:00:00"),
                )
                conn.commit()
            finally:
                conn.close()

            expected = _expected_snapshot_codes(base, "2026-08-09")

        self.assertEqual(len(expected), 5_203)
        self.assertEqual(expected, set(full_codes))

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
        self.assertEqual(coverage["covered_sessions"], 1)
        self.assertEqual(qualified, {})
        self.assertEqual(before, after)

    def test_a_history_coverage_uses_the_same_prior_usable_window_as_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base)
            try:
                migrate_selection_batch_schema(conn)
                usable_dates = pd.bdate_range("2026-01-02", periods=61).strftime("%Y-%m-%d").tolist()
                current_day = usable_dates[-1]
                prior_window = prior_usable_dates(conn, current_day, usable_dates=usable_dates)
                self.assertEqual(prior_window, usable_dates[-61:-1])

                zero = a_history_coverage(conn, current_day, usable_dates=usable_dates)
                self._insert_a_batch(
                    conn,
                    prior_window[-1],
                    status="empty",
                    include_candidate=False,
                    completed_at=f"{prior_window[-1]}T15:01:00",
                )
                one = a_history_coverage(conn, current_day, usable_dates=usable_dates)
                for trade_date in prior_window[:-1]:
                    self._insert_a_batch(
                        conn,
                        trade_date,
                        status="empty",
                        include_candidate=False,
                        completed_at=f"{trade_date}T15:01:00",
                    )
                full = a_history_coverage(conn, current_day, usable_dates=usable_dates)
            finally:
                conn.close()

        self.assertEqual((zero["covered_sessions"], zero["target_sessions"]), (0, 60))
        self.assertEqual((one["covered_sessions"], one["target_sessions"]), (1, 60))
        self.assertEqual((full["covered_sessions"], full["target_sessions"]), (60, 60))
        self.assertNotIn(current_day, full["covered_dates"])

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
        from app_panel import app_dir, screening_acceptance_now_cn, screening_base_dir

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SCREENING_BASE_DIR": tmp}, clear=False):
                self.assertEqual(screening_base_dir(), str(Path(tmp).resolve()))
        with mock.patch.dict(os.environ, {"SCREENING_BASE_DIR": ""}, clear=False):
            self.assertEqual(screening_base_dir(), app_dir())
        with mock.patch.dict(os.environ, {"SCREENING_BASE_DIR": "", "SCREENING_ACCEPTANCE_NOW_CN": "2026-08-07T10:30:00+08:00"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "requires SCREENING_BASE_DIR"):
                screening_acceptance_now_cn()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"SCREENING_BASE_DIR": tmp, "SCREENING_ACCEPTANCE_NOW_CN": "2026-08-07T10:30:00+08:00"},
                clear=False,
            ):
                self.assertEqual(screening_acceptance_now_cn(), dt.datetime(2026, 8, 7, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))))

    def test_isolated_snapshot_loader_uses_only_a_valid_cache(self) -> None:
        from mining.quote_snapshot import QuoteSnapshotAdapter, QuoteSnapshotResult
        from mining.streamlit_tabs.tab_scanner import _build_quote_snapshot_loader

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            observed_at = dt.datetime(2026, 4, 9, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
            calls: list[str] = []

            def provider_fetcher(provider: str, timeout: float) -> pd.DataFrame:
                calls.append(provider)
                raise AssertionError("isolated acceptance must not call a provider")

            adapter = QuoteSnapshotAdapter(
                cache_dir=base / "output" / "screening-v2-work" / "cache",
                provider_fetcher=provider_fetcher,
            )
            adapter._save_success(
                dates["target_trade_date"],
                QuoteSnapshotResult(
                    frame=pd.DataFrame(
                        [
                            {"sec_code": "600001", "sec_name": "Alpha", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "pre_close": 10.0, "volume": 1_000_000, "amount": 10_000_000},
                            {"sec_code": "300001", "sec_name": "Beta", "open": 20.0, "high": 20.2, "low": 19.9, "close": 20.1, "pre_close": 20.0, "volume": 1_000_000, "amount": 10_000_000},
                            {"sec_code": "600003", "sec_name": "Gamma", "open": 30.0, "high": 30.2, "low": 29.9, "close": 30.1, "pre_close": 30.0, "volume": 1_000_000, "amount": 10_000_000},
                        ]
                    ),
                    provider="isolated-cache",
                    observed_at=observed_at.isoformat(),
                    raw_rows=3,
                    normalized_rows=3,
                    coverage=1.0,
                    attempts=0,
                    errors=(),
                    status="snapshot_usable",
                    from_cache=False,
                    benchmark_closes={"000852": 6123.45, "399006": 2456.78},
                ),
            )
            loader = _build_quote_snapshot_loader(
                base,
                dates["target_trade_date"],
                adapter=adapter,
                now=observed_at + dt.timedelta(minutes=5),
                cache_only=True,
            )
            cached = loader()

        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.status, "snapshot_cache_fallback")
        self.assertEqual(cached.observed_at, observed_at.isoformat())
        self.assertEqual(cached.benchmark_closes, {"000852": 6123.45, "399006": 2456.78})
        self.assertEqual(calls, [])

    def test_public_snapshot_load_produces_and_restores_same_cycle_benchmarks(self) -> None:
        from mining.quote_snapshot import QuoteSnapshotAdapter
        from mining.selection_context import BENCHMARK_CODES

        trade_date = "2026-08-07"
        now = dt.datetime(2026, 8, 7, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        stock_codes = ("600001", "600002", "600003")
        stock_frame = pd.DataFrame(
            [
                {
                    "sec_code": code,
                    "sec_name": code,
                    "close": 10.0,
                    "pre_close": 9.8,
                    "open": 9.9,
                    "high": 10.1,
                    "low": 9.7,
                    "volume": 12_000_000,
                    "amount": 120_000_000,
                }
                for code in stock_codes
            ]
        )
        benchmark_values = {code: 5_000.0 + index for index, code in enumerate(BENCHMARK_CODES)}
        benchmark_frame = pd.DataFrame(
            [{"sec_code": code, "close": value} for code, value in benchmark_values.items()]
        )
        benchmark_calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            adapter = QuoteSnapshotAdapter(
                cache_dir=Path(tmp),
                provider_names=("stocks",),
                provider_fetcher=lambda _provider, _timeout: stock_frame,
                benchmark_provider_names=("indices",),
                benchmark_fetcher=lambda provider, _timeout: benchmark_calls.append(provider) or benchmark_frame,
            )
            live = adapter.load(trade_date, expected_codes=stock_codes, now=now)

            restarted = QuoteSnapshotAdapter(
                cache_dir=Path(tmp),
                provider_names=("offline-stocks",),
                provider_fetcher=mock.Mock(side_effect=AssertionError("cache-only must not fetch stocks")),
                benchmark_provider_names=("offline-indices",),
                benchmark_fetcher=mock.Mock(side_effect=AssertionError("cache-only must not fetch benchmarks")),
            )
            cached = restarted.load(
                trade_date,
                expected_codes=stock_codes,
                now=now + dt.timedelta(minutes=1),
                cache_only=True,
            )
            fallback_benchmark = mock.Mock(side_effect=AssertionError("stock failure must not fetch benchmarks"))
            failing = QuoteSnapshotAdapter(
                cache_dir=Path(tmp),
                provider_names=("offline-stocks",),
                provider_fetcher=mock.Mock(side_effect=RuntimeError("stocks unavailable")),
                benchmark_provider_names=("offline-indices",),
                benchmark_fetcher=fallback_benchmark,
            )
            fallback = failing.load(
                trade_date,
                expected_codes=stock_codes,
                now=now + dt.timedelta(minutes=2),
            )
            cross_day = failing.load(
                "2026-08-08",
                expected_codes=stock_codes,
                now=now + dt.timedelta(days=1),
            )

        self.assertEqual(live.status, "snapshot_usable")
        self.assertEqual(live.benchmark_status, "ready")
        self.assertEqual(live.benchmark_provider, "indices")
        self.assertEqual(live.benchmark_observed_at, now.isoformat())
        self.assertEqual(live.benchmark_closes, benchmark_values)
        self.assertEqual(benchmark_calls, ["indices"])
        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.benchmark_status, "ready")
        self.assertEqual(cached.benchmark_closes, benchmark_values)
        self.assertEqual(cached.benchmark_observed_at, live.benchmark_observed_at)
        self.assertTrue(fallback.from_cache)
        self.assertEqual(fallback.benchmark_closes, benchmark_values)
        self.assertEqual(cross_day.status, "provider_failed")
        fallback_benchmark.assert_not_called()

    def test_benchmark_failures_leave_stocks_usable_and_do_not_mix_cycles(self) -> None:
        from mining.quote_snapshot import QuoteSnapshotAdapter

        trade_date = "2026-08-07"
        now = dt.datetime(2026, 8, 7, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        stock_codes = ("600001", "600002", "600003")
        stock_frame = pd.DataFrame(
            [
                {
                    "sec_code": code,
                    "sec_name": code,
                    "close": 10.0,
                    "pre_close": 9.8,
                    "open": 9.9,
                    "high": 10.1,
                    "low": 9.7,
                    "volume": 12_000_000,
                    "amount": 120_000_000,
                }
                for code in stock_codes
            ]
        )
        cases = {
            "timeout": TimeoutError("slow index source"),
            "missing_primary": pd.DataFrame([{"sec_code": "000300", "close": 4_800.0}]),
            "invalid_primary": pd.DataFrame([{"sec_code": "000852", "close": float("nan")}]),
        }
        for label, response in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                benchmark_calls: list[str] = []

                def fetch_benchmark(provider: str, _timeout: float) -> pd.DataFrame:
                    benchmark_calls.append(provider)
                    if isinstance(response, BaseException):
                        raise response
                    return pd.DataFrame(response)

                adapter = QuoteSnapshotAdapter(
                    cache_dir=Path(tmp),
                    provider_names=("stocks",),
                    provider_fetcher=lambda _provider, _timeout: stock_frame,
                    benchmark_provider_names=("indices",),
                    benchmark_fetcher=fetch_benchmark,
                )
                result = adapter.load(trade_date, expected_codes=stock_codes, now=now)
                cached = QuoteSnapshotAdapter(
                    cache_dir=Path(tmp),
                    provider_fetcher=mock.Mock(side_effect=AssertionError("cache-only must not fetch stocks")),
                    benchmark_fetcher=mock.Mock(side_effect=AssertionError("cache-only must not fetch benchmarks")),
                ).load(
                    trade_date,
                    expected_codes=stock_codes,
                    now=now + dt.timedelta(minutes=1),
                    cache_only=True,
                )

                self.assertEqual(result.status, "snapshot_usable")
                self.assertEqual(result.benchmark_status, "unavailable")
                self.assertNotIn("000852", result.benchmark_closes)
                self.assertTrue(result.benchmark_errors)
                self.assertEqual(benchmark_calls, ["indices"])
                self.assertTrue(cached.from_cache)
                self.assertEqual(cached.benchmark_status, "unavailable")
                self.assertEqual(cached.benchmark_closes, result.benchmark_closes)
                self.assertEqual(cached.benchmark_observed_at, result.benchmark_observed_at)

    def test_snapshot_cache_fail_closed_matrix_never_requires_a_provider(self) -> None:
        from mining.quote_snapshot import QuoteSnapshotAdapter, QuoteSnapshotResult

        trade_date = "2026-08-07"
        now = dt.datetime(2026, 8, 7, 10, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        expected = {"600001", "600002", "600003"}
        full_frame = pd.DataFrame(
            [
                {"sec_code": code, "close": 10.0 + index}
                for index, code in enumerate(sorted(expected))
            ]
        )

        def result(*, frame=full_frame, observed_at=now - dt.timedelta(minutes=5)):
            return QuoteSnapshotResult(
                frame=frame,
                provider="isolated-cache",
                observed_at=observed_at.isoformat(),
                raw_rows=len(frame),
                normalized_rows=len(frame),
                coverage=1.0,
                attempts=0,
                errors=(),
                status="snapshot_usable",
                from_cache=False,
                benchmark_closes={
                    "000852": 6123.45,
                    "399006": 0.0,
                    "000300": float("inf"),
                    "000905": float("nan"),
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            adapter = QuoteSnapshotAdapter(cache_dir=Path(tmp), provider_fetcher=mock.Mock(side_effect=AssertionError))
            frame_path, metadata_path = adapter._cache_paths(trade_date)
            frame_path.parent.mkdir(parents=True)
            full_frame.to_pickle(frame_path)
            metadata_path.write_text(
                json.dumps(
                    {
                        "trade_date": trade_date,
                        "provider": "legacy-cache",
                        "observed_at": (now - dt.timedelta(minutes=5)).isoformat(),
                        "raw_rows": len(full_frame),
                    }
                ),
                encoding="utf-8",
            )
            legacy = adapter._cached_result(trade_date, expected, now, close_final=False)
            self.assertTrue(legacy.from_cache)
            self.assertEqual({}, legacy.benchmark_closes)
            self.assertIn("legacy cache has no benchmark_closes", legacy.errors)

        with tempfile.TemporaryDirectory() as tmp:
            adapter = QuoteSnapshotAdapter(cache_dir=Path(tmp), provider_fetcher=mock.Mock(side_effect=AssertionError))
            adapter._save_success(trade_date, result())
            cached = adapter._cached_result(trade_date, expected, now, close_final=False)
            self.assertEqual({"000852": 6123.45}, cached.benchmark_closes)
            self.assertFalse(adapter._bundle_path(trade_date).with_suffix(".tmp.pkl").exists())

            bundle = pd.read_pickle(adapter._bundle_path(trade_date))
            bundle["trade_date"] = "2026-08-06"
            pd.to_pickle(bundle, adapter._bundle_path(trade_date))
            self.assertIsNone(adapter._cached_result(trade_date, expected, now, close_final=False))

            adapter._bundle_path(trade_date).write_bytes(b"corrupted")
            corrupted = adapter._cached_result(trade_date, expected, now, close_final=False)
            self.assertEqual("provider_failed", corrupted.status)
            self.assertFalse(corrupted.from_cache)

        with tempfile.TemporaryDirectory() as tmp:
            adapter = QuoteSnapshotAdapter(cache_dir=Path(tmp), provider_fetcher=mock.Mock(side_effect=AssertionError))
            adapter._save_success(trade_date, result(observed_at=now - dt.timedelta(minutes=11)))
            stale = adapter._cached_result(trade_date, expected, now, close_final=False)
            self.assertEqual("snapshot_stale", stale.status)

        with tempfile.TemporaryDirectory() as tmp:
            adapter = QuoteSnapshotAdapter(cache_dir=Path(tmp), provider_fetcher=mock.Mock(side_effect=AssertionError))
            adapter._save_success(trade_date, result(frame=full_frame.iloc[:1].copy()))
            low = adapter._cached_result(trade_date, expected, now, close_final=False)
            self.assertEqual("coverage_below_threshold", low.status)


if __name__ == "__main__":
    unittest.main()
