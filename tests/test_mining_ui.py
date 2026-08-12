import unittest
import tempfile
from pathlib import Path
import sqlite3
import datetime as dt
import time
from unittest import mock

import pandas as pd

from tests._mining_test_helpers import create_sample_market_dbs, trading_days


class MiningUiSmokeTests(unittest.TestCase):
    def test_followup_change_accepts_object_typed_sql_prices(self) -> None:
        from mining.streamlit_tabs.tab_scanner import _build_followups

        previous = pd.DataFrame(
            {
                "strategy_id": ["strong_trend", "second_launch"],
                "sec_code": ["600001", "600003"],
                "sec_name": ["Alpha", "Gamma"],
                "rank": [1, 1],
                "entry_price": pd.Series([10.0, 20.0], dtype="object"),
            }
        )

        result = _build_followups(
            previous,
            {"600001": 11.0, "600003": 19.0},
            "snapshot_usable",
        )

        self.assertEqual(result["sec_code"].tolist(), ["600003", "600001"])
        self.assertEqual(result["today_change_pct"].tolist(), [-5.0, 10.0])

    def test_streamlit_tab_helpers_import(self) -> None:
        from mining.streamlit_tabs import (
            render_review_tab,
            render_rps_tab,
            render_scanner_tab,
        )

        self.assertTrue(callable(render_scanner_tab))
        self.assertTrue(callable(render_rps_tab))
        self.assertTrue(callable(render_review_tab))

    def test_opportunity_panel_loaders_return_today_and_yesterday_sections(self) -> None:
        from mining.streamlit_tabs.tab_scanner import (
            load_latest_opportunities,
            load_previous_day_followups,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)

            today_df, today_date = load_latest_opportunities(
                base_dir=base,
                trade_date=dates["next_trade_date"],
                use_intraday=False,
            )
            follow_df, previous_date, current_date = load_previous_day_followups(
                base_dir=base,
                trade_date=dates["next_trade_date"],
                use_intraday=False,
            )

        self.assertEqual(today_date, dates["next_trade_date"])
        self.assertTrue(today_df.empty)
        self.assertEqual(previous_date, dates["target_trade_date"])
        self.assertEqual(current_date, dates["next_trade_date"])
        self.assertTrue(follow_df.empty)

    def test_opportunity_panel_uses_intraday_snapshot_for_today_and_followups(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import (
            load_latest_opportunities,
            load_previous_day_followups,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            snapshot_today = dates["next_trade_date"]

            def fake_snapshot_loader() -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "code": "600001",
                            "close": 16.8,
                            "pre_close": 15.6,
                            "open": 16.0,
                            "high": 18.1,
                            "low": 15.9,
                            "volume": 12_000_000,
                            "amount": 2_100_000_000,
                            "change_pct": (16.8 / 15.6 - 1.0) * 100.0,
                        },
                        {
                            "code": "300001",
                            "close": 27.8,
                            "pre_close": 25.3,
                            "open": 26.2,
                            "high": 29.5,
                            "low": 26.1,
                            "volume": 15_000_000,
                            "amount": 2_300_000_000,
                            "change_pct": (27.8 / 25.3 - 1.0) * 100.0,
                        },
                        {
                            "code": "600003",
                            "close": 31.2,
                            "pre_close": 31.02,
                            "open": 31.05,
                            "high": 31.3,
                            "low": 30.9,
                            "volume": 8_000_000,
                            "amount": 950_000_000,
                            "change_pct": (31.2 / 31.02 - 1.0) * 100.0,
                        },
                    ]
                )

            today_df, today_date = load_latest_opportunities(
                base_dir=base,
                trade_date=snapshot_today,
                use_intraday=True,
                snapshot_loader=fake_snapshot_loader,
            )
            follow_df, previous_date, current_date = load_previous_day_followups(
                base_dir=base,
                trade_date=snapshot_today,
                use_intraday=True,
                snapshot_loader=fake_snapshot_loader,
            )

        self.assertEqual(today_date, snapshot_today)
        self.assertTrue(today_df.empty)
        self.assertEqual(previous_date, dates["target_trade_date"])
        self.assertEqual(current_date, snapshot_today)
        self.assertTrue(follow_df.empty)

    def test_snapshot_universe_derives_change_pct_when_cached_bar_has_no_pct_column(self) -> None:
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import _snapshot_universe

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            raw_cached_bar = pd.DataFrame(
                [
                    {
                        "sec_code": "600001",
                        "sec_name": "Cached Bar",
                        "open": 16.0,
                        "high": 18.1,
                        "low": 15.9,
                        "close": 16.8,
                        "pre_close": 15.6,
                        "volume": 12_000_000,
                        "amount": 2_100_000_000,
                    }
                ]
            )
            conn = connect(base_dir=base)
            try:
                universe = _snapshot_universe(conn, dates["next_trade_date"], raw_cached_bar)
            finally:
                conn.close()

        self.assertEqual(universe.loc[0, "sec_code"], "600001")
        self.assertAlmostEqual(float(universe.loc[0, "change_pct"]), (16.8 / 15.6 - 1.0) * 100.0)

    def test_opportunity_panel_prefers_latest_quotes_outside_trading_hours(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import (
            load_latest_opportunities,
            load_previous_day_followups,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            latest_quote_date = "2026-04-13"
            stock_conn = sqlite3.connect(base / "a_share_mvp.db")
            try:
                stock_conn.execute(
                    """
                    DELETE FROM kline_daily
                    WHERE sec_type IN ('stock', 'index') AND trade_date > ?
                    """,
                    (dates["target_trade_date"],),
                )
                stock_conn.commit()
            finally:
                stock_conn.close()

            def fake_latest_quotes() -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "code": "600001",
                            "close": 16.3,
                            "pre_close": 15.6,
                            "open": 15.8,
                            "high": 16.6,
                            "low": 15.7,
                            "volume": 11_000_000,
                            "amount": 1_900_000_000,
                            "change_pct": (16.3 / 15.6 - 1.0) * 100.0,
                        }
                    ]
                )

            today_df, today_date = load_latest_opportunities(
                base_dir=base,
                trade_date=latest_quote_date,
                use_intraday=False,
                prefer_latest_quotes=True,
                snapshot_loader=fake_latest_quotes,
            )
            follow_df, previous_date, current_date = load_previous_day_followups(
                base_dir=base,
                trade_date=latest_quote_date,
                use_intraday=False,
                prefer_latest_quotes=True,
                snapshot_loader=fake_latest_quotes,
            )

        self.assertEqual(today_date, latest_quote_date)
        self.assertFalse(today_df.empty)
        self.assertEqual(current_date, latest_quote_date)
        self.assertEqual(previous_date, dates["target_trade_date"])
        self.assertTrue(follow_df.empty)

    def test_after_close_prefers_latest_close_data_over_quotes_when_close_is_available(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import (
            load_latest_opportunities,
            load_previous_day_followups,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)

            def fake_latest_quotes() -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "code": "600001",
                            "close": 99.9,
                            "pre_close": 15.6,
                            "open": 99.9,
                            "high": 99.9,
                            "low": 99.9,
                            "volume": 0,
                            "amount": 0,
                            "change_pct": 0.0,
                        }
                    ]
                )

            today_df, today_date = load_latest_opportunities(
                base_dir=base,
                trade_date=dates["next_trade_date"],
                use_intraday=False,
                prefer_latest_quotes=True,
                fallback_trade_date=dates["next_trade_date"],
                snapshot_loader=fake_latest_quotes,
            )
            follow_df, previous_date, current_date = load_previous_day_followups(
                base_dir=base,
                trade_date=dates["next_trade_date"],
                use_intraday=False,
                prefer_latest_quotes=True,
                fallback_trade_date=dates["next_trade_date"],
                snapshot_loader=fake_latest_quotes,
            )

        self.assertEqual(today_date, dates["next_trade_date"])
        self.assertTrue(today_df.empty)
        self.assertEqual(previous_date, dates["target_trade_date"])
        self.assertEqual(current_date, dates["next_trade_date"])
        self.assertTrue(follow_df.empty)

    def test_force_latest_quotes_uses_quotes_even_when_close_is_available(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs import tab_scanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)

            def fake_latest_quotes() -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "code": "600001",
                            "close": 16.8,
                            "pre_close": 15.6,
                            "open": 16.0,
                            "high": 18.1,
                            "low": 15.9,
                            "volume": 12_000_000,
                            "amount": 2_100_000_000,
                            "change_pct": (16.8 / 15.6 - 1.0) * 100.0,
                        },
                        {
                            "code": "300001",
                            "close": 27.8,
                            "pre_close": 25.3,
                            "open": 26.2,
                            "high": 29.5,
                            "low": 26.1,
                            "volume": 15_000_000,
                            "amount": 2_300_000_000,
                            "change_pct": (27.8 / 25.3 - 1.0) * 100.0,
                        },
                        {
                            "code": "600003",
                            "close": 31.2,
                            "pre_close": 31.02,
                            "open": 31.05,
                            "high": 31.3,
                            "low": 30.9,
                            "volume": 8_000_000,
                            "amount": 950_000_000,
                            "change_pct": (31.2 / 31.02 - 1.0) * 100.0,
                        },
                    ]
                )

            with mock.patch.object(
                tab_scanner,
                "_load_close_opportunities",
                side_effect=AssertionError("force_latest_quotes should not fall back to close data"),
            ):
                today_df, today_date = tab_scanner.load_latest_opportunities(
                    base_dir=base,
                    trade_date=dates["next_trade_date"],
                    use_intraday=False,
                    prefer_latest_quotes=True,
                    force_latest_quotes=True,
                    fallback_trade_date=dates["next_trade_date"],
                    snapshot_loader=fake_latest_quotes,
                )
            follow_df, previous_date, current_date = tab_scanner.load_previous_day_followups(
                base_dir=base,
                trade_date=dates["next_trade_date"],
                use_intraday=False,
                prefer_latest_quotes=True,
                force_latest_quotes=True,
                fallback_trade_date=dates["next_trade_date"],
                snapshot_loader=fake_latest_quotes,
            )

        self.assertEqual(today_date, dates["next_trade_date"])
        self.assertEqual(previous_date, dates["target_trade_date"])
        self.assertEqual(current_date, dates["next_trade_date"])
        self.assertTrue(follow_df.empty)

    def test_intraday_momentum_prefilter_limits_history_lookup_codes(self) -> None:
        import pandas as pd

        from mining.scanners import momentum_breakout
        from mining.streamlit_tabs import tab_scanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            original_loader = momentum_breakout._load_stock_history
            requested_codes: list[set[str]] = []

            def fake_latest_quotes() -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "code": "600001",
                            "close": 16.8,
                            "pre_close": 15.6,
                            "open": 16.0,
                            "high": 18.1,
                            "low": 15.9,
                            "volume": 12_000_000,
                            "amount": 2_100_000_000,
                            "change_pct": (16.8 / 15.6 - 1.0) * 100.0,
                        },
                        {
                            "code": "300001",
                            "close": 27.8,
                            "pre_close": 25.3,
                            "open": 26.2,
                            "high": 29.5,
                            "low": 26.1,
                            "volume": 15_000_000,
                            "amount": 2_300_000_000,
                            "change_pct": (27.8 / 25.3 - 1.0) * 100.0,
                        },
                        {
                            "code": "600003",
                            "close": 31.2,
                            "pre_close": 31.02,
                            "open": 31.05,
                            "high": 31.3,
                            "low": 30.9,
                            "volume": 8_000_000,
                            "amount": 950_000_000,
                            "change_pct": (31.2 / 31.02 - 1.0) * 100.0,
                        },
                    ]
                )

            def recording_loader(conn, sec_codes, dates_arg):
                requested_codes.append(set(sec_codes))
                return original_loader(conn, sec_codes, dates_arg)

            with mock.patch.object(
                momentum_breakout,
                "_load_stock_history",
                side_effect=recording_loader,
            ):
                today_df, today_date = tab_scanner.load_latest_opportunities(
                    base_dir=base,
                    trade_date=dates["next_trade_date"],
                    use_intraday=True,
                    snapshot_loader=fake_latest_quotes,
                )

        self.assertEqual(today_date, dates["next_trade_date"])
        self.assertTrue(today_df.empty)
        self.assertTrue(requested_codes)
        self.assertEqual(requested_codes[0], {"600001", "300001"})

    def test_intraday_momentum_prefilter_excludes_weak_shadow_pullback(self) -> None:
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner
        from mining.streamlit_tabs.tab_scanner import _prefilter_momentum_universe

        params = MomentumBreakoutScanner().params.copy()
        params["early_strength_upper_shadow_min"] = 99.0
        universe = pd.DataFrame(
            [
                {
                    "sec_code": "600010",
                    "sec_name": "Mild Pullback",
                    "open": 10.0,
                    "high": 11.0,
                    "close": 10.4,
                    "amount": 2_000_000_000,
                    "change_pct": -2.5,
                },
                {
                    "sec_code": "600011",
                    "sec_name": "Weak Pullback",
                    "open": 10.0,
                    "high": 11.0,
                    "close": 10.4,
                    "amount": 2_000_000_000,
                    "change_pct": -3.5,
                },
            ]
        )

        filtered = _prefilter_momentum_universe(universe, params)

        self.assertEqual(set(filtered["sec_code"]), {"600010"})
    def test_latest_quote_price_map_filters_to_requested_codes(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import _load_latest_quote_price_map

        def fake_latest_quotes() -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {"code": "600001", "close": 16.8},
                    {"code": "300001", "close": 27.8},
                    {"code": "600003", "close": 31.2},
                ]
            )

        prices = _load_latest_quote_price_map(
            snapshot_loader=fake_latest_quotes,
            sec_codes=["600001"],
        )

        self.assertEqual(prices, {"600001": 16.8})

    def test_prepare_today_display_tolerates_legacy_rows_without_new_fields(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import _prepare_today_display

        legacy_df = pd.DataFrame(
            [
                {
                    "strategy_id": "momentum_breakout",
                    "sec_code": "300001",
                    "sec_name": "Legacy",
                    "rank": 1,
                    "change_pct": 6.5,
                }
            ]
        )

        display_df = _prepare_today_display(legacy_df)

        self.assertEqual(
            list(display_df.columns),
            ["来源", "排名", "代码", "名称", "涨幅%", "最高比开盘%", "最高比收盘%", "强度分", "路径"],
        )
        self.assertEqual(display_df.iloc[0]["来源"], "异动")
        self.assertTrue(pd.isna(display_df.iloc[0]["最高比开盘%"]))
        self.assertTrue(pd.isna(display_df.iloc[0]["最高比收盘%"]))

    def test_persisted_scanner_display_includes_new_strategy_labels(self) -> None:
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import _load_persisted_candidates, _prepare_today_display

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    INSERT INTO strategy_runs (
                      run_id, strategy_id, version, trade_date, run_at, universe_size,
                      n_candidates, status, error_msg
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, "manual", "v1.0", trade_date, "2026-04-10 00:00:00", 4, 4, "ok", None),
                )
                rows = [
                    ("momentum_breakout", "600001", "Alpha", 1),
                    ("rps_stock_top20", "300001", "Beta", 2),
                    ("trend_embryo", "601001", "Embryo", 3),
                    ("true_leader", "600003", "Leader", 4),
                    ("second_launch", "600005", "Second", 5),
                    ("launch_burst", "600006", "Launch", 6),
                ]
                for strategy_id, sec_code, sec_name, rank in rows:
                    conn.execute(
                        """
                        INSERT INTO candidates (
                          run_id, strategy_id, version, trade_date, sec_type, sec_code,
                          sec_name, entry_price, features_json, rank
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (1, strategy_id, "v1.0", trade_date, "stock", sec_code, sec_name, 10.0, "{}", rank),
                    )
                conn.commit()

                loaded = _load_persisted_candidates(conn, trade_date)
            finally:
                conn.close()

        self.assertEqual(
            set(loaded["strategy_id"]),
            {
                "momentum_breakout",
                "rps_stock_top20",
                "trend_embryo",
                "true_leader",
                "launch_burst",
            },
        )
        display = _prepare_today_display(loaded)
        self.assertIn("强趋势胚子", set(display["来源"]))
        self.assertIn("真龙/中军", set(display["来源"]))
        self.assertIn("主升启动", set(display["来源"]))

    def test_formal_loader_reads_only_current_complete_batch(self) -> None:
        from types import SimpleNamespace

        from mining.capabilities import formal_definitions
        from mining.candidate_persistence import (
            CapabilityResult,
            PullbackStateRow,
            migrate_selection_batch_schema,
            persist_close_final_batch,
        )
        from mining.db import connect
        from mining.scanners import Candidate
        from mining.streamlit_tabs.tab_scanner import (
            _load_formal_capability_candidates,
            _persisted_candidate_dates,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            trade_date = "2026-08-05"
            conn = connect(base_dir=base)
            try:
                migrate_selection_batch_schema(conn)
                context = SimpleNamespace(
                    trade_date=trade_date,
                    mode="close_final",
                    as_of="2026-08-05 15:10:00",
                    price_as_of=trade_date,
                    input_fingerprint="formal-ui-test",
                )
                results = []
                for index, definition in enumerate(formal_definitions(), start=1):
                    candidate = Candidate(
                        definition.strategy_id,
                        definition.version,
                        trade_date,
                        "stock",
                        f"{600000 + index:06d}",
                        f"正式{index}",
                        10.0,
                        {"reference_price": 10.0},
                        1,
                    )
                    state_rows = (
                        (PullbackStateRow(candidate.sec_code, "再启动", "P60"),)
                        if definition.strategy_id == "second_launch"
                        else ()
                    )
                    results.append(
                        CapabilityResult(
                            definition.strategy_id,
                            (candidate,),
                            6,
                            state_rows=state_rows,
                        )
                    )
                persist_close_final_batch(conn, context, results)
                legacy_run = conn.execute(
                    """
                    INSERT INTO strategy_runs (
                      strategy_id, version, trade_date, run_at, universe_size, n_candidates, status, error_msg
                    ) VALUES ('strong_trend', 'v2.0', ?, '2026-08-05 15:01:00', 1, 1, 'ok', NULL)
                    """,
                    (trade_date,),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO candidates (
                      run_id, strategy_id, version, trade_date, sec_type, sec_code,
                      sec_name, entry_price, features_json, rank
                    ) VALUES (?, 'strong_trend', 'v2.0', ?, 'stock', '000001', '旧版本', 10.0, '{}', 1)
                    """,
                    (legacy_run, trade_date),
                )
                conn.commit()
                loaded = _load_formal_capability_candidates(conn, trade_date)
                persisted_dates = _persisted_candidate_dates(conn)
            finally:
                conn.close()

        self.assertEqual({"v2.5"}, set(loaded["version"]))
        self.assertNotIn("000001", set(loaded["sec_code"]))
        self.assertEqual([trade_date], persisted_dates)

    def test_formal_view_uses_independent_dates_and_hides_rows_on_mismatch(self) -> None:
        from types import SimpleNamespace

        from mining.capabilities import formal_definitions
        from mining.candidate_persistence import (
            CapabilityResult,
            PullbackStateRow,
            migrate_selection_batch_schema,
            persist_close_final_batch,
        )
        from mining.db import connect
        from mining.scanners import Candidate
        from mining.streamlit_tabs.tab_scanner import _formal_capability_view

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                migrate_selection_batch_schema(conn)
                context = SimpleNamespace(
                    trade_date=trade_date,
                    mode="close_final",
                    as_of=f"{trade_date}T15:10:00+08:00",
                    price_as_of=trade_date,
                    input_fingerprint="date-evidence-test",
                )
                results = []
                for index, definition in enumerate(formal_definitions(), start=1):
                    candidate = Candidate(
                        definition.strategy_id,
                        definition.version,
                        trade_date,
                        "stock",
                        f"{600000 + index:06d}",
                        f"Formal {index}",
                        10.0,
                        {"reference_price": 10.0},
                        1,
                    )
                    states = (
                        (PullbackStateRow(candidate.sec_code, "再启动", "P60"),)
                        if definition.strategy_id == "second_launch"
                        else ()
                    )
                    results.append(CapabilityResult(definition.strategy_id, (candidate,), 6, state_rows=states))
                persist_close_final_batch(conn, context, results)
                now = dt.datetime.fromisoformat(f"{trade_date}T18:00:00+08:00")

                aligned_rows, aligned_status, aligned = _formal_capability_view(
                    conn,
                    trade_date,
                    selected_trade_date=trade_date,
                    now=now,
                )
                mismatched_rows, mismatched_status, mismatched = _formal_capability_view(
                    conn,
                    trade_date,
                    selected_trade_date="2099-01-01",
                    now=now,
                )
            finally:
                conn.close()

        self.assertFalse(aligned_rows.empty)
        self.assertFalse(aligned_status.empty)
        self.assertEqual(aligned["date_status"], "aligned")
        self.assertEqual(aligned["selected_trade_date"], trade_date)
        self.assertEqual(aligned["effective_trade_date"], trade_date)
        self.assertEqual(aligned["formal_trade_date"], trade_date)
        self.assertTrue(mismatched_rows.empty)
        self.assertEqual(mismatched["date_status"], "date_mismatch")
        self.assertEqual(mismatched["selected_trade_date"], "2099-01-01")
        self.assertEqual(mismatched["effective_trade_date"], trade_date)
        self.assertEqual(mismatched["formal_trade_date"], trade_date)
        self.assertEqual(set(mismatched_status["availability"]), {"date_mismatch"})

    def test_usable_close_without_batch_waits_without_loading_snapshot(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import _formal_capability_view

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                migrate_selection_batch_schema(conn)
                calls = 0

                def unexpected_snapshot_loader() -> pd.DataFrame:
                    nonlocal calls
                    calls += 1
                    raise AssertionError("close view must not load a provisional snapshot")

                rows, status, evidence = _formal_capability_view(
                    conn,
                    dates["target_trade_date"],
                    selected_trade_date=dates["target_trade_date"],
                    now=dt.datetime(2026, 4, 9, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    snapshot_loader=unexpected_snapshot_loader,
                )
            finally:
                conn.close()

        self.assertTrue(rows.empty)
        self.assertEqual(calls, 0)
        self.assertEqual(evidence["result_kind"], "waiting_for_formal_batch")
        self.assertEqual(set(status["availability"]), {"waiting_for_formal_batch"})

    def test_current_intraday_snapshot_is_provisional_shared_and_nonpersistent(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema
        from mining.db import connect
        from mining.selection_runtime import SelectionRuntime
        from mining.streamlit_tabs.tab_scanner import _build_cached_snapshot_loader, _formal_capability_view

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            intraday_date = "2026-04-17"
            now = dt.datetime(2026, 4, 17, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
            conn = connect(base_dir=base)
            try:
                migrate_selection_batch_schema(conn)
                snapshot = pd.read_sql_query(
                    """
                    SELECT sec_code, open, high, low, close, pre_close, change, change_pct, volume, amount
                    FROM ash.kline_daily WHERE sec_type='stock' AND trade_date=?
                    """,
                    conn,
                    params=[dates["target_trade_date"]],
                )
                before = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("selection_batches", "strategy_runs", "candidates", "pullback_state_history")
                }
                calls = 0

                def source_loader() -> pd.DataFrame:
                    nonlocal calls
                    calls += 1
                    return snapshot.copy()

                loader = _build_cached_snapshot_loader(source_loader)
                runtime = SelectionRuntime()
                first_rows, first_status, first = _formal_capability_view(
                    conn,
                    intraday_date,
                    selected_trade_date=intraday_date,
                    now=now,
                    runtime=runtime,
                    snapshot_loader=loader,
                    intraday_enabled=True,
                )
                repeated_rows, repeated_status, repeated = _formal_capability_view(
                    conn,
                    intraday_date,
                    selected_trade_date=intraday_date,
                    now=now,
                    runtime=runtime,
                    snapshot_loader=loader,
                    intraday_enabled=True,
                )
                after = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in before
                }
            finally:
                conn.close()

        self.assertEqual(calls, 1)
        self.assertEqual(first["mode"], "intraday_snapshot")
        self.assertEqual(first["result_kind"], "provisional")
        self.assertEqual(first["formal_batch_status"], "provisional")
        self.assertEqual(set(first_status["capability"]), {"A", "B", "C", "D", "E"})
        self.assertEqual(set(first_status["availability"]), {"provisional"})
        self.assertEqual(first["snapshot_source"], "injected_loader")
        self.assertEqual(first["date_status"], "aligned")
        self.assertEqual(repeated["result_kind"], "provisional")
        self.assertEqual(len(first_rows), len(repeated_rows))
        self.assertEqual(set(repeated_status["availability"]), {"provisional"})
        self.assertEqual(after, before)

    def test_complete_formal_batch_takes_priority_over_current_snapshot(self) -> None:
        from mining.candidate_persistence import migrate_selection_batch_schema
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import _formal_capability_view
        from run_daily import execute_formal_only_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                migrate_selection_batch_schema(conn)
            finally:
                conn.close()
            execute_formal_only_pipeline(base, trade_date)
            conn = connect(base_dir=base)
            try:
                calls = 0

                def unexpected_snapshot_loader() -> pd.DataFrame:
                    nonlocal calls
                    calls += 1
                    raise AssertionError("complete formal batch must win")

                rows, status, evidence = _formal_capability_view(
                    conn,
                    trade_date,
                    selected_trade_date=trade_date,
                    now=dt.datetime(2026, 4, 9, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    snapshot_loader=unexpected_snapshot_loader,
                    intraday_enabled=True,
                )
            finally:
                conn.close()

        self.assertEqual(calls, 0)
        self.assertEqual(evidence["result_kind"], "persisted")
        self.assertEqual(evidence["formal_batch_status"], "complete")
        self.assertFalse(status.empty)
        self.assertTrue(rows.empty or "result_kind" not in rows.columns)

    def test_launch_burst_display_exposes_decision_features(self) -> None:
        from mining.streamlit_tabs.tab_scanner import _prepare_launch_display

        display = _prepare_launch_display(
            pd.DataFrame(
                [
                    {
                        "rank": 1,
                        "sec_code": "002821",
                        "sec_name": "Launch",
                        "pct": 0.0767,
                        "volume_ratio": 1.586,
                        "sigma_multiple": 3.024,
                        "cluster_days": 14,
                        "cluster_width": 0.0429,
                    }
                ]
            )
        )

        self.assertEqual(
            list(display.columns),
            ["排名", "代码", "名称", "涨幅%", "量比", "σ倍数", "横盘天数", "均线簇宽度%"],
        )
        self.assertAlmostEqual(display.iloc[0]["涨幅%"], 7.67)
        self.assertEqual(display.iloc[0]["横盘天数"], 14)

    def test_scanner_panel_market_regime_summary_degrades_without_crashing(self) -> None:
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import _format_market_regime, _load_market_regime_summary

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                summary = _load_market_regime_summary(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertTrue(summary["available"])
        self.assertIn(str(summary["light"]), {"RED", "YELLOW", "GREEN"})
        self.assertIn("上涨家数占比", _format_market_regime(summary))

        broken_conn = sqlite3.connect(":memory:")
        try:
            broken = _load_market_regime_summary(broken_conn, dates["target_trade_date"])
        finally:
            broken_conn.close()
        self.assertFalse(broken["available"])
        self.assertIn("市场灯不可用", _format_market_regime(broken))

    def test_scanner_panel_strategy_run_status_uses_latest_row_and_handles_empty(self) -> None:
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import _load_strategy_run_status

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                self.assertTrue(_load_strategy_run_status(conn, trade_date).empty)
                conn.execute(
                    """
                    INSERT INTO strategy_runs (
                      strategy_id, version, trade_date, run_at, universe_size,
                      n_candidates, status, error_msg
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("launch_burst", "v1.0", trade_date, "2026-04-10 10:00:00", 10, 2, "ok", None),
                )
                conn.execute(
                    """
                    INSERT INTO strategy_runs (
                      strategy_id, version, trade_date, run_at, universe_size,
                      n_candidates, status, error_msg
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("launch_burst", "v1.0", trade_date, "2026-04-10 11:00:00", 12, 0, "empty", None),
                )
                status = _load_strategy_run_status(conn, trade_date)
            finally:
                conn.close()

        self.assertEqual(len(status), 1)
        self.assertEqual(status.iloc[0]["strategy_id"], "launch_burst")
        self.assertEqual(status.iloc[0]["status"], "empty")
        self.assertEqual(int(status.iloc[0]["n_candidates"]), 0)

    def test_scanner_panel_watchlist_state_dates_and_extend_rows_are_visible(self) -> None:
        from mining.db import connect
        from mining.streamlit_tabs.tab_scanner import (
            _load_last_watchlist_state_date,
            _playbook_cards_for_row,
            _watchlist_state_rows,
        )
        from mining.watchlist import STATE_EXTEND, STATE_READY, STATE_RETRIGGER

        watchlist = pd.DataFrame(
            [
                {"sec_code": "600001", "sec_name": "Alpha", "state": STATE_EXTEND, "triage": 2.0},
                {"sec_code": "600002", "sec_name": "Beta", "state": STATE_EXTEND, "triage": 5.0},
                {"sec_code": "600003", "sec_name": "Ready", "state": STATE_READY, "triage": 9.0},
            ]
        )
        extend = _watchlist_state_rows(watchlist, STATE_EXTEND, top_n=1)

        self.assertEqual(extend["sec_code"].tolist(), ["600002"])
        cards = _playbook_cards_for_row(
            extend.iloc[0],
            lookup=lambda key: [{"id": "extend-card", "title": "Extend"}] if key == STATE_EXTEND else [],
        )
        self.assertEqual([card["id"] for card in cards], ["extend-card"])

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    INSERT INTO watchlist_snapshots (
                      snapshot_date, sec_code, sec_name, state, triage, entry_price, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("2026-04-09", "600003", "Ready", STATE_READY, 1.0, 10.0, "2026-04-09 18:00:00"),
                )
                conn.execute(
                    """
                    INSERT INTO watchlist_snapshots (
                      snapshot_date, sec_code, sec_name, state, triage, entry_price, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("2026-04-10", "600004", "Trigger", STATE_RETRIGGER, 1.0, 10.0, "2026-04-10 18:00:00"),
                )
                ready_date = _load_last_watchlist_state_date(conn, STATE_READY)
                trigger_date = _load_last_watchlist_state_date(conn, STATE_RETRIGGER)
            finally:
                conn.close()

        self.assertEqual(ready_date, "2026-04-09")
        self.assertEqual(trigger_date, "2026-04-10")

    def test_last_completed_trade_date_before_open_returns_previous_trade_day(self) -> None:
        from app_panel import last_completed_trade_date_cn

        result = last_completed_trade_date_cn(dt.datetime(2026, 4, 14, 8, 47, 56), 8)

        self.assertEqual(result.strftime("%Y-%m-%d"), "2026-04-13")

    def test_opportunity_plan_uses_previous_day_before_open(self) -> None:
        from app_panel import resolve_opportunity_runtime

        plan = resolve_opportunity_runtime(dt.datetime(2026, 4, 14, 9, 10, 0), 8)

        self.assertEqual(plan["trade_date"], "2026-04-13")
        self.assertEqual(plan["fallback_trade_date"], "2026-04-13")
        self.assertFalse(plan["force_latest_quotes"])
        self.assertFalse(plan["use_intraday"])

    def test_opportunity_plan_uses_today_quotes_after_open_and_after_close(self) -> None:
        from app_panel import resolve_opportunity_runtime

        trading_plan = resolve_opportunity_runtime(dt.datetime(2026, 4, 14, 10, 5, 0), 8)
        after_close_plan = resolve_opportunity_runtime(dt.datetime(2026, 4, 14, 15, 10, 0), 8)

        self.assertEqual(trading_plan["trade_date"], "2026-04-14")
        self.assertTrue(trading_plan["force_latest_quotes"])
        self.assertTrue(trading_plan["use_intraday"])
        self.assertEqual(after_close_plan["trade_date"], "2026-04-14")
        self.assertTrue(after_close_plan["force_latest_quotes"])
        self.assertFalse(after_close_plan["use_intraday"])

    def test_call_with_timeout_returns_none_quickly_for_slow_probe(self) -> None:
        from app_panel import _call_with_timeout

        started = time.time()
        result = _call_with_timeout(0.05, time.sleep, 0.2)
        elapsed = time.time() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 0.2)

    def test_discover_data_dates_bundle_keeps_source_t1_when_local_db_lags(self) -> None:
        from app_panel import discover_data_dates_bundle

        discover_data_dates_bundle.clear()
        try:
            with mock.patch(
                "app_panel.last_completed_trade_date_cn",
                return_value=pd.Timestamp("2026-04-22"),
            ), mock.patch(
                "app_panel.get_prev_trade_days",
                return_value=["2026-04-22", "2026-04-21", "2026-04-20"],
            ) as prev_days:
                bundle = discover_data_dates_bundle(
                    None,
                    8,
                    local_last_dates={
                        "stock": "2026-04-21",
                        "index": "2026-04-21",
                        "concept": "2026-04-21",
                        "etf": "2026-04-21",
                    },
                )
        finally:
            discover_data_dates_bundle.clear()

        self.assertEqual(bundle["calendar_last_trade_day"], "2026-04-22")
        self.assertEqual(bundle["remote_latest_stock_close_day"], "2026-04-22")
        self.assertEqual(bundle["remote_latest_concept_close_day"], "2026-04-22")
        self.assertEqual(bundle["t1_close_day"], "2026-04-22")
        self.assertEqual(bundle["t2_close_day"], "2026-04-21")
        prev_days.assert_called_once_with("2026-04-22", n_back=2)

    def test_panel_close_date_is_not_downgraded_by_concept_lag(self) -> None:
        from app_panel import resolve_panel_close_date

        panel_dt = resolve_panel_close_date(
            requested_close_day="2026-04-22",
            close_ref_day="2026-04-22",
            stock_day="2026-04-22",
            index_day="2026-04-22",
            concept_day="2026-04-21",
        )

        self.assertEqual(panel_dt, pd.Timestamp("2026-04-22"))

    def test_panel_close_date_falls_back_when_stock_or_index_lags(self) -> None:
        from app_panel import resolve_panel_close_date

        panel_dt = resolve_panel_close_date(
            requested_close_day="2026-04-22",
            close_ref_day="2026-04-22",
            stock_day="2026-04-21",
            index_day="2026-04-22",
            concept_day="2026-04-22",
        )

        self.assertEqual(panel_dt, pd.Timestamp("2026-04-21"))

    def test_panel_close_date_uses_actual_stock_index_coverage_not_latest_date(self) -> None:
        from app_panel import resolve_available_panel_close_date

        stock_df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-04-21", "2026-04-23"]),
                "sec_code": ["600001", "600001"],
            }
        )
        index_df = pd.DataFrame(
            [
                {"trade_date": "2026-04-21", "sec_code": code}
                for code in ("000001", "399001", "000300", "000852")
            ]
            + [
                {"trade_date": "2026-04-23", "sec_code": code}
                for code in ("000001", "399001", "000300", "000852")
            ]
        )

        resolved, missing_requested, available = resolve_available_panel_close_date(
            "2026-04-22",
            stock_df,
            index_df,
            ["000001", "399001", "000300", "000852"],
        )

        self.assertEqual(resolved, pd.Timestamp("2026-04-21"))
        self.assertTrue(missing_requested)
        self.assertEqual(available, [pd.Timestamp("2026-04-21"), pd.Timestamp("2026-04-23")])

    def test_panel_date_and_metric_coverage_exclude_unknown_fields(self) -> None:
        from app_panel import resolve_available_panel_close_date, sentiment_metric_coverage

        stock_df = pd.DataFrame(
            {
                "trade_date": ["2026-04-21", "2026-04-21"],
                "sec_code": ["600001", "600002"],
                "change_pct": [1.0, None],
                "amount": [1000.0, None],
            }
        )
        index_df = pd.DataFrame(
            [
                {"trade_date": "2026-04-21", "sec_code": code}
                for code in ("000001", "399001", "000300", "000852")
            ]
        )
        readiness = {"2026-04-21": {"market_data_ready": True, "screening_ready": False}}
        resolved, missing, available = resolve_available_panel_close_date(
            "2026-04-21", stock_df, index_df, readiness_by_date=readiness
        )

        self.assertEqual(resolved, pd.Timestamp("2026-04-21"))
        self.assertFalse(missing)
        self.assertEqual(available, [pd.Timestamp("2026-04-21")])
        self.assertEqual(
            sentiment_metric_coverage(stock_df, "2026-04-21"),
            {"return_valid": 1, "amount_valid": 1, "hot_valid": 1, "expected": 2, "excluded": 1},
        )

    def test_metric_denominators_keep_return_amount_and_hot_inputs_separate(self) -> None:
        from app_panel import compute_daily_sentiment, sentiment_metric_coverage

        dates = [pd.Timestamp("2026-04-20"), pd.Timestamp("2026-04-21")]
        stock_df = pd.DataFrame(
            [
                {"trade_date": "2026-04-20", "sec_code": "600001", "change_pct": 0.0, "amount": 10.0, "close": 10.0},
                {"trade_date": "2026-04-20", "sec_code": "600002", "change_pct": 0.0, "amount": 20.0, "close": 20.0},
                {"trade_date": "2026-04-21", "sec_code": "600001", "change_pct": None, "amount": 100.0, "close": 11.0},
                {"trade_date": "2026-04-21", "sec_code": "600002", "change_pct": 2.0, "amount": None, "close": 21.0},
                {"trade_date": "2026-04-21", "sec_code": "600003", "change_pct": None, "amount": 300.0, "close": 5.0},
            ]
        )
        daily, _ = compute_daily_sentiment(
            stock_df,
            pd.DataFrame(),
            pd.DataFrame(),
            dates,
            {"B": 1.0},
            market_turnover=pd.Series([100.0, 120.0], index=dates),
        )
        row = daily.loc[daily["trade_date"].eq(pd.Timestamp("2026-04-21"))].iloc[0]

        self.assertAlmostEqual(float(row["allA_ew_ret"]), 0.06)
        self.assertAlmostEqual(float(row["hot_excess"]), 0.04)
        self.assertEqual(
            sentiment_metric_coverage(stock_df, "2026-04-21"),
            {"return_valid": 2, "amount_valid": 2, "hot_valid": 1, "expected": 3, "excluded": 2},
        )

    def test_close_mode_opportunity_runtime_follows_selected_close_day(self) -> None:
        from app_panel import resolve_selected_opportunity_runtime

        plan = resolve_selected_opportunity_runtime(
            effective_mode="close",
            panel_close_dt=pd.Timestamp("2026-04-21"),
            now_cn=dt.datetime(2026, 4, 23, 10, 5, 0),
            tz_offset_hours=8,
        )

        self.assertEqual(plan["trade_date"], "2026-04-21")
        self.assertEqual(plan["fallback_trade_date"], "2026-04-21")
        self.assertFalse(plan["force_latest_quotes"])
        self.assertFalse(plan["use_intraday"])

    def test_snapshot_close_catchup_is_allowed_before_open_for_previous_close(self) -> None:
        from app_panel import can_use_snapshot_for_close_target

        with mock.patch(
            "app_panel._today_trade_date_cn",
            return_value=pd.Timestamp("2026-04-23"),
        ), mock.patch(
            "app_panel.last_completed_trade_date_cn",
            return_value=pd.Timestamp("2026-04-22"),
        ):
            allowed = can_use_snapshot_for_close_target(
                "2026-04-22",
                now_cn=dt.datetime(2026, 4, 23, 8, 55, 0),
                tz_offset_hours=8,
            )

        self.assertTrue(allowed)

    def test_snapshot_close_catchup_does_not_backfill_yesterday_after_open(self) -> None:
        from app_panel import can_use_snapshot_for_close_target

        with mock.patch(
            "app_panel._today_trade_date_cn",
            return_value=pd.Timestamp("2026-04-23"),
        ), mock.patch(
            "app_panel.last_completed_trade_date_cn",
            return_value=pd.Timestamp("2026-04-22"),
        ):
            allowed = can_use_snapshot_for_close_target(
                "2026-04-22",
                now_cn=dt.datetime(2026, 4, 23, 9, 35, 0),
                tz_offset_hours=8,
            )

        self.assertFalse(allowed)

    def test_full_snapshot_normalization_excludes_non_shenzhen_shanghai_a_shares(self) -> None:
        from app_panel import normalize_a_spot_df_full

        raw = pd.DataFrame(
            [
                {"代码": "bj920000", "最新价": 16.73, "昨收": 16.66, "今开": 16.77, "最高": 16.82, "最低": 16.52, "成交量": 10, "成交额": 100, "涨跌幅": 0.42},
                {"代码": "sh600001", "最新价": 8.2, "昨收": 8.0, "今开": 8.1, "最高": 8.5, "最低": 8.0, "成交量": 20, "成交额": 200, "涨跌幅": 2.5},
                {"代码": "sz300001", "最新价": 12.1, "昨收": 12.0, "今开": 12.0, "最高": 12.4, "最低": 11.9, "成交量": 30, "成交额": 300, "涨跌幅": 0.83},
            ]
        )

        normalized, _ = normalize_a_spot_df_full(raw)

        self.assertEqual(set(normalized["sec_code"]), {"600001", "300001"})

    def test_target_close_backfill_uses_snapshot_instead_of_slow_daily_backfill(self) -> None:
        from app_panel import maybe_backfill_to_close_day

        local_before = {"stock": "2026-04-21", "index": "2026-04-21", "concept": "2026-04-22", "etf": "2026-04-21"}
        local_after = {"stock": "2026-04-22", "index": "2026-04-22", "concept": "2026-04-22", "etf": "2026-04-21"}
        with mock.patch("app_panel.can_use_snapshot_for_close_target", return_value=True), mock.patch(
            "app_panel.sync_close_snapshot_to_db_for_trade_date",
            return_value=(True, ["snapshot ok"]),
        ) as snapshot_sync, mock.patch(
            "app_panel.get_local_last_dates",
            return_value=local_after,
        ), mock.patch(
            "app_panel.run_cmd",
            side_effect=AssertionError("startup catch-up must not run slow BaoStock backfill"),
        ):
            result, logs = maybe_backfill_to_close_day(
                stock_db="a_share_mvp.db",
                concept_db=None,
                etf_db=None,
                target_day="2026-04-22",
                local_dates=local_before,
                include_concept=False,
                include_etf=False,
            )

        self.assertEqual(result, local_after)
        snapshot_sync.assert_called_once()
        self.assertTrue(any("[TARGET_STOCK_SNAPSHOT]" in line for line in logs))

    def test_close_snapshot_sync_writes_target_day_stock_and_index_rows(self) -> None:
        from app_panel import sync_close_snapshot_to_db_for_trade_date

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            create_sample_market_dbs(base)
            stock_db = str(base / "a_share_mvp.db")
            stock_codes = [f"sh600{idx:03d}" for idx in range(1000)]
            stock_codes.extend(f"sh601{idx:03d}" for idx in range(1000))
            stock_codes.append("sh603000")
            stock_rows = [
                {
                    "代码": code,
                    "名称": f"Stock{idx}",
                    "最新价": 10.0 + idx / 1000,
                    "昨收": 9.9 + idx / 1000,
                    "今开": 10.0,
                    "最高": 10.4,
                    "最低": 9.8,
                    "成交量": 1_000_000 + idx,
                    "成交额": 100_000_000 + idx,
                    "涨跌幅": 1.0,
                }
                for idx, code in enumerate(stock_codes)
            ]
            stock_rows.append(
                {"代码": "bj920000", "名称": "BJ", "最新价": 16.0, "昨收": 15.8, "今开": 16.0, "最高": 16.2, "最低": 15.7, "成交量": 1, "成交额": 1, "涨跌幅": 1.0}
            )
            index_rows = pd.DataFrame(
                [
                    {"代码": "000001", "最新价": 4100, "昨收": 4090, "成交额": 100_000_000_000, "涨跌幅": 0.24},
                    {"代码": "399001", "最新价": 15000, "昨收": 14900, "成交额": 120_000_000_000, "涨跌幅": 0.67},
                    {"代码": "000300", "最新价": 4800, "昨收": 4790, "成交额": 80_000_000_000, "涨跌幅": 0.21},
                    {"代码": "000852", "最新价": 8400, "昨收": 8390, "成交额": 70_000_000_000, "涨跌幅": 0.12},
                ]
            )

            with mock.patch("app_panel.ak_fetch_a_spot_sina", return_value=pd.DataFrame(stock_rows)), mock.patch(
                "app_panel.ak_fetch_index_spot_sina",
                return_value=index_rows,
            ):
                ok, logs = sync_close_snapshot_to_db_for_trade_date(
                    stock_db=stock_db,
                    concept_db=None,
                    trade_date="2026-04-22",
                    include_concept=False,
                )

            conn = sqlite3.connect(stock_db)
            try:
                stock_count = conn.execute(
                    "SELECT COUNT(*) FROM kline_daily WHERE sec_type='stock' AND trade_date='2026-04-22'"
                ).fetchone()[0]
                bj_count = conn.execute(
                    "SELECT COUNT(*) FROM kline_daily WHERE sec_type='stock' AND trade_date='2026-04-22' AND sec_code='920000'"
                ).fetchone()[0]
                index_count = conn.execute(
                    "SELECT COUNT(*) FROM kline_daily WHERE sec_type='index' AND trade_date='2026-04-22'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertTrue(ok, logs)
        self.assertEqual(stock_count, 2001)
        self.assertEqual(bj_count, 0)
        self.assertEqual(index_count, 4)

    def test_close_history_is_persisted_for_review_when_db_has_close_days(self) -> None:
        import sqlite3

        from mining.streamlit_tabs.tab_scanner import ensure_close_history_persisted

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)

            result = ensure_close_history_persisted(base_dir=base, up_to_trade_date=dates["target_trade_date"])

            conn = sqlite3.connect(base / "mining_mvp.db")
            try:
                latest = conn.execute(
                    """
                    SELECT MAX(trade_date)
                    FROM candidates
                    WHERE strategy_id IN ('momentum_breakout', 'rps_stock_top20')
                    """
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(latest, dates["target_trade_date"])
        self.assertEqual(result["latest_persisted"], dates["target_trade_date"])

    def test_daily_sync_also_pushes_new_close_day_into_mining_history(self) -> None:
        import sqlite3

        from app_panel import sync_mining_history_after_close_update
        from mining.streamlit_tabs.tab_scanner import ensure_close_history_persisted

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            ensure_close_history_persisted(base_dir=base, up_to_trade_date=dates["target_trade_date"])

            result = sync_mining_history_after_close_update(
                base_dir=str(base),
                up_to_trade_date=dates["next_trade_date"],
            )

            conn = sqlite3.connect(base / "mining_mvp.db")
            try:
                latest = conn.execute(
                    """
                    SELECT MAX(trade_date)
                    FROM candidates
                    WHERE strategy_id IN ('momentum_breakout', 'rps_stock_top20')
                    """
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(latest, dates["next_trade_date"])
        self.assertIn(dates["next_trade_date"], result["processed"])
        self.assertEqual(result["latest_persisted"], dates["next_trade_date"])

    def test_close_history_sync_only_processes_recent_tail_gaps(self) -> None:
        from run_daily import execute_daily_pipeline
        from mining.streamlit_tabs.tab_scanner import ensure_close_history_persisted

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            execute_daily_pipeline(
                base_dir=base,
                trade_date=dates["target_trade_date"],
                refresh=False,
                emit_reports=False,
            )

            result = ensure_close_history_persisted(
                base_dir=base,
                up_to_trade_date=dates["t_plus_2"],
                limit=3,
            )

        self.assertEqual(result["processed"], [dates["next_trade_date"], dates["t_plus_2"]])
        self.assertEqual(result["latest_persisted"], dates["t_plus_2"])

    def test_followup_label_uses_recent_close_when_not_true_previous_trade_day(self) -> None:
        from mining.streamlit_tabs.tab_scanner import _build_followup_label

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)

            label = _build_followup_label(
                base_dir=base,
                previous_date=dates["target_trade_date"],
                current_date=dates["t_plus_2"],
            )

        self.assertIn("最近收盘挖掘标的最新涨幅", label)
        self.assertIn(dates["target_trade_date"], label)
        self.assertIn(dates["t_plus_2"], label)

    def test_followups_use_previous_trade_day_even_when_current_day_is_already_persisted(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import (
            ensure_close_history_persisted,
            load_previous_day_followups,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            ensure_close_history_persisted(base_dir=base, up_to_trade_date=dates["next_trade_date"])

            def fake_latest_quotes() -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "code": "600001",
                            "close": 16.3,
                            "pre_close": 15.6,
                            "open": 15.8,
                            "high": 16.6,
                            "low": 15.7,
                            "volume": 11_000_000,
                            "amount": 1_900_000_000,
                            "change_pct": (16.3 / 15.6 - 1.0) * 100.0,
                        }
                    ]
                )

            follow_df, previous_date, current_date = load_previous_day_followups(
                base_dir=base,
                trade_date=dates["next_trade_date"],
                use_intraday=False,
                prefer_latest_quotes=True,
                fallback_trade_date=dates["next_trade_date"],
                snapshot_loader=fake_latest_quotes,
            )

        self.assertEqual(previous_date, dates["target_trade_date"])
        self.assertEqual(current_date, dates["next_trade_date"])
        self.assertFalse(follow_df.empty)

    def test_close_opportunities_reuse_persisted_candidates_without_rescanning(self) -> None:
        from mining.streamlit_tabs import tab_scanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            tab_scanner.ensure_close_history_persisted(base_dir=base, up_to_trade_date=dates["target_trade_date"])

            with mock.patch.object(
                tab_scanner,
                "_run_display_scanners",
                side_effect=AssertionError("should not rescan persisted close candidates"),
            ):
                today_df, today_date = tab_scanner.load_latest_opportunities(
                    base_dir=base,
                    trade_date=dates["target_trade_date"],
                    use_intraday=False,
                )

        self.assertEqual(today_date, dates["target_trade_date"])
        self.assertFalse(today_df.empty)

    def test_cached_snapshot_loader_fetches_underlying_data_once(self) -> None:
        import pandas as pd

        from mining.streamlit_tabs.tab_scanner import _build_cached_snapshot_loader

        calls = {"count": 0}

        def fake_loader() -> pd.DataFrame:
            calls["count"] += 1
            return pd.DataFrame([{"code": "600001", "close": 10.0}])

        loader = _build_cached_snapshot_loader(fake_loader)
        first = loader()
        second = loader()

        self.assertEqual(calls["count"], 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertIsNot(first, second)

    def test_display_scanners_reuse_rps_results_for_early_strength_exclusion(self) -> None:
        from mining.db import connect
        from mining.scanners import Candidate
        from mining.streamlit_tabs import tab_scanner
        from mining.universe import build_universe

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_days = trading_days("2026-02-20", 40)
            target_index = trade_days.index(dates["target_trade_date"])
            t_minus_2 = trade_days[target_index - 2]
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    UPDATE ash.kline_daily
                    SET open = 31.9, high = 33.65, low = 31.5, close = 32.2, pre_close = 31.0, change = 1.2, change_pct = 3.87, amount = 1300000000
                    WHERE sec_type='stock' AND sec_code='600003' AND trade_date=?
                    """,
                    (dates["target_trade_date"],),
                )
                conn.execute(
                    """
                    UPDATE ash.kline_daily
                    SET open = 28.7, high = 31.1, low = 28.5, close = 30.85, pre_close = 28.5, change = 2.35, change_pct = 8.25
                    WHERE sec_type='stock' AND sec_code='600003' AND trade_date=?
                    """,
                    (t_minus_2,),
                )
                universe = build_universe(conn, dates["target_trade_date"])
                fake_rps_candidates = [
                    Candidate(
                        strategy_id="rps_stock_top20",
                        version="v1.0",
                        trade_date=dates["target_trade_date"],
                        sec_type="stock",
                        sec_code="600001",
                        sec_name="Alpha",
                        entry_price=15.6,
                        rank=1,
                    )
                ]
                with mock.patch.object(
                    tab_scanner,
                    "select_rps_candidates",
                    return_value=fake_rps_candidates,
                ), mock.patch(
                    "mining.scanners.momentum_breakout._load_rps_exclusion_codes",
                    side_effect=AssertionError("should reuse shared rps results"),
                ):
                    tab_scanner._run_display_scanners(conn, dates["target_trade_date"], universe)
            finally:
                conn.close()


    def test_watchlist_display_uses_action_columns_without_edge_wording(self) -> None:
        from mining.streamlit_tabs.tab_scanner import _prepare_watchlist_display
        from mining.watchlist import split_actionable_watchlist

        watchlist = pd.DataFrame(
            [
                {
                    "sec_code": "600001",
                    "sec_name": "Ready",
                    "state": "回踩到位",
                    "run_up_pct": 0.2,
                    "flag_count": 3,
                    "pullback_pct": -0.12,
                    "shrink_ratio": 0.5,
                    "ma_proximity": 0.02,
                    "reclaim_ma10": False,
                    "pullback_red_days": 1,
                    "days_since_flag": 5,
                    "flag_strategies": "trend_embryo",
                    "triage": 2.0,
                },
                {
                    "sec_code": "600002",
                    "sec_name": "Trigger",
                    "state": "再启动",
                    "run_up_pct": 0.3,
                    "flag_count": 2,
                    "pullback_pct": -0.10,
                    "shrink_ratio": 0.6,
                    "ma_proximity": 0.01,
                    "reclaim_ma10": True,
                    "pullback_red_days": 0,
                    "days_since_flag": 4,
                    "flag_strategies": "true_leader",
                    "triage": 3.0,
                },
            ]
        )

        ready, trigger = split_actionable_watchlist(watchlist, top_n=30)
        display = _prepare_watchlist_display(pd.concat([ready, trigger], ignore_index=True))

        self.assertEqual(list(ready["state"]), ["回踩到位"])
        self.assertEqual(list(trigger["state"]), ["再启动"])
        self.assertEqual(
            list(display.columns),
            [
                "代码",
                "名称",
                "状态",
                "原始强度%",
                "标记次数",
                "峰值回撤%",
                "缩量比",
                "距MA%",
                "今日站回MA10",
                "回踩大阴线",
                "距标记日",
                "来源策略",
            ],
        )
        self.assertFalse(any("edge" in str(column).lower() for column in display.columns))
        self.assertAlmostEqual(display.iloc[0]["原始强度%"], 20.0)
        self.assertAlmostEqual(display.iloc[0]["峰值回撤%"], -12.0)

    def test_playbook_loader_indexes_maps_and_sections_safely(self) -> None:
        from mining.playbook import load_playbooks, lookup_playbook

        with tempfile.TemporaryDirectory() as tmp:
            cards_dir = Path(tmp) / "cards"
            setup_dir = cards_dir / "setups"
            setup_dir.mkdir(parents=True)
            (setup_dir / "SET-demo.md").write_text(
                """---
id: SET-demo
type: setup
title: Demo Card
source_docs: [D0001]
maps_to: [回踩到位, second_launch]
---

## 模式定义
Demo definition

## 看法
- 买点：Demo buy
- 确认信号：Demo confirm
- 放弃信号：Demo stop

## 案例
- Demo case

## 原文
- D0001
""",
                encoding="utf-8",
            )
            (setup_dir / "SET-missing.md").write_text(
                """---
id: SET-missing
type: setup
title: Missing Sections
maps_to: [trend_embryo]
---

## 模式定义
Only definition
""",
                encoding="utf-8",
            )
            (setup_dir / "BROKEN.md").write_bytes(b"\xff\xfe\x00")

            loaded = load_playbooks(cards_dir)
            state_hits = lookup_playbook("回踩到位", cards_dir)
            scanner_hits = lookup_playbook("second_launch", cards_dir)
            no_hits = lookup_playbook("不存在", cards_dir)

        self.assertEqual(loaded["errors"], 1)
        self.assertIn("回踩到位", loaded["index"])
        self.assertEqual(state_hits[0]["id"], "SET-demo")
        self.assertEqual(scanner_hits[0]["模式定义"], "Demo definition")
        self.assertIn("Demo confirm", scanner_hits[0]["看法"])
        self.assertEqual(no_hits, [])
        missing = loaded["index"]["trend_embryo"][0]
        self.assertEqual(missing["案例"], "")
        self.assertEqual(missing["原文"], "")

    def test_playbook_row_lookup_merges_state_and_scanner_cards(self) -> None:
        from mining.streamlit_tabs.tab_scanner import _playbook_cards_for_row, _playbook_empty_message

        cards_by_key = {
            "回踩到位": [{"id": "SET-ready", "模式定义": "ready", "看法": "view", "案例": "case", "原文": "D1"}],
            "second_launch": [{"id": "SET-ready", "模式定义": "ready", "看法": "view", "案例": "case", "原文": "D1"}],
            "true_leader": [{"id": "SET-leader", "模式定义": "leader", "看法": "view", "案例": "case", "原文": "D2"}],
        }

        def fake_lookup(key: str) -> list[dict[str, object]]:
            return cards_by_key.get(key, [])

        row = {"state": "回踩到位", "flag_strategies": "second_launch, true_leader"}
        cards = _playbook_cards_for_row(row, lookup=fake_lookup)
        empty_cards = _playbook_cards_for_row({"state": "延伸中", "flag_strategies": ""}, lookup=fake_lookup)

        self.assertEqual([card["id"] for card in cards], ["SET-ready", "SET-leader"])
        self.assertEqual(empty_cards, [])
        self.assertEqual(_playbook_empty_message(), "该状态暂无复盘笔记模式")
if __name__ == "__main__":
    unittest.main()
