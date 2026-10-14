"""Small synthetic SQLite databases in the same schema the backfill scripts write.

Numbers are random but in realistic units (amount in yuan, change_pct in percent),
so the panel's unit checks behave as they do on real data.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

INDEX_CODES = {"000001": 4.0e11, "399001": 5.0e11, "000300": 2.5e11, "000852": 1.5e11}


def trade_days(end: str, n: int) -> List[str]:
    days = pd.bdate_range(end=end, periods=n)
    return [d.strftime("%Y-%m-%d") for d in days]


def make_stock_db(path: Path, days: List[str], n_stocks: int = 300, seed: int = 1) -> None:
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE kline_daily (
             sec_type TEXT NOT NULL, sec_code TEXT NOT NULL, trade_date TEXT NOT NULL,
             open REAL, high REAL, low REAL, close REAL, pre_close REAL, change REAL,
             change_pct REAL, volume REAL, amount REAL, turnover_ratio REAL,
             source TEXT, updated_at TEXT,
             PRIMARY KEY (sec_type, sec_code, trade_date))"""
    )
    rows = []
    codes = [f"{600000 + i:06d}" for i in range(n_stocks)]
    for d in days:
        market = rng.normal(0, 1.0)
        for c in codes:
            pct = float(np.clip(market + rng.normal(0, 2.0), -10, 10))
            rows.append(("stock", c, d, None, None, None, 10.0, None, None, pct, None,
                         float(rng.lognormal(19, 1)), None, "synthetic", None))
    for c, amt in INDEX_CODES.items():
        close = 3000.0
        for d in days:
            pct = float(rng.normal(0, 1.0))
            close *= 1 + pct / 100
            rows.append(("index", c, d, None, None, None, close, None, None, pct, None,
                         amt * float(rng.uniform(0.8, 1.2)), None, "synthetic", None))
    con.executemany(f"INSERT INTO kline_daily VALUES ({','.join('?' * 15)})", rows)
    con.commit()
    con.close()


def make_concept_db(path: Path, days: List[str], n_concepts: int = 60, seed: int = 2) -> None:
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE concept_master (index_code TEXT PRIMARY KEY, name TEXT, concept_code TEXT, source TEXT, updated_at TEXT)")
    con.execute(
        """CREATE TABLE concept_kline (
             trade_date TEXT NOT NULL, concept_code TEXT NOT NULL, close REAL, pre_close REAL,
             change_pct REAL, volume REAL, amount REAL, PRIMARY KEY(trade_date, concept_code))"""
    )
    codes = [f"{885000 + i:06d}" for i in range(n_concepts)]
    con.executemany("INSERT INTO concept_master VALUES (?,?,?,?,?)",
                    [(c, f"概念{i}", c, "synthetic", None) for i, c in enumerate(codes)])
    rows = []
    for c in codes:
        close = 1000.0
        for d in days:
            pct = float(rng.normal(0, 1.5))
            close *= 1 + pct / 100
            rows.append((d.replace("-", "/"), c, close, None, pct, None, float(rng.lognormal(23, 0.8))))
    con.executemany("INSERT INTO concept_kline VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def make_etf_db(path: Path, days: List[str], seed: int = 3) -> None:
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE etf_total (trade_date TEXT PRIMARY KEY, sse_share_total REAL,
             szse_share_total REAL, total_share REAL, updated_at TEXT)"""
    )
    sse, szse = 1.5e12, 6.0e11
    rows = []
    for d in days:
        sse *= 1 + rng.normal(0, 0.003)
        szse *= 1 + rng.normal(0, 0.003)
        rows.append((d, sse, szse, sse + szse, None))
    con.executemany("INSERT INTO etf_total VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
