from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from .db import now_str


def _normalize_market_cap_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["sec_code", "total_mv"])
    code_col = next(
        (
            name
            for name in df.columns
            if str(name) in {"代码", "证券代码", "sec_code", "code"}
        ),
        None,
    )
    mv_col = next(
        (
            name
            for name in df.columns
            if str(name) in {"总市值", "总市值-动态", "total_mv", "market_cap"}
        ),
        None,
    )
    if code_col is None or mv_col is None:
        return pd.DataFrame(columns=["sec_code", "total_mv"])
    out = df[[code_col, mv_col]].copy()
    out.columns = ["sec_code", "total_mv"]
    out["sec_code"] = out["sec_code"].astype(str).str.extract(r"(\d{6})", expand=False)
    out["total_mv"] = pd.to_numeric(out["total_mv"], errors="coerce")
    return out.dropna(subset=["sec_code", "total_mv"])


def refresh_basics(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
    market_cap_frame: pd.DataFrame | None = None,
    listing_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    snapshot_date = trade_date or conn.execute(
        "SELECT MAX(trade_date) FROM ash.kline_daily WHERE sec_type='stock'"
    ).fetchone()[0]
    result: dict[str, Any] = {
        "market_cap_rows": 0,
        "listing_rows": 0,
        "messages": [],
    }

    if market_cap_frame is None:
        try:
            import akshare as ak  # type: ignore

            market_cap_frame = ak.stock_zh_a_spot_em()
        except Exception as exc:
            result["messages"].append(f"market cap refresh skipped: {exc}")

    if market_cap_frame is not None:
        normalized = _normalize_market_cap_frame(market_cap_frame)
        for row in normalized.itertuples(index=False):
            conn.execute(
                """
                INSERT OR REPLACE INTO stock_market_cap (sec_code, total_mv, snapshot_date, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (row.sec_code, float(row.total_mv), snapshot_date, now_str()),
            )
        result["market_cap_rows"] = len(normalized)

    if listing_frame is not None and not listing_frame.empty:
        copy = listing_frame.copy()
        if {"sec_code", "list_date"}.issubset(copy.columns):
            for row in copy.itertuples(index=False):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stock_listing (sec_code, list_date, snapshot_date, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (row.sec_code, row.list_date, snapshot_date, now_str()),
                )
            result["listing_rows"] = len(copy)
    else:
        result["messages"].append("listing refresh skipped; keeping existing snapshot")

    conn.commit()
    return result

