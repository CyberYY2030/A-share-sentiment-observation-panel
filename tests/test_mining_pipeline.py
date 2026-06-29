import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._mining_test_helpers import create_sample_market_dbs, trading_days



def _seed_true_leader_market(conn: sqlite3.Connection, target_day: str) -> None:
    trade_days = trading_days("2026-02-20", 40)
    target_index = trade_days.index(target_day)
    window = trade_days[target_index - 20 : target_index + 1]
    down_dates = set(window[::3]) | {target_day}

    previous_index_close = 6000.0
    for trade_day in window:
        pct = -0.8 if trade_day in down_dates else 0.3
        close = round(previous_index_close * (1.0 + pct / 100.0), 2)
        pre_close = round(previous_index_close, 2)
        conn.execute(
            """
            UPDATE ash.kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='index' AND sec_code='000852' AND trade_date=?
            """,
            (
                pre_close,
                max(pre_close, close),
                min(pre_close, close),
                close,
                pre_close,
                round(close - pre_close, 2),
                pct,
                trade_day,
            ),
        )
        previous_index_close = close

    def seed_stock(code: str, up_pct: float, down_pct: float, amount: float, start: float) -> None:
        previous = start
        for trade_day in window:
            pct = down_pct if trade_day in down_dates else up_pct
            close = round(previous * (1.0 + pct / 100.0), 2)
            pre_close = round(previous, 2)
            open_price = round(pre_close * 1.001, 2)
            high = round(max(open_price, close) * 1.01, 2)
            low = round(min(open_price, close) * 0.99, 2)
            conn.execute(
                """
                UPDATE ash.kline_daily
                SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?, amount=?
                WHERE sec_type='stock' AND sec_code=? AND trade_date=?
                """,
                (
                    open_price,
                    high,
                    low,
                    close,
                    pre_close,
                    round(close - pre_close, 2),
                    pct,
                    amount,
                    code,
                    trade_day,
                ),
            )
            previous = close

    seed_stock("600001", up_pct=2.0, down_pct=2.4, amount=3_000_000_000, start=10.0)
    seed_stock("600003", up_pct=0.9, down_pct=1.2, amount=1_000_000_000, start=20.0)
    seed_stock("300001", up_pct=2.4, down_pct=-1.2, amount=4_000_000_000, start=15.0)
    conn.commit()



def _rewrite_stock_window(conn: sqlite3.Connection, target_day: str, code: str, start: float, end: float, amount: float) -> None:
    trade_days = trading_days("2026-02-20", 40)
    target_index = trade_days.index(target_day)
    window = trade_days[target_index - 20 : target_index + 1]
    closes = [round(start + (end - start) * idx / (len(window) - 1), 2) for idx in range(len(window))]
    for idx, trade_day in enumerate(window):
        close = closes[idx]
        pre_close = closes[idx - 1] if idx else round(close / 1.002, 2)
        change = round(close - pre_close, 2)
        change_pct = round((change / pre_close) * 100.0, 2) if pre_close else 0.0
        open_price = round(pre_close * 1.001, 2)
        conn.execute(
            """
            UPDATE ash.kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?, amount=?
            WHERE sec_type='stock' AND sec_code=? AND trade_date=?
            """,
            (
                open_price,
                round(max(open_price, close) * 1.01, 2),
                round(min(open_price, close) * 0.99, 2),
                close,
                pre_close,
                change,
                change_pct,
                amount,
                code,
                trade_day,
            ),
        )
    conn.commit()


def _set_index_down_pattern(
    conn: sqlite3.Connection,
    target_day: str,
    main_down_offsets: set[int],
    fallback_down_offsets: set[int] | None = None,
) -> None:
    trade_days = trading_days("2026-02-20", 40)
    target_index = trade_days.index(target_day)
    all_window = trade_days[: target_index + 1]
    fallback_down_offsets = fallback_down_offsets or set()
    previous = 6000.0
    for idx, trade_day in enumerate(all_window):
        relative = idx - target_index
        pct = -0.8 if relative in main_down_offsets or idx in fallback_down_offsets else 0.3
        close = round(previous * (1.0 + pct / 100.0), 2)
        conn.execute(
            """
            UPDATE ash.kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='index' AND sec_code='000852' AND trade_date=?
            """,
            (
                previous,
                max(previous, close),
                min(previous, close),
                close,
                previous,
                round(close - previous, 2),
                pct,
                trade_day,
            ),
        )
        previous = close
    conn.commit()

def _seed_trend_embryo_market(conn: sqlite3.Connection, target_day: str) -> None:
    trade_days = trading_days("2026-02-20", 40)
    target_index = trade_days.index(target_day)
    window = trade_days[target_index - 5 : target_index + 1]

    def ensure_stock(code: str, name: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO ash.stock_info VALUES (?, ?, ?, ?, ?)",
            (code, f"sh.{code}", name, "正常交易", "2026-04-10 00:00:00"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO stock_market_cap VALUES (?, ?, ?, ?)",
            (code, 4_000_000_000, target_day, "2026-04-10 00:00:00"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO stock_listing VALUES (?, ?, ?, ?)",
            (code, "2025-01-01", target_day, "2026-04-10 00:00:00"),
        )

    def seed_stock(code: str, name: str, changes: list[float], amount: float, start: float = 10.0) -> None:
        ensure_stock(code, name)
        previous = start
        change_by_day = dict(zip(window[1:], changes))
        for trade_day in trade_days:
            pct = change_by_day.get(trade_day, 0.2)
            if trade_day == window[0]:
                previous = start
                close = start
                pre_close = round(start / 1.002, 2)
                pct = round((close / pre_close - 1.0) * 100.0, 2)
            else:
                pre_close = round(previous, 2)
                close = round(previous * (1.0 + pct / 100.0), 2)
            open_price = round(pre_close * 1.001, 2)
            high = round(max(open_price, close) * 1.01, 2)
            low = round(min(open_price, close) * 0.99, 2)
            conn.execute(
                """
                INSERT OR REPLACE INTO ash.kline_daily (
                  sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                  change, change_pct, volume, amount, turnover_ratio, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "stock",
                    code,
                    trade_day,
                    open_price,
                    high,
                    low,
                    close,
                    pre_close,
                    round(close - pre_close, 2),
                    pct,
                    12_000_000,
                    amount,
                    3.0,
                    "unit-test",
                    "2026-04-10 00:00:00",
                ),
            )
            previous = close

    seed_stock("601001", "EmbryoNoLimit", [2.8, 2.6, 2.5, 2.4, 2.3], 700_000_000)
    seed_stock("601002", "EmbryoOneLimit", [10.0, 1.0, 1.0, 1.0, 1.0], 700_000_000)
    seed_stock("601003", "EmbryoTwoLimit", [10.0, 10.0, 1.0, 1.0, 1.0], 700_000_000)
    seed_stock("301001", "EmbryoSpike", [15.0, 1.0, 1.0, 1.0, 1.0], 700_000_000)
    seed_stock("601004", "EmbryoTooLate", [8.0, 8.0, 8.0, 8.0, 4.0], 700_000_000)
    conn.commit()


def _insert_source_candidate(conn: sqlite3.Connection, strategy_id: str, trade_date: str, sec_code: str, sec_name: str) -> None:
    run_id = conn.execute(
        """
        INSERT INTO strategy_runs (
          strategy_id, version, trade_date, run_at, universe_size,
          n_candidates, status, error_msg
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (strategy_id, "vtest", trade_date, "2026-04-10 00:00:00", 1, 1, "ok", None),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO candidates (
          run_id, strategy_id, version, trade_date, sec_type, sec_code,
          sec_name, entry_price, features_json, rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, strategy_id, "vtest", trade_date, "stock", sec_code, sec_name, 10.0, "{}", 1),
    )


def _seed_second_launch_path(conn: sqlite3.Connection, target_day: str, shrink: bool = True) -> str:
    trade_days = trading_days("2026-02-20", 40)
    target_index = trade_days.index(target_day)
    flag_day = trade_days[target_index - 10]
    closes_by_offset = {
        -10: 10.0,
        -9: 11.0,
        -8: 12.0,
        -7: 11.8,
        -6: 11.5,
        -5: 11.2,
        -4: 11.0,
        -3: 10.95,
        -2: 10.90,
        -1: 10.88,
        0: 10.90,
    }
    last_close = 10.0
    for idx, trade_day in enumerate(trade_days):
        offset = idx - target_index
        close = closes_by_offset.get(offset, 10.0 if offset < -10 else last_close)
        if offset > 0:
            close = last_close
        pre_close = last_close if idx else round(close * 0.99, 2)
        low = close - 0.2
        if offset == -2:
            low = 10.50
        elif offset == -1:
            low = 10.60
        elif offset == 0:
            low = 10.70
        volume = 10_000_000
        if -14 <= offset <= -10:
            volume = 20_000_000
        if -4 <= offset <= 0:
            volume = 10_000_000 if shrink else 18_000_000
        conn.execute(
            """
            UPDATE ash.kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?, volume=?, amount=?
            WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
            """,
            (
                round(pre_close, 2),
                round(close + 0.3, 2),
                round(low, 2),
                round(close, 2),
                round(pre_close, 2),
                round(close - pre_close, 2),
                round((close / pre_close - 1.0) * 100.0, 2) if pre_close else 0.0,
                volume,
                volume * close,
                trade_day,
            ),
        )
        last_close = close
    _insert_source_candidate(conn, "trend_embryo", flag_day, "600001", "Alpha")
    conn.commit()
    return flag_day
class MiningPipelineTests(unittest.TestCase):
    def test_build_universe_filters_st_and_beijing_codes(self) -> None:
        from mining.db import connect
        from mining.universe import build_universe

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                universe = build_universe(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertEqual(set(universe["sec_code"]), {"600001", "300001", "600003"})

    def test_execute_daily_pipeline_persists_candidates_and_reports(self) -> None:
        from run_daily import execute_daily_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)

            result = execute_daily_pipeline(
                base_dir=base,
                trade_date=dates["target_trade_date"],
                refresh=False,
                out_dir=base / "output",
            )

            self.assertEqual(result["trade_date"], dates["target_trade_date"])
            self.assertTrue((base / "output" / f"report_{dates['target_trade_date'].replace('-', '')}.md").exists())
            self.assertTrue((base / "output" / f"candidates_{dates['target_trade_date'].replace('-', '')}.xlsx").exists())

            mining_db = base / "mining_mvp.db"
            self.assertTrue(mining_db.exists())
            conn = sqlite3.connect(mining_db)
            try:
                strategy_counts = dict(
                    conn.execute(
                        "SELECT strategy_id, COUNT(*) FROM candidates GROUP BY strategy_id"
                    ).fetchall()
                )
                self.assertGreaterEqual(strategy_counts.get("momentum_breakout", 0), 2)
                self.assertEqual(strategy_counts.get("rps_stock_top20", 0), 1)
                self.assertEqual(strategy_counts.get("rps_concept_top20", 0), 1)

                outcomes = dict(
                    conn.execute(
                        """
                        SELECT c.strategy_id, COUNT(*)
                        FROM outcomes o
                        JOIN candidates c ON c.candidate_id = o.candidate_id
                        GROUP BY c.strategy_id
                        """
                    ).fetchall()
                )
                self.assertGreaterEqual(outcomes.get("momentum_breakout", 0), 1)
                self.assertGreaterEqual(outcomes.get("rps_stock_top20", 0), 1)
            finally:
                conn.close()

    def test_execute_daily_pipeline_reuses_shared_rps_results_for_momentum_exclusion(self) -> None:
        from mining.db import connect
        from run_daily import execute_daily_pipeline

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
                conn.commit()
            finally:
                conn.close()

            with mock.patch(
                "mining.scanners.momentum_breakout._load_rps_exclusion_codes",
                side_effect=AssertionError("should reuse shared rps results"),
            ):
                result = execute_daily_pipeline(
                    base_dir=base,
                    trade_date=dates["target_trade_date"],
                    refresh=False,
                    out_dir=base / "output",
                    emit_reports=False,
                )

        self.assertEqual(result["trade_date"], dates["target_trade_date"])

    def test_momentum_shadow_path_requires_open_and_close_breakout(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    UPDATE ash.kline_daily
                    SET high = 26.4
                    WHERE sec_type='stock' AND sec_code='300001' AND trade_date=?
                    """,
                    (dates["target_trade_date"],),
                )
                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        codes = {candidate.sec_code for candidate in candidates}
        self.assertNotIn("300001", codes)

    def test_momentum_shadow_path_allows_mild_latest_pullback(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    UPDATE ash.kline_daily
                    SET open = 25.1, high = 26.7, close = 24.7, pre_close = 25.33, change_pct = -2.49
                    WHERE sec_type='stock' AND sec_code='300001' AND trade_date=?
                    """,
                    (dates["target_trade_date"],),
                )
                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        path_by_code = {candidate.sec_code: candidate.features["path"] for candidate in candidates}
        self.assertIn("300001", path_by_code)
        self.assertIn("shadow", path_by_code["300001"])

    def test_momentum_shadow_path_excludes_latest_drop_below_minus_three_percent(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    UPDATE ash.kline_daily
                    SET open = 25.1, high = 26.7, close = 24.7, pre_close = 25.5, change_pct = -3.14
                    WHERE sec_type='stock' AND sec_code='300001' AND trade_date=?
                    """,
                    (dates["target_trade_date"],),
                )
                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        codes = {candidate.sec_code for candidate in candidates}
        self.assertNotIn("300001", codes)

    def test_momentum_selects_early_strength_adjustment_path(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

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
                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        path_by_code = {candidate.sec_code: candidate.features["path"] for candidate in candidates}
        self.assertIn("600003", path_by_code)
        self.assertIn("early_strength_adjustment", path_by_code["600003"])

    def test_momentum_early_strength_adjustment_requires_recent_eight_percent_spike(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
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
                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        codes = {candidate.sec_code for candidate in candidates}
        self.assertNotIn("600003", codes)

    def test_momentum_early_strength_adjustment_excludes_rps_top20_overlap(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_days = trading_days("2026-02-20", 40)
            target_index = trade_days.index(dates["target_trade_date"])
            target_day = dates["target_trade_date"]
            t_minus_2 = trade_days[target_index - 2]
            conn = connect(base_dir=base)
            try:
                code = "601200"
                conn.execute(
                    "INSERT OR REPLACE INTO ash.stock_info VALUES (?, ?, ?, ?, ?)",
                    (code, f"sh.{code}", code, "姝ｅ父浜ゆ槗", "2026-04-10 00:00:00"),
                )
                closes: list[float] = []
                for idx, trade_day in enumerate(trade_days):
                    if trade_day == t_minus_2:
                        close = 28.15
                    elif trade_day == target_day:
                        close = 28.23
                    else:
                        close = round(12.0 + idx * 0.4, 2)
                    closes.append(close)

                for idx, trade_day in enumerate(trade_days):
                    close = closes[idx]
                    pre_close = closes[idx - 1] if idx else round(close * 0.99, 2)
                    change = round(close - pre_close, 2)
                    change_pct = round((change / pre_close) * 100, 2)
                    open_price = round(pre_close * 1.002, 2)
                    high = round(max(open_price, close) * 1.01, 2)
                    low = round(min(open_price, close) * 0.99, 2)
                    amount = 1_350_000_000
                    if trade_day == target_day:
                        open_price = 28.0
                        high = 29.55
                        low = 27.8
                        pre_close = closes[idx - 1]
                        close = 28.23
                        change = round(close - pre_close, 2)
                        change_pct = round((change / pre_close) * 100, 2)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO ash.kline_daily (
                          sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                          change, change_pct, volume, amount, turnover_ratio, source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "stock",
                            code,
                            trade_day,
                            open_price,
                            high,
                            low,
                            close,
                            pre_close,
                            change,
                            change_pct,
                            12_000_000,
                            amount,
                            3.0,
                            "unit-test",
                            "2026-04-10 00:00:00",
                        ),
                    )
                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, target_day)
            finally:
                conn.close()

        codes = {candidate.sec_code for candidate in candidates}
        self.assertNotIn("601200", codes)

    def test_momentum_early_strength_adjustment_keeps_only_top_ten_by_recent_window_gain(self) -> None:
        from mining.db import connect
        from mining.scanners.momentum_breakout import MomentumBreakoutScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            trade_days = trading_days("2026-02-20", 40)
            target_index = trade_days.index(dates["target_trade_date"])
            target_day = dates["target_trade_date"]
            t_minus_4 = trade_days[target_index - 4]
            t_minus_3 = trade_days[target_index - 3]
            t_minus_2 = trade_days[target_index - 2]
            t_minus_1 = trade_days[target_index - 1]
            conn = connect(base_dir=base)
            try:
                def seed_adjustment_stock(code: str, window_gain_pct: float) -> None:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO ash.stock_info VALUES (?, ?, ?, ?, ?)
                        """,
                        (code, f"sh.{code}", code, "姝ｅ父浜ゆ槗", "2026-04-10 00:00:00"),
                    )
                    closes: list[float] = []
                    for idx, trade_day in enumerate(trade_days):
                        if idx <= target_index - 11:
                            close = 25.0
                        elif idx <= target_index - 4:
                            close = 20.0
                        elif trade_day == t_minus_3:
                            close = 20.0
                        elif trade_day == t_minus_2:
                            close = 21.8
                        elif trade_day == t_minus_1:
                            close = round(20.0 * (1.0 + window_gain_pct / 100.0), 2)
                        elif trade_day == target_day:
                            close = round((20.0 * (1.0 + window_gain_pct / 100.0)) * 1.008, 2)
                        else:
                            close = 20.0
                        closes.append(round(close, 2))

                    for idx, trade_day in enumerate(trade_days):
                        close = closes[idx]
                        pre_close = closes[idx - 1] if idx else close
                        change = round(close - pre_close, 2)
                        change_pct = round((change / pre_close) * 100, 2) if pre_close else 0.0
                        open_price = round(close * 0.997, 2)
                        high = round(max(open_price, close) * 1.01, 2)
                        low = round(min(open_price, close) * 0.99, 2)
                        amount = 850_000_000
                        if trade_day == target_day:
                            open_price = round(pre_close * 1.001, 2)
                            high = round(max(open_price, close) * 1.05, 2)
                            low = round(min(open_price, close) * 0.99, 2)
                            amount = 1_250_000_000
                            change = round(close - pre_close, 2)
                            change_pct = round((change / pre_close) * 100, 2)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO ash.kline_daily (
                              sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                              change, change_pct, volume, amount, turnover_ratio, source, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                "stock",
                                code,
                                trade_day,
                                open_price,
                                high,
                                low,
                                close,
                                pre_close,
                                change,
                                change_pct,
                                12_000_000,
                                amount,
                                3.0,
                                "unit-test",
                                "2026-04-10 00:00:00",
                            ),
                        )

                seeded_codes: list[tuple[str, float]] = []
                for offset in range(11):
                    code = f"6011{offset:02d}"
                    window_gain_pct = 11.0 - offset * 0.5
                    seeded_codes.append((code, window_gain_pct))
                    seed_adjustment_stock(code, window_gain_pct)

                scanner = MomentumBreakoutScanner()
                candidates = scanner.run(conn, target_day)
            finally:
                conn.close()

        adjustment_codes = {
            candidate.sec_code
            for candidate in candidates
            if "early_strength_adjustment" in candidate.features["path"]
        }
        expected_top_ten = {code for code, _ in seeded_codes[:10]}
        self.assertEqual(adjustment_codes, expected_top_ten)
        self.assertNotIn(seeded_codes[-1][0], adjustment_codes)

    def test_true_leader_prefers_separation_and_amount_band(self) -> None:
        from mining.db import connect
        from mining.scanners.true_leader import TrueLeaderScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_true_leader_market(conn, dates["target_trade_date"])
                scanner = TrueLeaderScanner(params={"strength_min": 0.0})
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        codes = [candidate.sec_code for candidate in candidates]
        self.assertIn("600001", codes)
        self.assertIn("600003", codes)
        self.assertNotIn("300001", codes)
        self.assertLess(codes.index("600001"), codes.index("600003"))

        features = {candidate.sec_code: candidate.features for candidate in candidates}
        self.assertGreaterEqual(features["600001"]["separation_ratio"], 0.3)
        self.assertTrue(features["600001"]["divergence_today"])
        self.assertTrue(features["600001"]["is_new_high"])
        self.assertTrue(features["600001"]["amount_band_ok"])
        self.assertFalse(features["600003"]["amount_band_ok"])
        self.assertGreater(features["600001"]["score"], features["600003"]["score"])

    def test_true_leader_falls_back_when_main_window_has_few_down_days(self) -> None:
        from mining.db import connect
        from mining.scanners.true_leader import TrueLeaderScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_true_leader_market(conn, dates["target_trade_date"])
                _set_index_down_pattern(
                    conn,
                    dates["target_trade_date"],
                    main_down_offsets={0},
                    fallback_down_offsets={2, 7, 12},
                )
                scanner = TrueLeaderScanner(params={"strength_min": 0.0})
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        features = {candidate.sec_code: candidate.features for candidate in candidates}
        self.assertIn("600001", features)
        self.assertGreaterEqual(features["600001"]["down_days_used"], 3)
        self.assertFalse(features["600001"]["separation_insufficient"])
        self.assertIn("separation_strength", features["600001"])

    def test_true_leader_keeps_candidates_when_separation_is_insufficient_but_downweights_score(self) -> None:
        from mining.db import connect
        from mining.scanners.true_leader import TrueLeaderScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_true_leader_market(conn, dates["target_trade_date"])
                _set_index_down_pattern(conn, dates["target_trade_date"], main_down_offsets={0})
                insufficient = TrueLeaderScanner(
                    params={"strength_min": 0.0, "sep_excess_min": 99.0, "min_down_days": 3}
                ).run(conn, dates["target_trade_date"])
                sufficient = TrueLeaderScanner(
                    params={"strength_min": 0.0, "sep_excess_min": 0.0, "min_down_days": 1}
                ).run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        insufficient_features = {candidate.sec_code: candidate.features for candidate in insufficient}
        sufficient_features = {candidate.sec_code: candidate.features for candidate in sufficient}
        self.assertIn("600001", insufficient_features)
        self.assertTrue(insufficient_features["600001"]["separation_insufficient"])
        self.assertEqual(insufficient_features["600001"]["down_days_used"], 1)
        self.assertLess(insufficient_features["600001"]["score"], sufficient_features["600001"]["score"])

    def test_true_leader_excludes_hard_extension_and_penalizes_soft_extension(self) -> None:
        from mining.db import connect
        from mining.scanners.true_leader import TrueLeaderScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_true_leader_market(conn, dates["target_trade_date"])
                _rewrite_stock_window(conn, dates["target_trade_date"], "600001", 10.0, 21.0, 3_000_000_000)
                _rewrite_stock_window(conn, dates["target_trade_date"], "600003", 20.0, 34.0, 3_000_000_000)
                scanner = TrueLeaderScanner(params={"strength_min": 0.0})
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        codes = [candidate.sec_code for candidate in candidates]
        self.assertNotIn("600001", codes)
        features = {candidate.sec_code: candidate.features for candidate in candidates}
        self.assertIn("600003", features)
        self.assertTrue(features["600003"]["over_extended"])
        self.assertGreater(features["600003"]["over_extension_penalty"], 0.0)
    def test_execute_daily_pipeline_persists_true_leader_via_generic_scanner_branch(self) -> None:
        from mining.db import connect
        from run_daily import execute_daily_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_true_leader_market(conn, dates["target_trade_date"])
            finally:
                conn.close()

            execute_daily_pipeline(
                base_dir=base,
                trade_date=dates["target_trade_date"],
                refresh=False,
                out_dir=base / "output",
                emit_reports=False,
            )

            conn = sqlite3.connect(base / "mining_mvp.db")
            try:
                rows = conn.execute(
                    """
                    SELECT sec_code, features_json
                    FROM candidates
                    WHERE strategy_id='true_leader'
                    ORDER BY rank, sec_code
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "600001")
        self.assertIn("separation_ratio", rows[0][1])

    def test_trend_embryo_filters_early_path_and_scores_shape(self) -> None:
        from mining.db import connect
        from mining.scanners.trend_embryo import TrendEmbryoScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_trend_embryo_market(conn, dates["target_trade_date"])
                scanner = TrendEmbryoScanner()
                candidates = scanner.run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        codes = [candidate.sec_code for candidate in candidates]
        self.assertIn("601001", codes)
        self.assertIn("601002", codes)
        self.assertIn("301001", codes)
        self.assertNotIn("601003", codes)
        self.assertNotIn("601004", codes)

        features = {candidate.sec_code: candidate.features for candidate in candidates}
        self.assertEqual(features["601001"]["path"], "no_limit")
        self.assertEqual(features["601001"]["limit_up_count_5d"], 0)
        self.assertEqual(features["601002"]["path"], "one_limit")
        self.assertEqual(features["601002"]["limit_up_count_5d"], 1)
        self.assertGreater(features["301001"]["single_day_max_change"], 9.5)
        self.assertEqual(features["601001"]["score_mode"], "candidate_pool")
        self.assertEqual(features["301001"]["score"], 0.0)
        self.assertEqual(features["601001"]["score"], 0.0)
        self.assertIsNone(features["601001"]["independence"])

    def test_execute_daily_pipeline_persists_trend_embryo_via_generic_scanner_branch(self) -> None:
        from mining.db import connect
        from run_daily import execute_daily_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_trend_embryo_market(conn, dates["target_trade_date"])
            finally:
                conn.close()

            execute_daily_pipeline(
                base_dir=base,
                trade_date=dates["target_trade_date"],
                refresh=False,
                out_dir=base / "output",
                emit_reports=False,
            )

            conn = sqlite3.connect(base / "mining_mvp.db")
            try:
                rows = conn.execute(
                    """
                    SELECT sec_code, features_json
                    FROM candidates
                    WHERE strategy_id='trend_embryo'
                    ORDER BY rank, sec_code
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertGreaterEqual(len(rows), 3)
        self.assertIn("limit_up_count_5d", rows[0][1])
        self.assertIn("score_mode", rows[0][1])
    def test_watchlist_classify_state_covers_lifecycle_rules(self) -> None:
        from mining.watchlist import classify_state

        base = {
            "pullback_pct": -0.12,
            "ma_proximity": 0.02,
            "shrink_ratio": 0.6,
            "below_ma60": False,
            "made_new_low_recent": False,
            "reclaim_ma10": False,
            "vol_expand_up": False,
        }

        self.assertEqual(classify_state({**base, "pullback_pct": -0.31}), "破位失效")
        self.assertEqual(classify_state({**base, "below_ma60": True, "made_new_low_recent": True}), "破位失效")
        self.assertEqual(classify_state({**base, "reclaim_ma10": True, "vol_expand_up": True}), "再启动")
        self.assertEqual(classify_state(base), "回踩到位")
        self.assertEqual(classify_state({**base, "ma_proximity": 0.04}), "回踩中")
        self.assertEqual(classify_state({**base, "pullback_pct": -0.03}), "延伸中")
    def test_build_watchlist_sediments_flagged_candidates_and_pullback_state(self) -> None:
        from mining.db import connect
        from mining.watchlist import build_watchlist

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                flag_day = _seed_second_launch_path(conn, dates["target_trade_date"])
                watchlist = build_watchlist(conn, dates["target_trade_date"], lookback=40)
            finally:
                conn.close()

        self.assertEqual(len(watchlist), 1)
        row = watchlist.iloc[0]
        self.assertEqual(row["sec_code"], "600001")
        self.assertEqual(row["first_flag_date"], flag_day)
        self.assertEqual(row["last_flag_date"], flag_day)
        self.assertEqual(row["flag_count"], 1)
        self.assertEqual(row["flag_strategies"], "trend_embryo")
        self.assertAlmostEqual(row["peak_close_since_flag"], 12.0)
        self.assertAlmostEqual(row["first_flag_close"], 10.0)
        self.assertAlmostEqual(row["run_up_pct"], 0.2)
        self.assertAlmostEqual(row["pullback_pct"], 10.9 / 12.0 - 1.0)
        self.assertLessEqual(row["ma_proximity"], 0.03)
        self.assertLessEqual(row["shrink_ratio"], 0.7)
        self.assertEqual(row["pullback_red_days"], 0)
        self.assertFalse(row["made_new_low_recent"])
        self.assertFalse(row["reclaim_ma10"])
        self.assertFalse(row["vol_expand_up"])
        self.assertEqual(row["state"], "回踩到位")
        self.assertGreater(row["triage"], 0)
        self.assertEqual(row["days_since_flag"], 10)
        self.assertEqual(row["days_since_peak"], 8)

    def test_split_actionable_watchlist_limits_ready_and_trigger_lists(self) -> None:
        import pandas as pd

        from mining.watchlist import split_actionable_watchlist

        watchlist = pd.DataFrame(
            [
                {"sec_code": "600001", "state": "回踩到位", "triage": 2.0},
                {"sec_code": "600002", "state": "回踩到位", "triage": 3.0},
                {"sec_code": "600003", "state": "再启动", "triage": 1.0},
                {"sec_code": "600004", "state": "回踩中", "triage": 9.0},
            ]
        )

        ready, trigger = split_actionable_watchlist(watchlist, top_n=1)

        self.assertEqual(list(ready["sec_code"]), ["600002"])
        self.assertEqual(list(trigger["sec_code"]), ["600003"])

    def test_build_watchlist_empty_without_source_candidates(self) -> None:
        from mining.db import connect
        from mining.watchlist import build_watchlist

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                watchlist = build_watchlist(conn, dates["target_trade_date"], lookback=40)
            finally:
                conn.close()

        self.assertTrue(watchlist.empty)

    def test_second_launch_selects_pullback_shrink_and_stop_setup(self) -> None:
        from mining.db import connect
        from mining.scanners.second_launch import SecondLaunchScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_second_launch_path(conn, dates["target_trade_date"])
                candidates = SecondLaunchScanner().run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertEqual([candidate.sec_code for candidate in candidates], ["600001"])
        features = candidates[0].features
        self.assertLessEqual(features["pullback_pct"], -0.08)
        self.assertLessEqual(features["ma_proximity"], 0.03)
        self.assertAlmostEqual(features["shrink_ratio"], 0.5)
        self.assertTrue(features["stop_signal"])
        self.assertEqual(features["flag_strategies"], "trend_embryo")

    def test_second_launch_excludes_without_volume_shrink(self) -> None:
        from mining.db import connect
        from mining.scanners.second_launch import SecondLaunchScanner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_second_launch_path(conn, dates["target_trade_date"], shrink=False)
                candidates = SecondLaunchScanner().run(conn, dates["target_trade_date"])
            finally:
                conn.close()

        self.assertEqual(candidates, [])

    def test_execute_daily_pipeline_persists_second_launch_via_generic_scanner_branch(self) -> None:
        from mining.db import connect
        from run_daily import execute_daily_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_second_launch_path(conn, dates["target_trade_date"])
            finally:
                conn.close()

            execute_daily_pipeline(
                base_dir=base,
                trade_date=dates["target_trade_date"],
                refresh=False,
                out_dir=base / "output",
                emit_reports=False,
            )

            conn = sqlite3.connect(base / "mining_mvp.db")
            try:
                rows = conn.execute(
                    """
                    SELECT sec_code, features_json
                    FROM candidates
                    WHERE strategy_id='second_launch'
                    ORDER BY rank, sec_code
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "600001")
        self.assertIn("pullback_pct", rows[0][1])
        self.assertIn("stop_signal", rows[0][1])

    def test_persist_watchlist_snapshot_is_idempotent_and_keeps_context(self) -> None:
        from mining.db import connect
        from mining.watchlist import persist_watchlist_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_second_launch_path(conn, dates["target_trade_date"])
                first_count = persist_watchlist_snapshot(conn, dates["target_trade_date"])
                second_count = persist_watchlist_snapshot(conn, dates["target_trade_date"])
                rows = conn.execute(
                    """
                    SELECT snapshot_date, sec_code, state, entry_price, flag_strategies,
                           reclaim_ma10, vol_expand_up
                    FROM watchlist_snapshots
                    ORDER BY sec_code, state
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_date"], dates["target_trade_date"])
        self.assertEqual(rows[0]["sec_code"], "600001")
        self.assertAlmostEqual(rows[0]["entry_price"], 10.90)
        self.assertEqual(rows[0]["flag_strategies"], "trend_embryo")
        self.assertTrue(rows[0]["state"])

    def test_backfill_snapshot_outcomes_matches_candidate_outcome_values(self) -> None:
        from mining.backtest import backfill_outcomes, backfill_snapshot_outcomes
        from mining.db import connect
        from mining.watchlist import persist_watchlist_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                _seed_second_launch_path(conn, dates["target_trade_date"])
                persist_watchlist_snapshot(conn, dates["target_trade_date"])
                conn.execute(
                    """
                    INSERT INTO strategy_runs (
                      run_id, strategy_id, version, trade_date, run_at, universe_size,
                      n_candidates, status, error_msg
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (991, "unit", "vtest", dates["target_trade_date"], "2026-04-10 00:00:00", 1, 1, "ok", None),
                )
                conn.execute(
                    """
                    INSERT INTO candidates (
                      run_id, strategy_id, version, trade_date, sec_type, sec_code,
                      sec_name, entry_price, features_json, rank
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (991, "unit", "vtest", dates["target_trade_date"], "stock", "600001", "Alpha", 10.90, "{}", 1),
                )
                conn.commit()
                candidate_result = backfill_outcomes(conn, trade_date=dates["target_trade_date"], force=True)
                snapshot_result = backfill_snapshot_outcomes(conn, snapshot_date=dates["target_trade_date"], force=True)
                candidate = conn.execute(
                    """
                    SELECT o.r1, o.r2, o.r3, o.r4, o.r5, o.is_win, o.status
                    FROM outcomes o
                    JOIN candidates c ON c.candidate_id=o.candidate_id
                    WHERE c.strategy_id='unit'
                    """
                ).fetchone()
                snapshot = conn.execute(
                    """
                    SELECT r1, r2, r3, r4, r5, is_win, status
                    FROM watchlist_outcomes
                    WHERE snapshot_date=? AND sec_code='600001'
                    """,
                    (dates["target_trade_date"],),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(candidate_result["processed"], 1)
        self.assertEqual(snapshot_result["processed"], 1)
        self.assertEqual(snapshot["status"], "complete")
        for column in ["r1", "r2", "r3", "r4", "r5"]:
            self.assertAlmostEqual(snapshot[column], candidate[column])
        self.assertEqual(snapshot["is_win"], candidate["is_win"])

    def test_backfill_snapshot_outcomes_marks_unfinished_snapshot_partial(self) -> None:
        from mining.backtest import backfill_snapshot_outcomes
        from mining.db import connect

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            conn = connect(base_dir=base)
            try:
                conn.execute(
                    """
                    INSERT INTO watchlist_snapshots (
                      snapshot_date, sec_code, sec_name, state, triage, entry_price,
                      created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (dates["t_plus_2"], "600001", "Alpha", "????", 1.0, 10.0, "2026-04-10 00:00:00"),
                )
                conn.commit()
                result = backfill_snapshot_outcomes(conn, snapshot_date=dates["t_plus_2"], force=True)
                row = conn.execute(
                    """
                    SELECT status, r5
                    FROM watchlist_outcomes
                    WHERE snapshot_date=? AND sec_code='600001'
                    """,
                    (dates["t_plus_2"],),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["partial"], 1)
        self.assertEqual(row["status"], "partial")
        self.assertIsNone(row["r5"])

if __name__ == "__main__":
    unittest.main()
