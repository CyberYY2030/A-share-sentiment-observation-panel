
# -*- coding: utf-8 -*-
"""
Backfill沪深A股过去N个交易日(日线K)到本地SQLite，并保持字段与(概念指数/指数等)一致。

路线A：使用 BaoStock 获取个股日线（避免 Eastmoney push 接口 502/风控问题）。
可选：把少量指数、同花顺概念指数K线（来自 adata）写进同一张 kline_daily 表。

安装：
  pip install baostock pandas

运行示例：
  python backfill_baostock_hsA_60d.py --db a_share_mvp.db --days 60
  python backfill_baostock_hsA_60d.py --db a_share_mvp.db --days 60 --probe
  python backfill_baostock_hsA_60d.py --db a_share_mvp.db --days 60 --with-indexes
  python backfill_baostock_hsA_60d.py --db a_share_mvp.db --days 60 --with-concepts --concept-top 20

说明：
- BaoStock 代码格式：sh.600000 / sz.000001
- 本脚本仅抓“沪深A股”（过滤掉指数、B股、北交所、基金等）。
- 数据写入 kline_daily(sec_type, sec_code, trade_date, ...)；并建立 view: stock_kline_d 兼容旧表名。
"""

from __future__ import annotations

import argparse
import os
import time
import sqlite3
from typing import List, Optional

import pandas as pd

try:
    import baostock as bs
except Exception as e:
    print("ERROR: cannot import baostock. Run: pip install baostock")
    raise

# -----------------------------
# schema
# -----------------------------

KLINE_COLS = [
    "sec_type",         # 'stock' | 'index' | 'concept_ths' ...
    "sec_code",         # 纯代码：股票=6位；指数/概念=原始代码(如 000300 / 885530)
    "trade_date",       # YYYY-MM-DD
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "change_pct",       # percent, e.g. 1.23
    "volume",
    "amount",
    "turnover_ratio",   # percent
    "source",           # 'baostock' / 'adata_ths' ...
    "updated_at",       # ISO timestamp
]

CREATE_KLINE_SQL = f"""
CREATE TABLE IF NOT EXISTS kline_daily (
  sec_type TEXT NOT NULL,
  sec_code TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  pre_close REAL,
  change REAL,
  change_pct REAL,
  volume REAL,
  amount REAL,
  turnover_ratio REAL,
  source TEXT,
  updated_at TEXT,
  PRIMARY KEY (sec_type, sec_code, trade_date)
);
"""

CREATE_STOCK_INFO_SQL = """
CREATE TABLE IF NOT EXISTS stock_info (
  sec_code TEXT PRIMARY KEY,     -- 6位
  bs_code  TEXT,                 -- sh.600000
  name     TEXT,
  trade_status TEXT,
  updated_at TEXT
);
"""

CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_daily(trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_kline_type_code ON kline_daily(sec_type, sec_code);",
]

# 兼容旧代码：如果你之前用 stock_kline_d 这个表名
CREATE_VIEW_STOCK = """
CREATE VIEW IF NOT EXISTS stock_kline_d AS
SELECT
  sec_code AS stock_code,
  trade_date,
  open, high, low, close,
  pre_close AS pre_close,
  change,
  change_pct,
  volume,
  amount,
  turnover_ratio
FROM kline_daily
WHERE sec_type='stock';
"""


def now_iso() -> str:
    return pd.Timestamp.now().isoformat(timespec="seconds")


def db_connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_KLINE_SQL)
    cur.execute(CREATE_STOCK_INFO_SQL)
    cur.execute(CREATE_META_SQL)
    for sql in CREATE_INDEXES:
        cur.execute(sql)
    cur.execute(CREATE_VIEW_STOCK)
    conn.commit()


def upsert_kline(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df2 = df.copy()
    for c in KLINE_COLS:
        if c not in df2.columns:
            df2[c] = None
    df2 = df2[KLINE_COLS]
    sql = f"""
    INSERT OR REPLACE INTO kline_daily ({",".join(KLINE_COLS)})
    VALUES ({",".join(["?"]*len(KLINE_COLS))})
    """
    cur = conn.cursor()
    cur.executemany(sql, df2.itertuples(index=False, name=None))
    return cur.rowcount if cur.rowcount is not None else len(df2)


def upsert_stock_info(conn: sqlite3.Connection, info_df: pd.DataFrame) -> int:
    if info_df is None or info_df.empty:
        return 0
    info = info_df.copy()
    for c in ["sec_code", "bs_code", "name", "trade_status"]:
        if c not in info.columns:
            info[c] = None
    info["updated_at"] = now_iso()
    sql = """
    INSERT OR REPLACE INTO stock_info(sec_code, bs_code, name, trade_status, updated_at)
    VALUES (?,?,?,?,?)
    """
    cur = conn.cursor()
    rows = list(info[["sec_code", "bs_code", "name", "trade_status", "updated_at"]].itertuples(index=False, name=None))
    cur.executemany(sql, rows)
    return cur.rowcount if cur.rowcount is not None else len(rows)


# -----------------------------
# BaoStock helpers
# -----------------------------

def bs_login() -> None:
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_code} {lg.error_msg}")


def bs_logout() -> None:
    try:
        bs.logout()
    except Exception:
        pass


def get_trade_calendar(start_date: str, end_date: str) -> pd.DataFrame:
    rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
    if rs.error_code != "0":
        raise RuntimeError(f"query_trade_dates failed: {rs.error_code} {rs.error_msg}")
    return rs.get_data()  # expected cols: calendar_date, is_trading_day


def get_last_trading_day() -> str:
    """
    返回最近一个“已完成”的交易日（避免当天日线尚未入库/更新问题）。
    逻辑：以今天为上限，向前取一段交易日历，找最后一个 is_trading_day=1 且 < today 的日期。
    """
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    cal = get_trade_calendar(start, today)
    cal = cal[cal["is_trading_day"] == "1"].copy()
    cal = cal[cal["calendar_date"] < today]
    if cal.empty:
        return (pd.Timestamp.today() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return cal["calendar_date"].iloc[-1]


def last_n_trading_days(n: int, asof: Optional[str] = None) -> List[str]:
    asof_date = asof or get_last_trading_day()
    start = (pd.Timestamp(asof_date) - pd.Timedelta(days=max(120, n * 3))).strftime("%Y-%m-%d")
    cal = get_trade_calendar(start, asof_date)
    days = cal[cal["is_trading_day"] == "1"]["calendar_date"].tolist()
    days = [d for d in days if d <= asof_date]
    return days if len(days) < n else days[-n:]


def is_hs_a_share(bs_code: str) -> bool:
    """
    过滤“沪深A股”：
    - sh: 600/601/603/605/688 开头
    - sz: 000/001/002/003/300/301 开头
    排除：北交所 bj., B股(900/200), 指数(399xxx/sh.000xxx 等), 基金等。
    """
    if not isinstance(bs_code, str) or "." not in bs_code:
        return False
    bs_code = bs_code.lower()
    if bs_code.startswith("bj."):
        return False
    if not (bs_code.startswith("sh.") or bs_code.startswith("sz.")):
        return False
    code = bs_code.split(".", 1)[1]
    if len(code) != 6 or not code.isdigit():
        return False

    if bs_code.startswith("sh."):
        if code.startswith("900"):  # B股
            return False
        return code.startswith(("600", "601", "603", "605", "688"))
    if bs_code.startswith("sz."):
        if code.startswith("200"):  # B股
            return False
        if code.startswith("399"):  # 指数
            return False
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    return False


def fetch_all_hs_a_stock_list(asof: str) -> pd.DataFrame:
    rs = bs.query_all_stock(day=asof)
    if rs.error_code != "0":
        raise RuntimeError(f"query_all_stock failed: {rs.error_code} {rs.error_msg}")
    df = rs.get_data()
    if df is None or df.empty:
        raise RuntimeError(f"query_all_stock returned empty for {asof} (is it a trading day?)")
    df["code"] = df["code"].astype(str).str.lower()
    df = df[df["code"].apply(is_hs_a_share)].copy()
    df.rename(columns={"code": "bs_code", "code_name": "name", "tradeStatus": "trade_status"}, inplace=True)
    df["sec_code"] = df["bs_code"].str.split(".", n=1).str[1]
    return df[["sec_code", "bs_code", "name", "trade_status"]].reset_index(drop=True)


def fetch_kline_baostock(bs_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    query_history_k_data_plus(日K)
    fields：date,code,open,high,low,close,preclose,volume,amount,pctChg,turn
    """
    fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn"
    rs = bs.query_history_k_data_plus(
        bs_code,
        fields,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3",  # 不复权
    )
    if rs.error_code != "0":
        raise RuntimeError(f"history_k_data_plus failed for {bs_code}: {rs.error_code} {rs.error_msg}")

    df = rs.get_data()
    if df is None or df.empty:
        return pd.DataFrame()

    df.rename(columns={"date": "trade_date"}, inplace=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg", "turn"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    sec_code = bs_code.split(".", 1)[1]
    out = pd.DataFrame({
        "sec_type": "stock",
        "sec_code": sec_code,
        "trade_date": df["trade_date"],
        "open": df["open"],
        "high": df["high"],
        "low": df["low"],
        "close": df["close"],
        "pre_close": df["preclose"],
        "change_pct": df["pctChg"],
        "volume": df["volume"],
        "amount": df["amount"],
        "turnover_ratio": df["turn"],
        "source": "baostock",
        "updated_at": now_iso(),
    })
    out["change"] = out["close"] - out["pre_close"]
    return out


def fetch_indexes_baostock(start_date: str, end_date: str) -> pd.DataFrame:
    index_map = {
        "000300": "sh.000300",  # 沪深300
        "000852": "sh.000852",  # 中证1000
        "000001": "sh.000001",  # 上证综指
        "399001": "sz.399001",  # 深证成指
    }
    frames = []
    for sec_code, bs_code in index_map.items():
        try:
            df = fetch_kline_baostock(bs_code, start_date, end_date)
            if df.empty:
                continue
            df["sec_type"] = "index"
            df["sec_code"] = sec_code
            df["source"] = "baostock"
            frames.append(df)
        except Exception as e:
            print(f"[INDEX] FAIL {bs_code}: {e}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# -----------------------------
# Optional: adata concept kline (hot list only)
# -----------------------------

def try_import_adata():
    try:
        import adata  # type: ignore
        return adata
    except Exception:
        return None


def fetch_hot_concepts_adata(adata_mod, top_n: int = 20) -> pd.DataFrame:
    market = getattr(adata_mod.stock, "market", None)
    if market is None:
        raise RuntimeError("adata.stock.market not found")

    def _collect_funcs(obj) -> list:
        """Collect possible hot-concept functions defensively (adata API naming can vary by version)."""
        out = []
        # 1) Known/common names
        for name in [
            "hot_concept_20_ths",
            "hot_concept_ths",
            "hot_concept_100_ths",
            "hot_concept",
            "hot_concept_20",
            "hot_concept_100",
        ]:
            fn = getattr(obj, name, None)
            if callable(fn):
                out.append((name, fn))
        # 2) Heuristic discovery (handles renamed APIs)
        for name in dir(obj):
            low = name.lower()
            if ("hot" in low) and ("concept" in low):
                fn = getattr(obj, name, None)
                if callable(fn):
                    out.append((name, fn))
        # de-dup by name
        seen = set()
        uniq = []
        for n, f in out:
            if n in seen:
                continue
            seen.add(n)
            uniq.append((n, f))
        return uniq

    cand = []
    cand.extend(_collect_funcs(market))
    # Some versions may put concept endpoints under stock.info
    info = getattr(adata_mod.stock, "info", None)
    if info is not None:
        cand.extend(_collect_funcs(info))

    if not cand:
        raise RuntimeError(
            "adata has no discoverable hot concept function. "
            "Hint: run without --with-concepts, or provide concept codes manually."
        )

    df = None
    last_err = None
    last_name = None
    for name, fn in cand:
        try:
            # Different versions accept different params
            try:
                tmp = fn()
            except TypeError:
                try:
                    tmp = fn(top_n)
                except TypeError:
                    tmp = fn(n=top_n)
            if isinstance(tmp, pd.DataFrame) and not tmp.empty:
                df = tmp
                last_name = name
                break
        except Exception as e:
            last_err = e

    if df is None or df.empty:
        raise RuntimeError(f"adata hot concept returned empty. last_fn={last_name} last_err={last_err}")

    df = df.copy()
    if "concept_code" in df.columns:
        df["index_code"] = df["concept_code"]
    if "index_code" not in df.columns:
        for alt in ["concept_id", "code", "indexCode"]:
            if alt in df.columns:
                df["index_code"] = df[alt]
                break
    if "concept_name" not in df.columns:
        for alt in ["name", "conceptName", "index_name"]:
            if alt in df.columns:
                df["concept_name"] = df[alt]
                break
    df["index_code"] = df["index_code"].astype(str)
    df = df.head(top_n).reset_index(drop=True)
    return df[["index_code", "concept_name"]]


def fetch_concept_kline_adata(adata_mod, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    market = getattr(adata_mod.stock, "market", None)
    if market is None:
        raise RuntimeError("adata.stock.market not found")

    cand = []
    for name in ["get_market_concept_ths", "concept_kline_ths", "get_concept_kline_ths", "concept_kline"]:
        fn = getattr(market, name, None)
        if callable(fn):
            cand.append(fn)
    if not cand:
        raise RuntimeError("adata has no THS concept kline function")

    df = None
    last_err = None
    for fn in cand:
        try:
            try:
                df = fn(index_code=index_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
            except TypeError:
                df = fn(index_code=index_code, start_date=start_date, end_date=end_date)
            if isinstance(df, pd.DataFrame) and not df.empty:
                break
        except Exception as e:
            last_err = e
            df = None
    if df is None or df.empty:
        raise RuntimeError(f"concept kline empty for {index_code}. last_err={last_err}")

    df = df.copy()
    if "trade_date" not in df.columns:
        for alt in ["date", "dt", "tradeDate"]:
            if alt in df.columns:
                df["trade_date"] = df[alt]
                break
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)].copy()
    if df.empty:
        return pd.DataFrame()

    for c in ["open", "high", "low", "close", "volume", "amount", "change", "change_pct"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    out = pd.DataFrame({
        "sec_type": "concept_ths",
        "sec_code": str(index_code),
        "trade_date": df["trade_date"],
        "open": df.get("open"),
        "high": df.get("high"),
        "low": df.get("low"),
        "close": df.get("close"),
        "pre_close": None,
        "change": df.get("change"),
        "change_pct": df.get("change_pct"),
        "volume": df.get("volume"),
        "amount": df.get("amount"),
        "turnover_ratio": None,
        "source": "adata_ths",
        "updated_at": now_iso(),
    })
    out = out.sort_values("trade_date")
    out["pre_close"] = out["close"].shift(1)
    if out["change"].isna().all():
        out["change"] = out["close"] - out["pre_close"]
    if out["change_pct"].isna().all():
        out["change_pct"] = (out["close"] / out["pre_close"] - 1.0) * 100.0
    return out


# -----------------------------
# main
# -----------------------------

def probe_once(db_path: str) -> None:
    conn = db_connect(db_path)
    ensure_schema(conn)
    bs_login()
    try:
        asof = get_last_trading_day()
        days = last_n_trading_days(10, asof=asof)
        start_date, end_date = days[0], days[-1]
        print(f"[PROBE] asof={asof} range={start_date}..{end_date}")

        lst = fetch_all_hs_a_stock_list(asof)
        print(f"[PROBE] hsA list size={len(lst)} sample=\n{lst.head(3)}")
        upsert_stock_info(conn, lst.head(200))

        for bs_code in ["sh.600000", "sz.000001"]:
            df = fetch_kline_baostock(bs_code, start_date, end_date)
            print(f"[PROBE] {bs_code} rows={len(df)} head=\n{df.head(2)}")
            upsert_kline(conn, df)

        out = pd.read_sql_query(
            "SELECT sec_type, sec_code, trade_date, close FROM kline_daily ORDER BY trade_date DESC LIMIT 5",
            conn,
        )
        print("[PROBE] readback:\n", out)
        print("[PROBE] OK")
    finally:
        bs_logout()
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="a_share_mvp.db", help="sqlite db path")
    ap.add_argument("--days", type=int, default=60, help="trading days to backfill")
    ap.add_argument("--asof", default=None, help="as-of trading date YYYY-MM-DD (default: last completed trading day)")
    ap.add_argument("--sleep", type=float, default=0.0, help="sleep seconds between stocks to reduce rate-limit")
    ap.add_argument("--probe", action="store_true", help="run small probe and exit")
    ap.add_argument("--with-indexes", action="store_true", help="also backfill common indexes into sec_type='index'")
    ap.add_argument("--with-concepts", action="store_true", help="also backfill THS hot concepts (requires adata)")
    ap.add_argument("--concept-top", type=int, default=20, help="how many hot concepts to fetch")
    ap.add_argument("--limit", type=int, default=0, help="limit number of stocks (debug)")
    args = ap.parse_args()

    if args.probe:
        probe_once(args.db)
        return

    conn = db_connect(args.db)
    ensure_schema(conn)
    bs_login()
    try:
        asof = args.asof or get_last_trading_day()
        td = last_n_trading_days(args.days, asof=asof)
        if len(td) < 5:
            raise RuntimeError(f"too few trading days from baostock calendar: {len(td)}")
        start_date, end_date = td[0], td[-1]
        print(f"[RUN] days={len(td)} asof={asof} range={start_date}..{end_date}")

        stock_list = fetch_all_hs_a_stock_list(asof)
        if args.limit and args.limit > 0:
            stock_list = stock_list.head(args.limit).copy()
        upsert_stock_info(conn, stock_list)
        conn.commit()
        print(f"[RUN] stock_info saved: {len(stock_list)}")

        total = len(stock_list)
        done, fail = 0, 0
        t0 = time.time()

        batch_frames: List[pd.DataFrame] = []
        BATCH_N = 200

        for i, row in enumerate(stock_list.itertuples(index=False), start=1):
            bs_code = row.bs_code
            try:
                df = fetch_kline_baostock(bs_code, start_date, end_date)
                if df.empty:
                    fail += 1
                else:
                    batch_frames.append(df)
                    done += 1
                if args.sleep > 0:
                    time.sleep(args.sleep)
            except Exception as e:
                fail += 1
                if i <= 5:
                    print(f"[STOCK] FAIL {bs_code}: {e}")

            if len(batch_frames) >= BATCH_N:
                big = pd.concat(batch_frames, ignore_index=True)
                upsert_kline(conn, big)
                conn.commit()
                batch_frames = []

            if i % 200 == 0 or i == total:
                dt = time.time() - t0
                print(f"[RUN] progress {i}/{total} done={done} fail={fail} elapsed={dt:.1f}s")

        if batch_frames:
            big = pd.concat(batch_frames, ignore_index=True)
            upsert_kline(conn, big)
            conn.commit()

        if args.with_indexes:
            idx_df = fetch_indexes_baostock(start_date, end_date)
            if not idx_df.empty:
                upsert_kline(conn, idx_df)
                conn.commit()
                print(f"[INDEX] saved rows={len(idx_df)}")

        if args.with_concepts:
            adata_mod = try_import_adata()
            if adata_mod is None:
                print("[CONCEPT] skip: cannot import adata (pip install adata)")
            else:
                try:
                    hot = fetch_hot_concepts_adata(adata_mod, top_n=args.concept_top)
                except Exception as e:
                    print(f"[CONCEPT] skip: cannot fetch hot concept list from adata ({e})")
                    hot = pd.DataFrame()

                if not hot.empty:
                    print(f"[CONCEPT] hot size={len(hot)} sample=\n{hot.head(3)}")
                frames = []
                for j, r in enumerate(hot.itertuples(index=False), start=1):
                    code = str(r.index_code)
                    try:
                        kdf = fetch_concept_kline_adata(adata_mod, code, start_date, end_date)
                        if not kdf.empty:
                            frames.append(kdf)
                    except Exception as e:
                        print(f"[CONCEPT] FAIL {code}: {e}")
                    if j % 10 == 0:
                        print(f"[CONCEPT] progress {j}/{len(hot)}")
                if frames:
                    cdf = pd.concat(frames, ignore_index=True)
                    upsert_kline(conn, cdf)
                    conn.commit()
                    print(f"[CONCEPT] saved rows={len(cdf)}")

        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("last_run", now_iso()))
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("last_asof", asof))
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("last_range", f"{start_date}..{end_date}"))
        conn.commit()

        cnt = pd.read_sql_query(
            "SELECT sec_type, COUNT(*) AS rows, COUNT(DISTINCT sec_code) AS n_codes "
            "FROM kline_daily GROUP BY sec_type ORDER BY sec_type",
            conn,
        )
        print("[RUN] DB summary:\n", cnt)
        print("[RUN] DONE db=", os.path.abspath(args.db))
    finally:
        bs_logout()
        conn.close()


if __name__ == "__main__":
    main()
