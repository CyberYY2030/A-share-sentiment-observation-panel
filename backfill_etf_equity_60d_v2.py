"""Backfill & Update Equity ETF shares (份额) into a dedicated SQLite DB.

Goal
----
1) Use AKShare ETF scale endpoints to fetch *equity* ETF shares.
2) Apply the same filtering rules you used in the dashboard:
   - Exclude names containing: '债', '货币', 'REIT' (case-insensitive for REIT)
   - SZSE: keep only 股票基金 / 混合基金 (when category columns exist)
   - SSE: exclude 跨境 (when type columns exist)
3) Backfill last N trade dates (default 60) and save into an ETF-only DB.

Notes
-----
- AKShare currently documents fund_etf_scale_sse(date=YYYYMMDD) as date-aware,
  while fund_etf_scale_szse() may only return the latest day in some versions.
  This script will *auto-detect* whether SZSE supports a date argument.
  If not supported, it will save only the latest snapshot for SZSE.

Run
---
python backfill_etf_equity_60d.py --db etf_mvp.db --days 60 --ref-db a_share_mvp.db

Outputs
-------
- SQLite: tables etf_master / etf_scale / etf_total
- CSV: equity_etf_master_YYYYMMDD.csv (in --out-dir)
"""
from __future__ import annotations
from pathlib import Path


import argparse
import datetime as dt
import inspect
import os
import re
import sqlite3
import sys
import time
from typing import Iterable, List, Optional, Tuple

import pandas as pd


def _now_ts() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def log(msg: str) -> None:
    print(f"[ETF { _now_ts() }] {msg}")


def sqlite_connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_master (
            fund_code TEXT PRIMARY KEY,
            exchange TEXT,
            fund_name TEXT,
            category TEXT,
            is_equity INTEGER,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_scale (
            trade_date TEXT,
            exchange TEXT,
            fund_code TEXT,
            fund_name TEXT,
            share REAL,
            source TEXT,
            updated_at TEXT,
            PRIMARY KEY (trade_date, exchange, fund_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_total (
            trade_date TEXT PRIMARY KEY,
            sse_share_total REAL,
            szse_share_total REAL,
            total_share REAL,
            updated_at TEXT
        )
        """
    )
    conn.commit()


def _pick_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    cols = list(df.columns)
    for c in candidates:
        if c in cols:
            return c
    # fuzzy contains
    for c in candidates:
        for col in cols:
            if c in col:
                return col
    return None


def _share_to_float(df: pd.DataFrame, share_col: str) -> pd.Series:
    s = pd.to_numeric(df[share_col], errors="coerce")
    # infer unit by column name
    col = share_col
    if "万" in col:
        s = s * 1e4
    if "亿" in col:
        s = s * 1e8
    return s


def _normalize_code(code: str) -> str:
    s = str(code).strip()
    s = re.sub(r"\D", "", s)
    return s.zfill(6) if s else ""


def _filter_equity_etf(df: pd.DataFrame, exchange: str) -> pd.DataFrame:
    # name column
    name_col = _pick_col(df, ["基金简称", "证券简称", "基金名称", "名称", "简称"])
    if name_col is None:
        name_col = df.columns[0]
    names = df[name_col].astype(str)

    bad = names.str.contains("债", na=False) | names.str.contains("货币", na=False) | names.str.contains(
        "REIT", case=False, na=False) | names.str.contains("港股", na=False) | names.str.contains("恒生", na=False)
    out = df.loc[~bad].copy()

    if exchange.upper() == "SSE":
        # exclude cross-border when possible
        type_col = _pick_col(out, ["ETF类型", "类型", "投资类别", "基金类别"])
        if type_col is not None:
            out = out.loc[~out[type_col].astype(str).str.contains("跨境", na=False)].copy()
    else:
        # SZSE keep only 股票基金 / 混合基金 if we can
        cat_col = _pick_col(out, ["投资类别", "基金类别", "类型"])
        if cat_col is not None:
            keep = out[cat_col].astype(str).isin(["股票基金", "混合基金"])
            # if the column is not that style, fall back to keyword check
            if keep.sum() == 0:
                keep = out[cat_col].astype(str).str.contains("股票|混合", na=False)
            out = out.loc[keep].copy()
    return out


def _etf_master_from_scale(df: pd.DataFrame, exchange: str) -> pd.DataFrame:
    code_col = _pick_col(df, ["基金代码", "证券代码", "代码"])
    name_col = _pick_col(df, ["基金简称", "证券简称", "基金名称", "名称", "简称"])
    cat_col = _pick_col(df, ["投资类别", "基金类别", "ETF类型", "类型"])
    if code_col is None:
        raise RuntimeError(f"cannot find fund code column for {exchange}: cols={list(df.columns)}")
    if name_col is None:
        name_col = code_col
    if cat_col is None:
        cat_col = ""

    out = pd.DataFrame(
        {
            "fund_code": df[code_col].map(_normalize_code),
            "exchange": exchange.upper(),
            "fund_name": df[name_col].astype(str),
            "category": df[cat_col].astype(str) if cat_col else "",
        }
    )
    out = out.loc[out["fund_code"].str.len() == 6].drop_duplicates(subset=["exchange", "fund_code"])
    out["is_equity"] = 1
    return out


def load_trade_dates_from_ref_db(ref_db: str, days: int) -> List[str]:
    if not ref_db or not os.path.exists(ref_db):
        return []
    conn = sqlite_connect(ref_db)
    try:
        # try common table/col names
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            if "trade_date" in cols:
                # prefer index rows if available
                q = f"SELECT DISTINCT trade_date FROM {t} WHERE trade_date IS NOT NULL ORDER BY trade_date DESC LIMIT ?"
                rows = conn.execute(q, (max(days, 120),)).fetchall()
                ds = [str(r[0]) for r in rows]
                ds = [d.replace("/", "-") for d in ds]
                ds = sorted(set(ds))
                if len(ds) >= 10:
                    return ds[-days:]
        return []
    finally:
        conn.close()


def get_trade_dates_via_baostock(days: int, asof: str) -> List[str]:
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    try:
        # request a calendar window and filter trade days
        end = dt.datetime.strptime(asof, "%Y-%m-%d").date()
        start = end - dt.timedelta(days=days * 3)
        rs = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
        items = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            # ['calendar_date', 'is_trading_day']
            cal, is_td = row[0], row[1]
            if str(is_td) == "1":
                items.append(str(cal))
        items = sorted(items)
        return items[-days:]
    finally:
        bs.logout()


def _safe_call(fn, *args, **kwargs):
    # basic retries for transient network issues
    last = None
    for i in range(3):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


def fetch_scale_sse(ak, yyyymmdd: str) -> pd.DataFrame:
    return _safe_call(ak.fund_etf_scale_sse, date=yyyymmdd)


def fetch_scale_szse(ak, yyyymmdd: Optional[str]) -> pd.DataFrame:
    fn = ak.fund_etf_scale_szse
    sig = None
    try:
        sig = inspect.signature(fn)
    except Exception:
        sig = None

    if sig and "date" in sig.parameters and yyyymmdd is not None:
        return _safe_call(fn, date=yyyymmdd)
    # some versions use trade_date
    if sig and "trade_date" in sig.parameters and yyyymmdd is not None:
        return _safe_call(fn, trade_date=yyyymmdd)
    # fallback: latest only
    return _safe_call(fn)


def upsert_master(conn: sqlite3.Connection, master: pd.DataFrame) -> None:
    now = _now_ts()
    rows = []
    for _, r in master.iterrows():
        rows.append(
            (
                r["fund_code"],
                r["exchange"],
                r["fund_name"],
                r.get("category", ""),
                int(r.get("is_equity", 1)),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO etf_master (fund_code, exchange, fund_name, category, is_equity, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(fund_code) DO UPDATE SET
          exchange=excluded.exchange,
          fund_name=excluded.fund_name,
          category=excluded.category,
          is_equity=excluded.is_equity,
          updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def upsert_scale(conn: sqlite3.Connection, trade_date: str, exchange: str, df: pd.DataFrame, source: str) -> int:
    code_col = _pick_col(df, ["基金代码", "证券代码", "代码"])
    name_col = _pick_col(df, ["基金简称", "证券简称", "基金名称", "名称", "简称"])
    share_col = _pick_col(df, ["基金份额", "份额"])
    if code_col is None or share_col is None:
        raise RuntimeError(f"missing columns for scale: code_col={code_col}, share_col={share_col}, cols={list(df.columns)}")
    if name_col is None:
        name_col = code_col
    now = _now_ts()
    tmp = df.copy()
    tmp["fund_code"] = tmp[code_col].map(_normalize_code)
    tmp["fund_name"] = tmp[name_col].astype(str)
    tmp["share"] = _share_to_float(tmp, share_col)
    tmp = tmp.loc[tmp["fund_code"].str.len() == 6]
    rows = [
        (trade_date, exchange.upper(), r.fund_code, r.fund_name, float(r.share) if pd.notna(r.share) else None, source, now)
        for r in tmp.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO etf_scale (trade_date, exchange, fund_code, fund_name, share, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, exchange, fund_code) DO UPDATE SET
          fund_name=excluded.fund_name,
          share=excluded.share,
          source=excluded.source,
          updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_total(conn: sqlite3.Connection, trade_date: str) -> None:
    row_sse = conn.execute(
        "SELECT SUM(share) AS s FROM etf_scale WHERE trade_date=? AND exchange='SSE'", (trade_date,)
    ).fetchone()
    row_sz = conn.execute(
        "SELECT SUM(share) AS s FROM etf_scale WHERE trade_date=? AND exchange='SZSE'", (trade_date,)
    ).fetchone()
    sse = float(row_sse[0]) if row_sse and row_sse[0] is not None else None
    sz = float(row_sz[0]) if row_sz and row_sz[0] is not None else None
    total = None
    if sse is not None and sz is not None:
        total = sse + sz
    elif sse is not None:
        total = sse
    elif sz is not None:
        total = sz
    now = _now_ts()
    conn.execute(
        """
        INSERT INTO etf_total (trade_date, sse_share_total, szse_share_total, total_share, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET
          sse_share_total=excluded.sse_share_total,
          szse_share_total=excluded.szse_share_total,
          total_share=excluded.total_share,
          updated_at=excluded.updated_at
        """,
        (trade_date, sse, sz, total, now),
    )
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="etf_mvp.db", help="ETF sqlite db path")
    ap.add_argument("--days", type=int, default=60, help="backfill trade days")
    ap.add_argument("--asof", default=None, help="asof date YYYY-MM-DD (default: today)")
    ap.add_argument("--ref-db", default="a_share_mvp.db", help="ref db to get trading dates")
    ap.add_argument("--out-dir", default=os.path.join("data", "output"), help="output directory")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    asof = args.asof or dt.date.today().isoformat()

    # trade dates
    dates = load_trade_dates_from_ref_db(args.ref_db, args.days)
    if not dates:
        log(f"ref-db trade dates unavailable, fallback to baostock calendar")
        dates = get_trade_dates_via_baostock(args.days, asof)
    if not dates:
        raise RuntimeError("cannot build trading date list")
    asof_td = dates[-1]
    log(f"trade dates: {dates[0]} ... {dates[-1]} (n={len(dates)})")

    import akshare as ak

    log(f"akshare version: {getattr(ak, '__version__', 'unknown')}")

    # fetch master lists using asof trade date
    yyyymmdd = asof_td.replace("-", "")
    df_sse = fetch_scale_sse(ak, yyyymmdd)
    df_sse = _filter_equity_etf(df_sse, "SSE")
    master_sse = _etf_master_from_scale(df_sse, "SSE")

    df_sz = fetch_scale_szse(ak, yyyymmdd)
    df_sz = _filter_equity_etf(df_sz, "SZSE")
    master_sz = _etf_master_from_scale(df_sz, "SZSE")

    master = pd.concat([master_sse, master_sz], ignore_index=True)
    # keep only equity
    master = master.drop_duplicates(subset=["exchange", "fund_code"])
    log(f"equity etf master: SSE={len(master_sse)} SZSE={len(master_sz)} total={len(master)}")
    master_csv = os.path.join(args.out_dir, f"equity_etf_master_{yyyymmdd}.csv")
    master.to_csv(master_csv, index=False, encoding="utf-8-sig")
    log(f"master csv exported: {master_csv}")

    # db
    conn = sqlite_connect(args.db)
    ensure_schema(conn)
    upsert_master(conn, master)

    # backfill SSE shares for each date
    sse_codes = set(master.loc[master.exchange == "SSE", "fund_code"].tolist())
    sz_codes = set(master.loc[master.exchange == "SZSE", "fund_code"].tolist())

    sz_supports_date = False
    try:
        sig = inspect.signature(ak.fund_etf_scale_szse)
        sz_supports_date = ("date" in sig.parameters) or ("trade_date" in sig.parameters)
    except Exception:
        sz_supports_date = False
    log(f"fund_etf_scale_szse date-aware: {sz_supports_date}")

    total_rows = 0
    for i, d in enumerate(dates, 1):
        ymd = d.replace("-", "")
        # SSE
        try:
            df = fetch_scale_sse(ak, ymd)
            df = _filter_equity_etf(df, "SSE")
            code_col = _pick_col(df, ["基金代码", "证券代码", "代码"])
            if code_col is not None:
                df = df.loc[df[code_col].map(_normalize_code).isin(sse_codes)].copy()
            n = upsert_scale(conn, d, "SSE", df, source="akshare")
            total_rows += n
        except Exception as e:
            log(f"SSE {d} fetch failed: {e}")

        # SZSE (maybe latest only)
        if sz_supports_date:
            try:
                df2 = fetch_scale_szse(ak, ymd)
                df2 = _filter_equity_etf(df2, "SZSE")
                code_col2 = _pick_col(df2, ["基金代码", "证券代码", "代码"])
                if code_col2 is not None:
                    df2 = df2.loc[df2[code_col2].map(_normalize_code).isin(sz_codes)].copy()
                n2 = upsert_scale(conn, d, "SZSE", df2, source="akshare")
                total_rows += n2
            except Exception as e:
                log(f"SZSE {d} fetch failed: {e}")
        elif d == asof_td:
            try:
                df2 = df_sz.copy()
                code_col2 = _pick_col(df2, ["基金代码", "证券代码", "代码"])
                if code_col2 is not None:
                    df2 = df2.loc[df2[code_col2].map(_normalize_code).isin(sz_codes)].copy()
                n2 = upsert_scale(conn, d, "SZSE", df2, source="akshare_latest")
                total_rows += n2
            except Exception as e:
                log(f"SZSE latest snapshot save failed: {e}")

        # totals
        upsert_total(conn, d)

        if i % 10 == 0 or i == len(dates):
            log(f"progress {i}/{len(dates)} saved_rows~{total_rows}")

    conn.close()
    log(f"DONE. db={args.db} total_rows={total_rows}")


if __name__ == "__main__":
    main()
