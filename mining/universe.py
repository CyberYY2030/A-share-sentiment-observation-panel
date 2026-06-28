from __future__ import annotations

import sqlite3

import pandas as pd

from .features import board_kind, change_pct


MAX_MARKET_CAP = 5_000 * 100_000_000
MIN_LISTING_DAYS = 87


def build_universe(conn: sqlite3.Connection, trade_date: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT
          k.sec_code,
          COALESCE(s.name, k.sec_code) AS sec_name,
          k.open,
          k.high,
          k.low,
          k.close,
          k.pre_close,
          k.change,
          k.change_pct,
          k.volume,
          k.amount,
          k.turnover_ratio,
          m.total_mv,
          l.list_date
        FROM ash.kline_daily k
        LEFT JOIN ash.stock_info s ON s.sec_code = k.sec_code
        LEFT JOIN stock_market_cap m ON m.sec_code = k.sec_code
        LEFT JOIN stock_listing l ON l.sec_code = k.sec_code
        WHERE k.sec_type='stock'
          AND k.trade_date=?
          AND k.amount > 0
          AND k.volume > 0
        """,
        conn,
        params=[trade_date],
    )
    if df.empty:
        return df

    df["sec_code"] = df["sec_code"].astype(str).str.zfill(6)
    df["sec_name"] = df["sec_name"].fillna(df["sec_code"])
    df["change_pct"] = df["change_pct"].fillna(df.apply(change_pct, axis=1))
    df["board"] = df["sec_code"].map(board_kind)
    df["mv_missing"] = df["total_mv"].isna()
    df["listing_missing"] = df["list_date"].isna()

    df = df[~df["sec_code"].str.startswith(("8", "4", "9"))].copy()
    df = df[
        ~(
            (df["open"] == df["high"])
            & (df["high"] == df["low"])
            & (df["low"] == df["close"])
        )
    ].copy()
    df = df[~df["sec_name"].astype(str).str.upper().str.contains("ST", na=False)].copy()
    df = df[(df["total_mv"].isna()) | (df["total_mv"] <= MAX_MARKET_CAP)].copy()

    listing_dates = pd.to_datetime(df["list_date"], errors="coerce")
    age_days = (pd.Timestamp(trade_date) - listing_dates).dt.days
    df = df[df["listing_missing"] | (age_days >= MIN_LISTING_DAYS)].copy()

    return df.reset_index(drop=True)

