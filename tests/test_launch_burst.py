from __future__ import annotations

import sqlite3
import tempfile
import unittest
import inspect
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tests._mining_test_helpers import create_sample_market_dbs


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "launch_samples"


def _synthetic_history(
    code: str = "600001",
    target_date: str = "2026-06-18",
    target_close: float = 10.6,
    target_volume: float = 2_000_000,
    target_amount: float = 300_000_000,
) -> pd.DataFrame:
    dates = pd.bdate_range(end=target_date, periods=65).strftime("%Y-%m-%d")
    rows = []
    for index, day in enumerate(dates):
        is_target = index == len(dates) - 1
        close = target_close if is_target else 10.0
        rows.append(
            {
                "sec_type": "stock",
                "sec_code": code,
                "sec_name": f"Stock {code}",
                "trade_date": day,
                "open": 10.0,
                "high": close,
                "low": 9.9,
                "close": close,
                "pre_close": None if is_target else 10.0,
                "volume": target_volume if is_target else 1_000_000,
                "amount": target_amount if is_target else 100_000_000,
            }
        )
    return pd.DataFrame(rows)


def _scan(history: pd.DataFrame, params: dict | None = None):
    from mining.scanners.launch_burst import LaunchBurstScanner, select_candidates_from_history

    scanner = LaunchBurstScanner(params=params)
    target_date = str(history["trade_date"].max())
    return select_candidates_from_history(
        history,
        target_date,
        scanner.params,
        scanner.strategy_id,
        scanner.version,
    )


def _load_fixture(code: str) -> pd.DataFrame:
    frame = pd.read_csv(FIXTURE_DIR / f"{code}.csv", dtype={"date": str})
    frame = frame.rename(columns={"date": "trade_date"})
    frame["sec_type"] = "stock"
    frame["sec_code"] = code
    frame["sec_name"] = code
    if "pre_close" not in frame.columns:
        frame["pre_close"] = pd.to_numeric(frame["close"], errors="coerce").shift(1)
    if "amount" not in frame.columns:
        frame["amount"] = (
            pd.to_numeric(frame["volume"], errors="coerce")
            * pd.to_numeric(frame["close"], errors="coerce")
            * 100.0
        )
    return frame


def _fixture_hit_dates(code: str, label_date: str, radius: int) -> list[str]:
    frame = _load_fixture(code)
    dates = frame["trade_date"].astype(str).tolist()
    label_pos = dates.index(label_date)
    selected_dates = dates[max(0, label_pos - radius) : label_pos + radius + 1]
    hits = []
    for trade_date in selected_dates:
        subset = frame[frame["trade_date"].astype(str).le(trade_date)]
        candidates = _scan(subset)
        if any(candidate.sec_code == code for candidate in candidates):
            hits.append(trade_date)
    return hits


def _insert_history(conn: sqlite3.Connection, history: pd.DataFrame) -> None:
    code = str(history.iloc[0]["sec_code"])
    conn.execute(
        "INSERT OR REPLACE INTO ash.stock_info VALUES (?, ?, ?, ?, ?)",
        (code, f"sh.{code}", f"Stock {code}", "正常交易", "2026-06-18 00:00:00"),
    )
    for row in history.itertuples(index=False):
        pre_close = row.pre_close if pd.notna(row.pre_close) else None
        change = row.close - (pre_close if pre_close else 10.0)
        change_pct = None
        conn.execute(
            """
            INSERT OR REPLACE INTO ash.kline_daily (
              sec_type, sec_code, trade_date, open, high, low, close, pre_close,
              change, change_pct, volume, amount, turnover_ratio, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.sec_type,
                row.sec_code,
                row.trade_date,
                row.open,
                row.high,
                row.low,
                row.close,
                pre_close,
                change,
                change_pct,
                row.volume,
                row.amount,
                1.0,
                "unit-test",
                "2026-06-18 00:00:00",
            ),
        )
    conn.commit()


class LaunchBurstTests(unittest.TestCase):
    def test_registration_watchlist_and_playbook_hooks_are_wired(self) -> None:
        from mining.playbook import lookup_playbook
        from mining.scanners import get_registered_scanners
        from mining.watchlist import STRENGTH_SCANNERS, build_watchlist

        registered = {scanner_cls().strategy_id for scanner_cls in get_registered_scanners()}
        default_sources = inspect.signature(build_watchlist).parameters["source_strategies"].default
        cards = lookup_playbook("launch_burst")

        self.assertIn("launch_burst", registered)
        self.assertIn("launch_burst", STRENGTH_SCANNERS)
        self.assertIn("launch_burst", default_sources)
        self.assertIn("SET-launch-breakthrough", {card["id"] for card in cards})

    def test_synthetic_signal_uses_previous_close_when_pre_close_is_null(self) -> None:
        candidates = _scan(_synthetic_history())

        self.assertEqual([candidate.sec_code for candidate in candidates], ["600001"])
        self.assertAlmostEqual(candidates[0].features["pct"], 0.06)
        self.assertEqual(candidates[0].features["cluster_days"], 15)
        self.assertGreaterEqual(candidates[0].features["sigma_multiple"], 3.0)

    def test_each_gate_rejects_its_synthetic_boundary_failure(self) -> None:
        cases = {}

        cluster = _synthetic_history()
        cluster.loc[cluster.index[:-1], "close"] = pd.Series(
            [8.0 + 2.0 * index / 63.0 for index in range(64)], index=cluster.index[:-1]
        )
        cluster.loc[cluster.index[:-1], "open"] = cluster.loc[cluster.index[:-1], "close"]
        cluster.loc[cluster.index[:-1], "high"] = cluster.loc[cluster.index[:-1], "close"]
        cluster.loc[cluster.index[:-1], "pre_close"] = cluster.loc[cluster.index[:-1], "close"].shift(1)
        cases["cluster"] = cluster

        first = _synthetic_history()
        first.loc[first.index[-6], ["open", "high", "close"]] = [10.0, 10.4, 10.4]
        first.loc[first.index[-5], "pre_close"] = 10.4
        cases["first"] = first

        contract = _synthetic_history(target_volume=4_000_000)
        contract.loc[contract.index[-6:-1], "volume"] = 2_000_000
        cases["contract"] = contract

        cases["big"] = _synthetic_history(target_close=10.44)

        strong_close = _synthetic_history()
        strong_close.loc[strong_close.index[-1], "high"] = 11.3
        cases["strong_close"] = strong_close

        cases["volume_expand"] = _synthetic_history(target_volume=1_490_000)

        reclaim = _synthetic_history()
        reclaim.loc[reclaim.index[-1], "open"] = 10.01
        cases["reclaim"] = reclaim

        cases["liquidity"] = _synthetic_history(target_amount=199_999_999)

        price = _synthetic_history(target_close=3_180.0)
        price.loc[price.index[:-1], ["open", "high", "low", "close", "pre_close"]] = 3_000.0
        price.loc[price.index[-1], ["open", "high", "low", "pre_close"]] = [3_000.0, 3_180.0, 2_990.0, None]
        cases["price_sanity"] = price

        for name, history in cases.items():
            with self.subTest(gate=name):
                self.assertEqual(_scan(history), [])

    def test_inclusive_volume_amount_and_close_strength_thresholds(self) -> None:
        history = _synthetic_history(target_volume=1_500_000, target_amount=200_000_000)
        history.loc[history.index[-1], "high"] = 11.0

        self.assertEqual(len(_scan(history, {"close_strength": 10.6 / 11.0})), 1)

    def test_index_rows_are_excluded_and_daily_cap_keeps_highest_scores(self) -> None:
        stocks = pd.concat(
            [
                _synthetic_history("600001", target_close=10.5),
                _synthetic_history("600002", target_close=10.6),
                _synthetic_history("600003", target_close=10.7),
            ],
            ignore_index=True,
        )
        index_rows = _synthetic_history("000852", target_close=10.9)
        index_rows["sec_type"] = "index"

        candidates = _scan(pd.concat([stocks, index_rows], ignore_index=True), {"daily_cap": 2})

        self.assertEqual([candidate.sec_code for candidate in candidates], ["600003", "600002"])
        self.assertEqual([candidate.rank for candidate in candidates], [1, 2])

    def test_kailaiying_fixture_hits_exact_2026_06_18(self) -> None:
        self.assertEqual(_fixture_hit_dates("002821", "2026-06-18", 0), ["2026-06-18"])

    def test_boe_fixture_hits_within_three_trading_days(self) -> None:
        hits = _fixture_hit_dates("000725", "2017-09-18", 3)
        self.assertIn("2017-09-18", hits)

    def test_optional_zte_fixture(self) -> None:
        if not (FIXTURE_DIR / "000063.csv").exists():
            self.skipTest("000063 fixture not cached")
        self.assertTrue(_fixture_hit_dates("000063", "2017-05-17", 3))

    def test_optional_tianqi_fixture(self) -> None:
        if not (FIXTURE_DIR / "002466.csv").exists():
            self.skipTest("002466 fixture not cached")
        self.assertTrue(_fixture_hit_dates("002466", "2017-06-13", 3))

    def test_generic_daily_and_evaluate_harnesses_persist_launch_burst(self) -> None:
        from mining.db import connect
        from mining.evaluate import run_scanners_for_dates
        from mining.scanners.launch_burst import LaunchBurstScanner
        from run_daily import _run_scanners

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dates = create_sample_market_dbs(base)
            target_date = dates["target_trade_date"]
            conn = connect(base_dir=base)
            try:
                _insert_history(conn, _synthetic_history("601999", target_date=target_date))
                with patch("run_daily.get_registered_scanners", return_value=[LaunchBurstScanner]):
                    daily = _run_scanners(conn, target_date)
                saved = conn.execute(
                    "SELECT sec_code FROM candidates WHERE strategy_id='launch_burst' AND trade_date=?",
                    (target_date,),
                ).fetchall()
                conn.execute("DELETE FROM candidates WHERE strategy_id='launch_burst'")
                conn.commit()
                evaluated = run_scanners_for_dates(
                    conn,
                    [target_date],
                    strategy_ids=["launch_burst"],
                    skip_existing=False,
                    progress_every=0,
                )
            finally:
                conn.close()

        self.assertEqual(daily[0]["strategy_id"], "launch_burst")
        self.assertEqual([row[0] for row in saved], ["601999"])
        self.assertEqual(evaluated["candidates_saved"], 1)


if __name__ == "__main__":
    unittest.main()
